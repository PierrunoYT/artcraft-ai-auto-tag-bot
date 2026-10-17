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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import discord
import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import posts
from .classifier import DuplicateJudge
from .config import Config
from .journal import Journal

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
            -- Cross-linked duplicate posts, stored in both directions.
            CREATE TABLE IF NOT EXISTS related (
                thread_id INTEGER NOT NULL,
                related_id INTEGER NOT NULL,
                PRIMARY KEY (thread_id, related_id)
            );
            -- The bot's "Related posts" message in each cross-linked post.
            CREATE TABLE IF NOT EXISTS notices (
                thread_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            );
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
            self.unrelate(thread_id)
            self.drop_notice(thread_id)
        return len(stale)

    def relate(self, thread_ids: Sequence[int]) -> list[tuple[int, int]]:
        """Mark every listed post as related to every other one; returns the pairs
        that were not related before (what an undo should remove)."""
        pairs = [(a, b) for a in thread_ids for b in thread_ids if a != b]
        query = "SELECT 1 FROM related WHERE thread_id = ? AND related_id = ?"
        new = [p for p in pairs if self.db.execute(query, p).fetchone() is None]
        self.db.executemany("INSERT OR IGNORE INTO related VALUES (?, ?)", new)
        self.db.commit()
        return new

    def unrelate_pairs(self, pairs: Sequence[Sequence[int]]) -> None:
        self.db.executemany(
            "DELETE FROM related WHERE thread_id = ? AND related_id = ?", [tuple(p) for p in pairs]
        )
        self.db.commit()

    def related(self, thread_id: int) -> list[int]:
        """Posts related to a post, newest first."""
        rows = self.db.execute(
            "SELECT related_id FROM related WHERE thread_id = ? ORDER BY related_id DESC",
            (thread_id,),
        )
        return [row[0] for row in rows]

    def unrelate(self, thread_id: int) -> list[int]:
        """Remove a post from all relations; returns the posts it was related to."""
        former = self.related(thread_id)
        self.db.execute(
            "DELETE FROM related WHERE thread_id = ? OR related_id = ?", (thread_id, thread_id)
        )
        self.db.commit()
        return former

    def notice(self, thread_id: int) -> int | None:
        row = self.db.execute(
            "SELECT message_id FROM notices WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return row[0] if row else None

    def set_notice(self, thread_id: int, message_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO notices VALUES (?, ?)", (thread_id, message_id))
        self.db.commit()

    def drop_notice(self, thread_id: int) -> None:
        self.db.execute("DELETE FROM notices WHERE thread_id = ?", (thread_id,))
        self.db.commit()

    def candidates(self, forum_ids: Iterable[int], exclude: int) -> list[tuple[int, array]]:
        # A snapshot, so ranking can run off the event loop while the index changes.
        return [
            (t, v)
            for forum_id in forum_ids
            for t, v in self.forums.get(forum_id, {}).items()
            if t != exclude
        ]

    def close(self) -> None:
        self.db.close()


@dataclass(frozen=True)
class Match:
    thread_id: int
    title: str
    score: float  # Embedding similarity, used to shortlist candidates.
    confirmed: bool  # Whether the AI check judged it a real duplicate.
    archived: bool = False  # Live state when known; replying revives an archived post.
    locked: bool = False


def post_link(guild_id: int, match: Match) -> str:
    """A masked link showing the post's live title (channel mentions of archived posts
    render as "unknown" in clients that have not loaded them)."""
    title = discord.utils.escape_markdown(match.title[:80]).replace("[", "(").replace("]", ")")
    return f"[{title}](https://discord.com/channels/{guild_id}/{match.thread_id})"


# Posts whose live titles are fetched per notice; the rest are counted as "more".
MAX_NOTICE_LINKS = 40
NOTICE_LIMIT = 2000  # Discord's message length limit.


class CrossLinker:
    """Keeps one "Related posts" message in each cross-linked post, listing all the
    others, and edits it as the group changes so old posts are not spammed."""

    def __init__(
        self,
        client: discord.Client,
        index: DuplicateIndex,
        config: Config,
        journal: Journal | None = None,
    ):
        self.client = client
        self.index = index
        self.config = config
        self.journal = journal
        self._locks: dict[int, asyncio.Lock] = {}

    def relate(self, group: list[int]) -> list[int]:
        """Relate posts, merging in every existing group they belong to, so all
        members of the merged group are related to each other. Journals the newly
        added pairs so an undo can remove them; returns the merged group."""
        merged = list(dict.fromkeys(group))
        for thread_id in group:
            merged += [t for t in self.index.related(thread_id) if t not in merged]
        new = self.index.relate(merged)
        if new and self.journal:
            self.journal.record("relate", group[0], after=new)
        return merged

    async def link(self, new_id: int, duplicate_ids: list[int]) -> None:
        """Relate a new post to its confirmed duplicates and the groups they already
        belong to, then update every member's notice."""
        merged = self.relate([new_id, *duplicate_ids])
        targets = merged if self.config.duplicate_cross_link != "off" else [new_id]
        for thread_id in targets:
            await self.refresh(thread_id)

    async def forget(self, thread_id: int) -> None:
        """Drop a post from its group (deleted, or on request) and update the others."""
        affected = self.index.unrelate(thread_id)
        self.index.drop_notice(thread_id)
        self.index.remove(thread_id)
        for other in affected:
            await self.refresh(other)

    async def refresh(self, thread_id: int) -> None:
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            gone = await self._refresh(thread_id)
        if gone:
            log.info("Thread %s no longer exists; removed from its duplicate group", thread_id)
            await self.forget(thread_id)

    async def _titles(self, thread_ids: list[int]) -> list[Match]:
        """Live titles of related posts; posts deleted on Discord are dropped here and
        their other links are fixed the next time those posts are refreshed."""
        found = []
        for thread_id in thread_ids:
            try:
                other = await self.client.fetch_channel(thread_id)
            except discord.NotFound:
                other = None
            except discord.HTTPException:
                continue
            if not isinstance(other, discord.Thread):
                self.index.unrelate(thread_id)
                self.index.drop_notice(thread_id)
                self.index.remove(thread_id)
                continue
            found.append(Match(thread_id, other.name, 0.0, True, other.archived, other.locked))
        return found

    def _content(self, guild_id: int, related: list[Match], unfetched: int = 0) -> str | None:
        """The notice text: as many links as fit in one Discord message (newest
        first), then "…and N more"."""
        if not related:
            return None
        header = "🔁 Related posts (found automatically):\n"
        footer = (
            "\n\nIf one of these covers the same request, please continue there instead "
            "of starting a new post. Archived posts come back when you reply in them."
        )
        budget = NOTICE_LIMIT - len(header) - len(footer) - len("\n…and 9999 more.")
        lines: list[str] = []
        for match in related:
            line = f"• {post_link(guild_id, match)}{state_note(match)}"
            if sum(len(x) + 1 for x in lines) + len(line) > budget:
                break
            lines.append(line)
        more = len(related) - len(lines) + max(unfetched, 0)
        extra = f"\n…and {more} more." if more else ""
        return header + "\n".join(lines) + extra + footer

    async def _refresh(self, thread_id: int) -> bool:
        """Create, edit, or delete a post's notice; True if the post no longer exists."""
        try:
            thread = await self.client.fetch_channel(thread_id)
        except discord.NotFound:
            return True
        if not isinstance(thread, discord.Thread) or thread.locked:
            return False  # Never post in locked threads.
        message_id = self.index.notice(thread_id)
        if message_id == 0:
            return False  # A moderator deleted the notice; respect that.
        if thread.archived and message_id is None and self.config.duplicate_cross_link == "active":
            return False
        forum = thread.parent
        if forum is None or not forum.permissions_for(thread.guild.me).send_messages_in_threads:
            log.warning("Thread %s: missing Send Messages in Threads; no notice", thread_id)
            return False
        related = self.index.related(thread_id)
        # Titles are read live; very large groups only fetch the newest posts.
        shown = await self._titles(related[:MAX_NOTICE_LINKS])
        content = self._content(thread.guild.id, shown, len(related) - MAX_NOTICE_LINKS)
        if content is None and message_id is None:
            return False
        reopened = thread.archived
        if reopened:
            # Discord only allows messages in unarchived threads; restore afterwards.
            await thread.edit(archived=False, reason="ArtCraft duplicate cross-link")
        try:
            await self._write(thread, message_id, content)
        finally:
            if reopened:
                try:
                    await thread.edit(archived=True, reason="ArtCraft duplicate cross-link")
                except discord.HTTPException:
                    log.warning("Thread %s could not be re-archived", thread_id)
        return False

    async def _write(self, thread: discord.Thread, message_id: int | None, content: str | None):
        none = discord.AllowedMentions.none()
        if message_id is not None:
            message = thread.get_partial_message(message_id)
            try:
                if content is None:
                    await message.delete()
                    self.index.drop_notice(thread.id)
                    self._record("notice_delete", thread.id, message_id)
                    log.info("Thread %s: related-posts notice removed", thread.id)
                else:
                    await message.edit(content=content, allowed_mentions=none)
                    self._record("notice_edit", thread.id, message_id)
                    log.info("Thread %s: related-posts notice updated", thread.id)
            except discord.NotFound:
                # Deleted by a moderator: remember that and do not post again.
                self.index.set_notice(thread.id, 0)
                log.info("Thread %s: notice was deleted; not reposting", thread.id)
            return
        sent = await thread.send(content, allowed_mentions=none)
        self.index.set_notice(thread.id, sent.id)
        self._record("notice_post", thread.id, sent.id)
        log.info("Thread %s: related-posts notice posted", thread.id)

    def _record(self, kind: str, thread_id: int, message_id: int) -> None:
        # Message IDs only; the notice text (which shows post titles) is not stored.
        if self.journal:
            self.journal.record(kind, thread_id, message_id=message_id)


def state_note(match: Match) -> str:
    """Tell members which linked posts can be revived by replying."""
    if match.locked:
        return " (locked)"
    if match.archived:
        return " (archived – reply there to bring it back)"
    return ""


class DuplicateFinder:
    def __init__(
        self,
        client: discord.Client,
        embedder: Embedder,
        index: DuplicateIndex,
        config: Config,
        slots: asyncio.Semaphore,
        judge: DuplicateJudge,
        journal: Journal | None = None,
    ):
        self.client = client
        self.embedder = embedder
        self.index = index
        self.config = config
        # Caps concurrent embedding requests (INDEX_CONCURRENCY), separately from tagging.
        self.slots = slots
        self.judge = judge
        self.links = CrossLinker(client, index, config, journal)

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
        # "all" compares posts across every configured forum; "forum" only within one.
        scope = (
            self.config.forum_channel_ids if self.config.duplicate_scope == "all" else {forum_id}
        )
        candidates = self.index.candidates(scope, exclude=thread_id)
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

    async def _load_cached(self, thread_id: int, cache: dict | None) -> tuple[str, str] | None:
        # Forum-wide runs see each post as a candidate many times; read it once.
        # The cache lives only in memory for the duration of the run.
        if cache is None:
            return await self._load(thread_id)
        if thread_id not in cache:
            cache[thread_id] = await self._load(thread_id)
        return cache[thread_id]

    async def confirm(
        self,
        title: str,
        body: str,
        shortlist: list[tuple[int, float]],
        cache: dict | None = None,
    ) -> list[Match]:
        """Shortlisted posts that still exist, AI-confirmed duplicates first (in the
        model's order), then the rest by similarity."""
        loaded = await asyncio.gather(*(self._load_cached(t, cache) for t, _ in shortlist))
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
        # Index first: a failed AI check must not leave the new post unindexed.
        self.index.add(thread.id, thread.parent_id, vector)
        found = await self._duplicates_of(thread, title, body, vector)
        if found:
            await self.links.link(thread.id, [m.thread_id for m in found])
        return found

    async def _duplicates_of(
        self,
        thread: discord.Thread,
        title: str,
        body: str,
        vector: array,
        cache: dict | None = None,
    ) -> list[Match]:
        """AI-confirmed duplicates of a post, most relevant first, capped for replies."""
        shortlist = await self.shortlist(
            thread.parent_id,
            thread.id,
            vector,
            self.config.duplicate_threshold,
            self.config.duplicate_candidates,
        )
        if not shortlist:
            log.info("Thread %s: no similar posts", thread.id)
            return []
        found = [m for m in await self.confirm(title, body, shortlist, cache) if m.confirmed]
        found = found[: self.config.duplicate_max_matches]
        if not found:
            log.info("Thread %s: %d candidates, none confirmed", thread.id, len(shortlist))
            return []
        log.info(
            "Thread %s duplicates (AI-confirmed): %s",
            thread.id,
            ", ".join(f"{m.thread_id} ({m.score:.2f})" for m in found),
        )
        return found

    async def find(self, thread: discord.Thread, cache: dict | None = None) -> list[Match]:
        """AI-confirmed duplicates of an existing post, indexing it first if needed.
        Posts nothing; `cache` shares candidate text across a forum-wide run."""
        if thread.parent_id not in self.config.forum_channel_ids:
            return []
        if thread.id in self.index.excluded:
            return []
        post = await self._read_post(thread)
        if post is None:
            return []
        vector = self.index.get(thread.id)
        if vector is None:
            vector = await self._embed(*post)
            self.index.add(thread.id, thread.parent_id, vector)
        return await self._duplicates_of(thread, *post, vector, cache)

    async def link_post(self, thread: discord.Thread) -> list[Match]:
        """Run the new-post check on an existing post and cross-link its group."""
        found = await self.find(thread)
        if found:
            await self.links.link(thread.id, [m.thread_id for m in found])
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
