from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    openrouter_api_key: str = field(repr=False)
    forum_channel_ids: frozenset[int]
    model: str = "openai/gpt-4.1-mini"
    max_ai_tags: int = 3

    @classmethod
    def from_env(cls) -> Config:
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name} must be set")
            return value

        token = required("DISCORD_TOKEN")
        key = required("OPENROUTER_API_KEY")
        try:
            ids = frozenset(
                int(value.strip()) for value in required("FORUM_CHANNEL_IDS").split(",")
            )
            if any(value <= 0 for value in ids):
                raise ValueError
        except ValueError:
            raise ValueError(
                "FORUM_CHANNEL_IDS must contain comma-separated positive IDs"
            ) from None
        try:
            max_tags = int(os.getenv("MAX_AI_TAGS", "3"))
            if not 1 <= max_tags <= 5:
                raise ValueError
        except ValueError:
            raise ValueError("MAX_AI_TAGS must be an integer from 1 to 5") from None
        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini").strip()
        if not model or model.endswith(":online"):
            raise ValueError("OPENROUTER_MODEL must be nonempty and must not enable web search")
        return cls(token, key, ids, model, max_tags)
