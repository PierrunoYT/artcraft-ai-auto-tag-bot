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

import discord
import httpx
from cryptography.fernet import Fernet, InvalidToken

from .config import Config

log = logging.getLogger(__name__)

MAX_REPLY_MATCHES = 3
# The starter can lag THREAD_CREATE by several seconds; back off before giving up.
NEW_POST_RETRY_DELAYS = (1, 2, 4, 8)


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


class DuplicateFinder:
    def __init__(
        self,
        client: discord.Client,
        embedder: Embedder,
        index: DuplicateIndex,
        config: Config,
        slots: asyncio.Semaphore,
    ):
        self.client = client
        self.embedder = embedder
        self.index = index
        self.config = config
        # Shared with the tagger so both respect TAG_CONCURRENCY.
        self.slots = slots

    async def _embed_post(
        self, thread: discord.Thread, retry_delays: Sequence[float] = ()
    ) -> array | None:
        for delay in (*retry_delays, None):
            try:
                starter = await thread.fetch_message(thread.id)
                break
            except discord.NotFound:
                if delay is None:
                    log.info("Thread %s not indexed: starter message is unavailable", thread.id)
                    return None
                await asyncio.sleep(delay)
        async with self.slots:
            return await self.embedder.embed(f"{thread.name[:256]}\n\n{starter.content[:6000]}")

    async def index_post(self, thread: discord.Thread, refresh: bool = False) -> bool:
        """Embed and store a post unless it is already indexed; True when stored."""
        if thread.parent_id not in self.config.forum_channel_ids:
            return False
        if thread.id in self.index.excluded or (thread.id in self.index and not refresh):
            return False
        vector = await self._embed_post(thread)
        return vector is not None and self.index.add(thread.id, thread.parent_id, vector)

    async def matches(
        self, forum_id: int, thread_id: int, vector: array, threshold: float, limit: int
    ) -> list[tuple[int, float]]:
        candidates = self.index.candidates(forum_id, exclude=thread_id)
        return await asyncio.to_thread(rank, candidates, vector, threshold, limit)

    async def check_new_post(self, thread: discord.Thread) -> list[tuple[int, float]]:
        """Reply with links to similar posts, then add the new post to the index."""
        if thread.parent_id not in self.config.forum_channel_ids:
            return []
        if thread.id in self.index.excluded:
            return []
        vector = await self._embed_post(thread, NEW_POST_RETRY_DELAYS)
        if vector is None:
            return []
        found = await self.matches(
            thread.parent_id,
            thread.id,
            vector,
            self.config.duplicate_threshold,
            MAX_REPLY_MATCHES,
        )
        self.index.add(thread.id, thread.parent_id, vector)
        if not found:
            log.info("Thread %s: no similar posts", thread.id)
            return []
        log.info(
            "Thread %s may duplicate: %s",
            thread.id,
            ", ".join(f"{t} ({score:.2f})" for t, score in found),
        )
        await self._reply(thread, found)
        return found

    async def similar_to(self, thread: discord.Thread, limit: int = 5) -> list[tuple[int, float]]:
        """Closest posts regardless of threshold, for moderators tuning detection."""
        vector = self.index.get(thread.id) or await self._embed_post(thread)
        if vector is None:
            return []
        return await self.matches(thread.parent_id, thread.id, vector, -1.0, limit)

    async def _reply(self, thread: discord.Thread, found: list[tuple[int, float]]) -> None:
        forum = self.client.get_channel(thread.parent_id)
        if forum is None or not forum.permissions_for(thread.guild.me).send_messages_in_threads:
            log.warning("Thread %s: missing Send Messages in Threads; no reply sent", thread.id)
            return
        lines = "\n".join(f"• <#{t}> ({score:.0%} similar)" for t, score in found)
        await thread.send(
            f"🔁 Possibly related posts:\n{lines}\n\n"
            "If one of these covers your post, consider adding to it there.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
