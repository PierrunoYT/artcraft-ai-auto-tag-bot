"""Duplicate detection over an encrypted index of forum post embeddings.

Only post IDs, forum IDs, the embedding model name, and encrypted embedding vectors are
stored; no titles, message text, usernames, or author IDs.
"""

import asyncio
import logging
import math
import operator
import sqlite3
from array import array
from collections.abc import Sequence
from dataclasses import dataclass

import discord
import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import posts
from .classifier import DuplicateJudge
from .config import Config

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Safe error text: never includes post content, response bodies, or credentials."""


def normalize(values: Sequence[float]) -> array:
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("zero vector")
    return array("f", (value / norm for value in values))


def rank(
    candidates: list[tuple[int, array]], vector: array, threshold: float, limit: int
) -> list[tuple[int, float]]:
    # Vectors are unit length, so the dot product is the cosine similarity.
    scores = [(thread_id, sum(map(operator.mul, vector, other))) for thread_id, other in candidates]
    matches = sorted((s for s in scores if s[1] >= threshold), key=lambda s: s[1], reverse=True)
    return matches[:limit]


class Embedder:
    def __init__(self, http: httpx.AsyncClient, api_key: str, model: str):
        self.http = http
        self.api_key = api_key
        self.model = model

    async def embed(self, text: str) -> array:
        payload = {
            "model": self.model,
            "input": text,
            # Same fail-closed privacy routing as classification.
            "provider": {"zdr": True, "data_collection": "deny"},
        }
        try:
            response = await self.http.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=25,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise EmbeddingError(f"OpenRouter returned HTTP {exc.response.status_code}") from None
        except httpx.RequestError:
            raise EmbeddingError("OpenRouter embedding request failed or timed out") from None
        try:
            values = response.json()["data"][0]["embedding"]
            if not isinstance(values, list) or not values:
                raise ValueError
            if any(isinstance(v, bool) or not isinstance(v, int | float) for v in values):
                raise ValueError
            return normalize(values)
        except (ValueError, TypeError, KeyError, IndexError):
            raise EmbeddingError("OpenRouter returned an invalid embedding") from None


class DuplicateIndex:
    """Encrypted SQLite store of post embeddings, mirrored in memory per forum."""

    def __init__(self, path: str, key: str, model: str):
        self.fernet = Fernet(key)
        self.model = model
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                thread_id INTEGER PRIMARY KEY,
                forum_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                vector BLOB NOT NULL
            );
            -- Posts whose data was deleted on request; never indexed again.
            CREATE TABLE IF NOT EXISTS excluded (thread_id INTEGER PRIMARY KEY);
            """
        )
        # Vectors from another model are not comparable; drop them.
        self.db.execute("DELETE FROM posts WHERE model != ?", (model,))
        self.db.commit()
        self.forums: dict[int, dict[int, array]] = {}
        self.forum_of: dict[int, int] = {}
        for thread_id, forum_id, blob in self.db.execute(
            "SELECT thread_id, forum_id, vector FROM posts"
        ):
            try:
                vector = array("f", self.fernet.decrypt(blob))
            except InvalidToken:
                self.db.close()
                raise ValueError("DUPLICATE_DB_KEY cannot decrypt the existing index") from None
            self._remember(thread_id, forum_id, vector)
        self.excluded = {row[0] for row in self.db.execute("SELECT thread_id FROM excluded")}

    def __contains__(self, thread_id: int) -> bool:
        return thread_id in self.forum_of

    def __len__(self) -> int:
        return len(self.forum_of)

    def _remember(self, thread_id: int, forum_id: int, vector: array) -> None:
        self._forget(thread_id)
        self.forums.setdefault(forum_id, {})[thread_id] = vector
        self.forum_of[thread_id] = forum_id

    def _forget(self, thread_id: int) -> bool:
        forum_id = self.forum_of.pop(thread_id, None)
        if forum_id is None:
            return False
        del self.forums[forum_id][thread_id]
        return True

    def get(self, thread_id: int) -> array | None:
        forum_id = self.forum_of.get(thread_id)
        return None if forum_id is None else self.forums[forum_id][thread_id]

    def add(self, thread_id: int, forum_id: int, vector: array) -> bool:
        if thread_id in self.excluded:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO posts VALUES (?, ?, ?, ?)",
            (thread_id, forum_id, self.model, self.fernet.encrypt(vector.tobytes())),
        )
        self.db.commit()
        self._remember(thread_id, forum_id, vector)
        return True

    def remove(self, thread_id: int) -> bool:
        self.db.execute("DELETE FROM posts WHERE thread_id = ?", (thread_id,))
        self.db.commit()
        return self._forget(thread_id)

    def exclude(self, thread_id: int) -> bool:
        """Delete a post's data on request and keep it out of the index."""
        removed = self.remove(thread_id)
        self.db.execute("INSERT OR IGNORE INTO excluded VALUES (?)", (thread_id,))
        self.db.commit()
        self.excluded.add(thread_id)
        return removed

    def keep_forums(self, forum_ids: frozenset[int]) -> int:
        """Delete data for forums that are no longer configured."""
        stale = [t for t, f in self.forum_of.items() if f not in forum_ids]
        for thread_id in stale:
            self.remove(thread_id)
        return len(stale)

    def candidates(self, forum_id: int, exclude: int) -> list[tuple[int, array]]:
        # A snapshot, so ranking can run off the event loop while the index changes.
        return [(t, v) for t, v in self.forums.get(forum_id, {}).items() if t != exclude]

    def close(self) -> None:
        self.db.close()


