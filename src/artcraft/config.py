from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from cryptography.fernet import Fernet

DEFAULT_MODEL = "openai/gpt-6-luna"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    openrouter_api_key: str = field(repr=False)
    forum_channel_ids: frozenset[int]
    model: str = DEFAULT_MODEL
    max_ai_tags: int = 3
    # "add" keeps existing tags; "replace" swaps all non-moderator-only tags.
    tag_mode: Literal["add", "replace"] = "add"
    # Discord user IDs allowed to run /autotag; empty disables the commands.
    command_user_ids: frozenset[int] = frozenset()
    # Posts classified and written at the same time, across events and batches.
    tag_concurrency: int = 4
    # Duplicate detection stores encrypted embeddings; off unless explicitly enabled.
    duplicates: bool = False
    duplicate_db_key: str = field(default="", repr=False)
    duplicate_db_path: str = "duplicates.db"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # Minimum embedding similarity to shortlist a candidate for the AI check. Real
    # duplicates scored only 0.50-0.59 with text-embedding-3-small, close to related
    # posts, so the floor is low and the AI check makes the final call.
    duplicate_threshold: float = 0.45
    # Candidates shortlisted by embedding similarity and sent to the AI check.
    duplicate_candidates: int = 10
    # Most AI-confirmed duplicates linked in a reply.
    duplicate_max_matches: int = 5
    # "on": every post in a duplicate group lists the others (archived posts are
    # briefly reopened); "active": skip archived posts; "off": only the new post.
    duplicate_cross_link: Literal["on", "active", "off"] = "on"
    # "all": compare posts across every configured forum; "forum": only within one.
    duplicate_scope: Literal["all", "forum"] = "all"
    # Posts indexed at the same time; embeddings are cheap and fast, so this is
    # separate from (and higher than) TAG_CONCURRENCY.
    index_concurrency: int = 10
    # Action journal (IDs and timestamps only) for resuming runs, catching up after
    # downtime, and undoing the bot's changes.
    state_db_path: str = "artcraft_state.db"
    # Days to keep journal entries; 0 keeps them until the operator deletes them.
    action_retention_days: int = 90
    # Handle posts created while the bot was offline when it starts.
    catch_up: bool = True

    @classmethod
    def from_env(cls) -> Config:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name} must be set")
            return value

        def id_list(name: str, value: str) -> frozenset[int]:
            try:
                ids = frozenset(int(item.strip()) for item in value.split(","))
                if any(item <= 0 for item in ids):
                    raise ValueError
            except ValueError:
                raise ValueError(f"{name} must contain comma-separated positive IDs") from None
            return ids

        token = required("DISCORD_TOKEN")
        key = required("OPENROUTER_API_KEY")
        ids = id_list("FORUM_CHANNEL_IDS", required("FORUM_CHANNEL_IDS"))
        users = os.getenv("COMMAND_USER_IDS", "").strip()
        command_user_ids = id_list("COMMAND_USER_IDS", users) if users else frozenset()
        try:
            max_tags = int(os.getenv("MAX_AI_TAGS", "3"))
            if not 1 <= max_tags <= 5:
                raise ValueError
        except ValueError:
            raise ValueError("MAX_AI_TAGS must be an integer from 1 to 5") from None
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip()
        if not model or model.endswith(":online"):
            raise ValueError("OPENROUTER_MODEL must be nonempty and must not enable web search")
        tag_mode = os.getenv("TAG_MODE", "add").strip().lower()
        if tag_mode not in ("add", "replace"):
            raise ValueError("TAG_MODE must be 'add' or 'replace'")
        try:
            concurrency = int(os.getenv("TAG_CONCURRENCY", "4"))
            if not 1 <= concurrency <= 10:
                raise ValueError
        except ValueError:
            raise ValueError("TAG_CONCURRENCY must be an integer from 1 to 10") from None

        duplicates = os.getenv("DUPLICATES", "off").strip().lower()
        if duplicates not in ("on", "off"):
            raise ValueError("DUPLICATES must be 'on' or 'off'")
        db_key = os.getenv("DUPLICATE_DB_KEY", "").strip()
        if duplicates == "on":
            try:
                Fernet(db_key)
            except ValueError:
                raise ValueError(
                    "DUPLICATE_DB_KEY must be a Fernet key when DUPLICATES=on; generate one with "
                    '`uv run python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"`'
                ) from None
        db_path = os.getenv("DUPLICATE_DB_PATH", "duplicates.db").strip()
        if not db_path:
            raise ValueError("DUPLICATE_DB_PATH must be nonempty")
        embedding_model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
        if not embedding_model:
            raise ValueError("EMBEDDING_MODEL must be nonempty")
        try:
            threshold = float(os.getenv("DUPLICATE_THRESHOLD", "0.45"))
            if not 0.2 <= threshold <= 0.95:
                raise ValueError
        except ValueError:
            raise ValueError("DUPLICATE_THRESHOLD must be a number from 0.2 to 0.95") from None
        try:
            candidates = int(os.getenv("DUPLICATE_CANDIDATES", "10"))
            if not 1 <= candidates <= 25:
                raise ValueError
        except ValueError:
            raise ValueError("DUPLICATE_CANDIDATES must be an integer from 1 to 25") from None
        try:
            max_matches = int(os.getenv("DUPLICATE_MAX_MATCHES", "5"))
            if not 1 <= max_matches <= 10:
                raise ValueError
        except ValueError:
            raise ValueError("DUPLICATE_MAX_MATCHES must be an integer from 1 to 10") from None
        cross_link = os.getenv("DUPLICATE_CROSS_LINK", "on").strip().lower()
        if cross_link not in ("on", "active", "off"):
            raise ValueError("DUPLICATE_CROSS_LINK must be 'on', 'active', or 'off'")
        scope = os.getenv("DUPLICATE_SCOPE", "all").strip().lower()
        if scope not in ("all", "forum"):
            raise ValueError("DUPLICATE_SCOPE must be 'all' or 'forum'")
        try:
            index_concurrency = int(os.getenv("INDEX_CONCURRENCY", "10"))
            if not 1 <= index_concurrency <= 20:
                raise ValueError
        except ValueError:
            raise ValueError("INDEX_CONCURRENCY must be an integer from 1 to 20") from None
        state_db_path = os.getenv("STATE_DB_PATH", "artcraft_state.db").strip()
        if not state_db_path:
            raise ValueError("STATE_DB_PATH must be nonempty")
        try:
            retention = int(os.getenv("ACTION_RETENTION_DAYS", "90"))
            if not 0 <= retention <= 3650:
                raise ValueError
        except ValueError:
            raise ValueError(
                "ACTION_RETENTION_DAYS must be an integer from 0 (keep forever) to 3650"
            ) from None
        catch_up = os.getenv("CATCH_UP", "on").strip().lower()
        if catch_up not in ("on", "off"):
            raise ValueError("CATCH_UP must be 'on' or 'off'")

        return cls(
            discord_token=token,
            openrouter_api_key=key,
            forum_channel_ids=ids,
            model=model,
            max_ai_tags=max_tags,
            tag_mode=tag_mode,
            command_user_ids=command_user_ids,
            tag_concurrency=concurrency,
            duplicates=duplicates == "on",
            duplicate_db_key=db_key,
            duplicate_db_path=db_path,
            embedding_model=embedding_model,
            duplicate_threshold=threshold,
            duplicate_candidates=candidates,
            duplicate_max_matches=max_matches,
            duplicate_cross_link=cross_link,
            duplicate_scope=scope,
            index_concurrency=index_concurrency,
            state_db_path=state_db_path,
            action_retention_days=retention,
            catch_up=catch_up == "on",
        )
