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