@dataclass(frozen=True)
class Match:
    thread_id: int
    title: str
    score: float  # Embedding similarity, used to shortlist candidates.
    confirmed: bool  # Whether the AI check judged it a real duplicate.


def post_link(guild_id: int, match: Match) -> str:
    """A masked link showing the post's live title (channel mentions of archived posts
    render as "unknown" in clients that have not loaded them)."""
    title = discord.utils.escape_markdown(match.title[:80]).replace("[", "(").replace("]", ")")
    return f"[{title}](https://discord.com/channels/{guild_id}/{match.thread_id})"


class DuplicateFinder:
    def __init__(
        self,
        client: discord.Client,
        embedder: Embedder,
        index: DuplicateIndex,
        config: Config,
        slots: asyncio.Semaphore,
        judge: DuplicateJudge,
    ):
        self.client = client
        self.embedder = embedder
        self.index = index
        self.config = config
        # Caps concurrent embedding requests (INDEX_CONCURRENCY), separately from tagging.
        self.slots = slots
        self.judge = judge

    async def _read_post(
        self, thread: discord.Thread, retry_delays: Sequence[float] = ()
    ) -> tuple[str, str] | None:
        """(title, body) of a post; body is empty when only the title is usable."""
        title = thread.name[:256]
        text = await posts.starter_text(thread, retry_delays)
        if text is not None:
            return title, text[:6000]
        # The starter was deleted; the title is still public on Discord.
        if not posts.title_is_usable(title):
            log.info("Thread %s: no starter and the title is too short to use", thread.id)
            return None
        log.info("Thread %s: starter deleted; using the title only", thread.id)
        return title, ""

    async def _embed(self, title: str, body: str) -> array:
        async with self.slots:
            return await self.embedder.embed(f"{title}\n\n{body}" if body else title)

    async def _embed_post(
        self, thread: discord.Thread, retry_delays: Sequence[float] = ()
    ) -> tuple[array, str, str] | None:
        """(vector, title, body) for a post, or None if nothing usable remains."""
        post = await self._read_post(thread, retry_delays)
        if post is None:
            return None
        return await self._embed(*post), *post

    async def index_post(self, thread: discord.Thread, refresh: bool = False) -> str:
        """Embed and store a post; returns "indexed", "unchanged" (already indexed),
        or "skipped" (unconfigured forum, excluded, or nothing usable to embed)."""
        if thread.parent_id not in self.config.forum_channel_ids:
            return "skipped"
        if thread.id in self.index.excluded:
            return "skipped"
        if thread.id in self.index and not refresh:
            return "unchanged"
        embedded = await self._embed_post(thread)
        if embedded is None:
            # Never keep a vector derived from content that is gone.
            self.index.remove(thread.id)
            return "skipped"
        if not self.index.add(thread.id, thread.parent_id, embedded[0]):
            return "skipped"
        return "indexed"

    async def shortlist(
        self, forum_id: int, thread_id: int, vector: array, floor: float, limit: int
    ) -> list[tuple[int, float]]:
        candidates = self.index.candidates(forum_id, exclude=thread_id)
        return await asyncio.to_thread(rank, candidates, vector, floor, limit)

    async def _load(self, thread_id: int) -> tuple[str, str] | None:
        """A candidate's live (title, body) from Discord; un-indexes posts that no
        longer exist. Nothing read here is stored."""
        try:
            thread = await self.client.fetch_channel(thread_id)
        except discord.NotFound:
            thread = None
        except discord.HTTPException:
            return None  # E.g. no access right now; keep the entry.
        if not isinstance(thread, discord.Thread):
            self.index.remove(thread_id)
            log.info("Thread %s removed from the duplicate index (no longer exists)", thread_id)
            return None
        text = await posts.starter_text(thread)
        return thread.name[:256], (text or "")[:1000]

    async def confirm(
        self, title: str, body: str, shortlist: list[tuple[int, float]]
    ) -> list[Match]:
        """Shortlisted posts that still exist, AI-confirmed duplicates first (in the
        model's order), then the rest by similarity."""
        loaded = await asyncio.gather(*(self._load(t) for t, _ in shortlist))
        live = {t: post for (t, _), post in zip(shortlist, loaded, strict=True) if post}
        if not live:
            return []
        chosen = await self.judge.confirm(title, body, {str(t): p for t, p in live.items()})
        order = {int(t): i for i, t in enumerate(chosen)}
        matches = [Match(t, live[t][0], score, t in order) for t, score in shortlist if t in live]
        return sorted(matches, key=lambda m: (not m.confirmed, order.get(m.thread_id, 0)))

    async def check_new_post(self, thread: discord.Thread) -> list[Match]:
        """Reply with links to AI-confirmed duplicates, then add the post to the index."""
        if thread.parent_id not in self.config.forum_channel_ids:
            return []
        if thread.id in self.index.excluded:
            return []
        embedded = await self._embed_post(thread, posts.NEW_POST_RETRY_DELAYS)
        if embedded is None:
            return []
        vector, title, body = embedded
        shortlist = await self.shortlist(
            thread.parent_id,
            thread.id,
            vector,
            self.config.duplicate_threshold,
            self.config.duplicate_candidates,
        )
        self.index.add(thread.id, thread.parent_id, vector)
        if not shortlist:
            log.info("Thread %s: no similar posts", thread.id)
            return []
        found = [m for m in await self.confirm(title, body, shortlist) if m.confirmed]
        found = found[: self.config.duplicate_max_matches]
        if not found:
            log.info("Thread %s: %d candidates, none confirmed", thread.id, len(shortlist))
            return []
        log.info(
            "Thread %s duplicates (AI-confirmed): %s",
            thread.id,
            ", ".join(f"{m.thread_id} ({m.score:.2f})" for m in found),
        )
        await self._reply(thread, found)
        return found

    async def review(self, thread: discord.Thread, limit: int = 10) -> list[Match]:
        """The closest posts regardless of the floor, with the AI verdict a new post
        would get, for moderators checking detection quality."""
        post = await self._read_post(thread)
        if post is None:
            return []
        # Reuse the stored vector when the post is indexed; embed only if it is not.
        vector = self.index.get(thread.id) or await self._embed(*post)
        shortlist = await self.shortlist(thread.parent_id, thread.id, vector, -1.0, limit)
        if not shortlist:
            return []
        return await self.confirm(*post, shortlist)

    async def _reply(self, thread: discord.Thread, found: list[Match]) -> None:
        forum = self.client.get_channel(thread.parent_id)
        if forum is None or not forum.permissions_for(thread.guild.me).send_messages_in_threads:
            log.warning("Thread %s: missing Send Messages in Threads; no reply sent", thread.id)
            return
        lines = "\n".join(f"• {post_link(thread.guild.id, m)}" for m in found)
        await thread.send(
            f"🔁 Possibly related posts:\n{lines}\n\n"
            "If one of these covers your post, consider adding to it there.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
