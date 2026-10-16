import asyncio
from dataclasses import dataclass

import discord

from .classifier import Classifier
from .config import Config


@dataclass(frozen=True)
class TagResult:
    added: int
    reason: str
    removed: int = 0


class Tagger:
    def __init__(self, client: discord.Client, classifier: Classifier, config: Config):
        self.client = client
        self.classifier = classifier
        self.config = config
        # Serialize this bot's classifications/writes and cap concurrent API spend.
        self.lock = asyncio.Lock()

    async def tag(self, thread_id: int) -> TagResult:
        async with self.lock:
            thread = await self.client.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                return TagResult(0, "Not a forum thread.")
            if thread.parent_id not in self.config.forum_channel_ids:
                return TagResult(0, "Forum is not enabled.")
            if thread.archived or thread.locked:
                return TagResult(0, "Archived or locked threads are not changed.")
            forum = await self.client.fetch_channel(thread.parent_id)
            if not isinstance(forum, discord.ForumChannel):
                return TagResult(0, "Not a forum channel.")
            permissions = forum.permissions_for(thread.guild.me)
            if not all(
                (
                    permissions.view_channel,
                    permissions.read_message_history,
                    permissions.manage_threads,
                )
            ):
                return TagResult(
                    0, "Bot needs View Channel, Read Message History, and Manage Threads."
                )
            candidates = {str(tag.id): tag.name for tag in forum.available_tags}
            if not candidates:
                return TagResult(0, "No forum tags are available.")
            # The starter can briefly lag THREAD_CREATE. Never substitute a reply
            # if the starter has been deleted or is unavailable.
            for attempt in range(3):
                try:
                    starter = await thread.fetch_message(thread.id)
                    break
                except discord.NotFound:
                    if attempt == 2:
                        return TagResult(0, "Starter message is unavailable.")
                    await asyncio.sleep(1)
            suggestions = await self.classifier.classify(thread.name, starter.content, candidates)

            # Re-read after AI latency; do not apply a classification that includes
            # deleted tags, or change a thread that has since been locked/archived.
            forum = await self.client.fetch_channel(thread.parent_id)
            fresh = await self.client.fetch_channel(thread.id)
            if fresh.archived or fresh.locked:
                return TagResult(0, "Thread was archived or locked during classification.")
            allowed = {tag.id for tag in forum.available_tags}
            if any(tag_id not in allowed for tag_id in suggestions):
                return TagResult(
                    0, "Suggested forum tags were deleted during classification; retry."
                )
            # Raw IDs include tags missing from discord.py's cached parent metadata.
            existing = set(fresh._applied_tags)
            selected = set(suggestions)
            added = len(selected - existing)
            removed = len(existing - selected)
            if not added and not removed:
                return TagResult(0, "Tags already match the classification.")
            # Replace all tags, including manual ones. A valid empty selection clears
            # the list; errors never reach this write. Discord has no conditional write.
            await fresh.edit(
                applied_tags=[discord.Object(tag_id) for tag_id in suggestions],
                reason="ArtCraft AI retag (full replacement)",
            )
            return TagResult(added, f"Retagged: added {added}, removed {removed} tag(s).", removed)
