import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import discord

from .classifier import Classifier
from .config import Config

log = logging.getLogger(__name__)

# Discord's limit on applied tags per forum post.
MAX_APPLIED_TAGS = 5


@dataclass(frozen=True)
class TagResult:
    status: Literal["updated", "unchanged", "skipped"]
    reason: str
    added: int = 0
    removed: int = 0


def skipped(reason: str) -> TagResult:
    return TagResult("skipped", reason)


class Tagger:
    def __init__(self, client: discord.Client, classifier: Classifier, config: Config):
        self.client = client
        self.classifier = classifier
        self.config = config
        # Serialize this bot's classifications/writes and cap concurrent API spend.
        self.lock = asyncio.Lock()

    async def tag(self, thread_id: int, include_archived: bool = False) -> TagResult:
        result = await self._tag(thread_id, include_archived)
        # Reasons hold only tag names and status text, never post content.
        log.info("Thread %s %s: %s", thread_id, result.status, result.reason)
        return result

    async def _tag(self, thread_id: int, include_archived: bool) -> TagResult:
        async with self.lock:
            thread = await self.client.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                return skipped("Not a forum thread.")
            if thread.parent_id not in self.config.forum_channel_ids:
                return skipped("Forum is not enabled.")
            if thread.locked:
                return skipped("Locked threads are not changed.")
            if thread.archived and not include_archived:
                return skipped("Archived threads are not changed.")
            # The gateway keeps the cached forum current; tags are re-fetched before writing.
            forum = self.client.get_channel(thread.parent_id)
            if forum is None:
                forum = await self.client.fetch_channel(thread.parent_id)
            if not isinstance(forum, discord.ForumChannel):
                return skipped("Not a forum channel.")
            permissions = forum.permissions_for(thread.guild.me)
            if not all(
                (
                    permissions.view_channel,
                    permissions.read_message_history,
                    permissions.manage_threads,
                )
            ):
                return skipped("Bot needs View Channel, Read Message History, and Manage Threads.")
            # Moderator-only tags are never offered: post text could otherwise talk the
            # model into applying a tag the author is not allowed to set.
            candidates = {
                str(tag.id): tag.name for tag in forum.available_tags if not tag.moderated
            }
            if not candidates:
                return skipped("No forum tags are available to members.")
            # The starter can briefly lag THREAD_CREATE. Never substitute a reply
            # if the starter has been deleted or is unavailable.
            for attempt in range(3):
                try:
                    starter = await thread.fetch_message(thread.id)
                    break
                except discord.NotFound:
                    if attempt == 2:
                        return skipped("Starter message is unavailable.")
                    await asyncio.sleep(1)
            suggestions = await self.classifier.classify(thread.name, starter.content, candidates)

            # Re-read after AI latency; do not apply a classification that includes
            # deleted tags, or change a thread that has since been locked/archived.
            forum = await self.client.fetch_channel(thread.parent_id)
            fresh = await self.client.fetch_channel(thread.id)
            if fresh.locked or (fresh.archived and not include_archived):
                return skipped("Thread was archived or locked during classification.")
            allowed = {tag.id for tag in forum.available_tags if not tag.moderated}
            if any(tag_id not in allowed for tag_id in suggestions):
                return skipped(
                    "Suggested forum tags were deleted or made moderator-only "
                    "during classification; retry."
                )
            # Raw IDs include tags missing from discord.py's cached parent metadata.
            existing = list(fresh._applied_tags)
            if self.config.tag_mode == "replace":
                # Moderator-only tags are outside the AI's choice, so keep them.
                moderated = {tag.id for tag in forum.available_tags if tag.moderated}
                kept = [tag_id for tag_id in existing if tag_id in moderated]
            else:
                kept = existing
            final = (kept + [tag_id for tag_id in suggestions if tag_id not in kept])[
                :MAX_APPLIED_TAGS
            ]
            added = len(set(final) - set(existing))
            removed = len(set(existing) - set(final))
            if not added and not removed:
                return TagResult("unchanged", "Tags already match the classification.")
            if not final and forum.flags.require_tag:
                return skipped("Forum requires a tag; the empty selection was not applied.")
            # Discord has no conditional write; edits by others during classification
            # can be overwritten.
            applied_tags = [discord.Object(tag_id) for tag_id in final]
            reason = f"ArtCraft AI tagging ({self.config.tag_mode})"
            if fresh.archived:
                # Discord only edits unarchived threads: unarchive in the same request,
                # then archive again so the post keeps its state.
                await fresh.edit(archived=False, applied_tags=applied_tags, reason=reason)
                try:
                    await fresh.edit(archived=True, reason=f"{reason}; restore archive")
                except discord.HTTPException:
                    log.warning("Thread %s was retagged but could not be re-archived", fresh.id)
            else:
                await fresh.edit(applied_tags=applied_tags, reason=reason)
            names = {tag.id: tag.name for tag in forum.available_tags}
            changes = [f"+{names.get(t, t)}" for t in final if t not in existing]
            changes += [f"-{names.get(t, t)}" for t in existing if t not in final]
            return TagResult("updated", f"Retagged: {', '.join(changes)}.", added, removed)
