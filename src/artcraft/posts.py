"""Reading forum posts for classification and duplicate detection."""

import asyncio
from collections.abc import Sequence

import discord

# The starter of a brand-new post can lag THREAD_CREATE by several seconds.
NEW_POST_RETRY_DELAYS: Sequence[float] = (1, 2, 4, 8)
# Shorter titles ("Bug", "Question") say too little to tag or compare on their own.
MIN_TITLE_WORDS = 3


async def starter_text(thread: discord.Thread, retry_delays: Sequence[float] = ()) -> str | None:
    """The starter message's text, or None if it is deleted or unavailable.

    Only a forum post's starter (whose ID equals the thread ID) is read, never replies.
    """
    for delay in (*retry_delays, None):
        try:
            return (await thread.fetch_message(thread.id)).content
        except discord.NotFound:
            if delay is None:
                return None
            await asyncio.sleep(delay)
    return None


def title_is_usable(title: str) -> bool:
    """Whether a title alone is descriptive enough when the starter is gone."""
    return len(title.split()) >= MIN_TITLE_WORDS
