from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

DEFAULT_MODEL = "openai/gpt-6-luna"


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
        return cls(token, key, ids, model, max_tags, tag_mode, command_user_ids)
