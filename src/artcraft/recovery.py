"""Recovery from the action journal: resume interrupted runs after a restart, catch up
on posts created while the bot was offline, and undo the bot's own changes."""

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING

import discord

from .journal import Action, current_run, now, stamp

if TYPE_CHECKING:
    from .bot import ArtCraftBot

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60
# Posts created shortly before the last heartbeat may not have been handled yet.
CATCH_UP_MARGIN = datetime.timedelta(minutes=10)
# Background run kinds that can resume, and the command group that runs them.
RESUMABLE = {
    "tag_all": "tag_commands",
    "index": "duplicate_commands",
    "link_all": "duplicate_commands",
}


class Recovery:
    def __init__(self, bot: "ArtCraftBot"):
        self.bot = bot

    def mark_seen(self) -> None:
        self.bot.journal.set_state("last_seen", stamp(now()))

    async def start(self) -> None:
        """Run once after connecting: purge old journal entries, resume interrupted
        runs, catch up on missed posts, then record uptime every minute."""
        journal = self.bot.journal
        removed = journal.purge(self.bot.config.action_retention_days)
        if removed:
            log.info("Journal: removed %d entries older than the retention period", removed)
        last_seen = journal.get_state("last_seen")  # Read before the heartbeat moves it.
        await self.resume_runs()
        if self.bot.config.catch_up:
            await self.catch_up(last_seen)
        while True:
            self.mark_seen()
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def resume_runs(self) -> None:
        journal, config = self.bot.journal, self.bot.config
        for run in journal.interrupted_runs():
            attr = RESUMABLE.get(run.kind)
            group = getattr(self.bot, attr) if attr else None
            forum = self.bot.get_channel(run.forum_id)
            usable = (
                group is not None
                and isinstance(forum, discord.ForumChannel)
                and run.forum_id in config.forum_channel_ids
                and (attr != "duplicate_commands" or self.bot.duplicates is not None)
            )
            if not usable:
                # Single commands and runs that cannot continue keep their journaled
                # changes (still undoable) but are not resumed.
                journal.finish_run(run.id, "interrupted")
                log.info("Run #%s (%s) was interrupted and cannot resume", run.id, run.kind)
                continue
            if forum.id in group.running_batches:
                continue
            log.info("Resuming run #%s (%s) on forum %s", run.id, run.kind, forum.id)
            try:
                await group.start_full_run(
                    None, forum, run.kind, run.options, resume=run, user_id=run.user_id
                )
            except discord.HTTPException as exc:
                log.warning("Run #%s could not resume (HTTP %s)", run.id, exc.status)

    async def catch_up(self, last_seen: str | None) -> None:
        """Handle posts created while the bot was offline that it has not handled."""
        if last_seen is None:
            log.info("First start with the journal: nothing to catch up on")
            return
        cutoff = datetime.datetime.fromisoformat(last_seen) - CATCH_UP_MARGIN
        forums = self.bot.config.forum_channel_ids
        missed: list[discord.Thread] = []
        for guild in self.bot.guilds:
            try:
                threads = await guild.active_threads()
            except discord.HTTPException as exc:
                log.warning("Catch-up: could not list threads (HTTP %s)", exc.status)
                continue
            missed += [
                t
                for t in threads
                if t.parent_id in forums
                and discord.utils.snowflake_time(t.id) >= cutoff
                and not self.bot.journal.is_handled(t.id)
            ]
        if not missed:
            log.info("Catch-up: no posts were missed while offline")
            return
        log.info("Catch-up: handling %d post(s) created while offline", len(missed))
        for thread in sorted(missed, key=lambda t: t.id):
            await self.bot.process_new_post(thread)

    async def undo(self, actions: list[Action], user_id: int | None, label: str) -> str:
        """Revert journaled changes, newest first. Tags are restored only where they
        still match what the bot left; duplicate links are removed and the affected
        notices are updated or deleted. The undo itself is journaled as a run."""
        journal = self.bot.journal
        run_id = journal.start_run("undo", 0, {"label": label, "actions": len(actions)}, user_id)
        token = current_run.set(run_id)
        counts = {"undone": 0, "conflict": 0, "skipped": 0, "failed": 0}
        done: list[int] = []
        affected: set[int] = set()
        try:
            for action in actions:
                if action.kind == "tags":
                    try:
                        status = await self.bot.tagger.set_tags(
                            action.thread_id, action.after, action.before
                        )
                    except discord.HTTPException:
                        status = "failed"
                    if status in ("undone", "unchanged"):
                        done.append(action.id)
                        counts["undone"] += 1
                    else:
                        counts[status] += 1
                elif self.bot.duplicates is None:
                    counts["skipped"] += 1
                elif action.kind == "relate":
                    self.bot.duplicates.index.unrelate_pairs(action.after)
                    affected.update(thread_id for pair in action.after for thread_id in pair)
                    done.append(action.id)
                    counts["undone"] += 1
                elif action.kind == "notice_post":
                    # Removed (or trimmed) below once the links behind it are gone.
                    affected.add(action.thread_id)
                    done.append(action.id)
                    counts["undone"] += 1
            if self.bot.duplicates:
                for thread_id in sorted(affected, reverse=True):
                    try:
                        await self.bot.duplicates.links.refresh(thread_id)
                    except discord.HTTPException:
                        counts["failed"] += 1
            journal.mark_undone(done)
        finally:
            current_run.reset(token)
            journal.finish_run(run_id, "finished")
        log.info("Undo of %s (run #%s): %s", label, run_id, counts)
        return (
            f"{counts['undone']} change(s) undone, {counts['conflict']} left alone because "
            f"someone changed them since, {counts['skipped']} skipped, "
            f"{counts['failed']} failed. This undo is run #{run_id}."
        )

    def thread_plan(self, thread_id: int) -> tuple[list[Action], list[Action], set[int]]:
        """What undoing everything for one post covers: its tag changes, the duplicate
        link actions involving it, and its duplicate group (the post itself included),
        from current links and the journal."""
        actions = self.bot.journal.actions()
        tags = [a for a in actions if a.kind == "tags" and a.thread_id == thread_id]
        links = [
            a for a in actions if a.kind == "relate" and any(thread_id in pair for pair in a.after)
        ]
        group = {thread_id} | {t for a in links for pair in a.after for t in pair}
        if self.bot.duplicates:
            group |= set(self.bot.duplicates.index.related(thread_id))
        return tags, links, group

    async def undo_thread(self, thread_id: int, user_id: int | None) -> str:
        """Undo everything the bot did for one post: restore its tags, unlink its
        duplicate group, and delete the bot's messages in every post of that group
        the same way they were posted (one per post). Members still linked to posts
        outside the group get their notice updated instead of deleted."""
        journal = self.bot.journal
        tags, links, group = self.thread_plan(thread_id)
        run_id = journal.start_run("undo_thread", 0, {"thread_id": thread_id}, user_id)
        token = current_run.set(run_id)
        restored = conflicts = deleted = updated = failed = 0
        done: list[int] = []
        try:
            for action in tags:
                try:
                    status = await self.bot.tagger.set_tags(thread_id, action.after, action.before)
                except discord.HTTPException:
                    status = "failed"
                if status in ("undone", "unchanged"):
                    done.append(action.id)
                    restored += status == "undone"
                elif status == "conflict":
                    conflicts += 1
                elif status == "failed":
                    failed += 1
            duplicates = self.bot.duplicates
            if duplicates:
                duplicates.index.unrelate_pairs([(a, b) for a in group for b in group if a != b])
            done += [a.id for a in links]
            for member in sorted(group, reverse=True):
                try:
                    if duplicates and duplicates.index.related(member):
                        await duplicates.links.refresh(member)  # Linked elsewhere too.
                        updated += 1
                    else:
                        deleted += await self.delete_bot_messages(member)
                        if duplicates:
                            duplicates.index.drop_notice(member)
                except discord.HTTPException as exc:
                    failed += 1
                    log.warning("Thread %s: undo of bot messages failed (%s)", member, exc.status)
            journal.mark_undone(done)
        finally:
            current_run.reset(token)
            journal.finish_run(run_id, "finished")
        log.info(
            "Undo of thread %s (run #%s): %d tag change(s) restored, %d conflict(s), "
            "%d post(s) unlinked, %d message(s) deleted, %d notice(s) updated, %d failed",
            thread_id,
            run_id,
            restored,
            conflicts,
            len(group) - 1,
            deleted,
            updated,
            failed,
        )
        return (
            f"{restored} tag change(s) restored"
            + (f" ({conflicts} left alone because someone changed them since)" if conflicts else "")
            + f", unlinked from {len(group) - 1} related post(s), deleted {deleted} bot "
            f"message(s) across {len(group)} post(s)"
            + (f", updated {updated} notice(s) still linked elsewhere" if updated else "")
            + (f", {failed} failed" if failed else "")
            + f". This undo is run #{run_id}."
        )

    async def delete_bot_messages(self, thread_id: int) -> int:
        """Delete every message the bot posted in a post, reopening it briefly if it
        is archived. Locked posts are left alone. Returns how many were deleted."""
        try:
            thread = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return 0
        if not isinstance(thread, discord.Thread) or thread.locked:
            return 0
        mine = [m async for m in thread.history(limit=500) if m.author.id == self.bot.user.id]
        if not mine:
            return 0
        reopened = thread.archived
        if reopened:
            # Discord only allows deleting messages in unarchived threads.
            await thread.edit(archived=False, reason="ArtCraft undo")
        try:
            for message in mine:
                await message.delete()
                self.bot.journal.record("notice_delete", thread_id, message_id=message.id)
        finally:
            if reopened:
                try:
                    await thread.edit(archived=True, reason="ArtCraft undo")
                except discord.HTTPException:
                    log.warning("Thread %s could not be re-archived", thread_id)
        return len(mine)
