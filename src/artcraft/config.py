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
    # Calibrated on text-embedding-3-small: rephrased duplicates scored ~0.77-0.78,
    # related but different requests at most ~0.52.
    duplicate_threshold: float = 0.7

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
            threshold = float(os.getenv("DUPLICATE_THRESHOLD", "0.7"))
            if not 0.5 <= threshold <= 0.99:
                raise ValueError
        except ValueError:
            raise ValueError("DUPLICATE_THRESHOLD must be a number from 0.5 to 0.99") from None

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
        )
