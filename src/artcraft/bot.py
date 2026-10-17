import asyncio
import contextlib
import datetime
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

import discord
import httpx
from discord import app_commands

from .classifier import ClassificationError, Classifier, DuplicateJudge
from .config import Config
from .duplicates import (
    DuplicateFinder,
    DuplicateIndex,
    Embedder,
    EmbeddingError,
    Match,
    post_link,
)
from .journal import Action, Journal, Run, current_run, now
from .recovery import Recovery
from .tagging import Tagger

log = logging.getLogger(__name__)

TAG_STATUSES = ("updated", "unchanged", "skipped", "failed")
INDEX_STATUSES = ("indexed", "unchanged", "skipped", "failed")
LINK_STATUSES = ("grouped", "unique", "skipped", "failed")


def new_counts(keys: tuple[str, ...] = TAG_STATUSES) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


@dataclass
class Job:
    """What a background forum run does with each post."""

    verb: str
    statuses: tuple[str, ...]
    workers: int
    handle: Callable[[discord.Thread], Awaitable[str | tuple[str, Any]]]
    # Runs after the posts (given whether the run was stopped); returns summary text.
    finish: Callable[[bool], Awaitable[str]] | None = None


class ArtCraftBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
        # Starter edits and deletions keep the duplicate index in sync with Discord.
        intents.guild_messages = config.duplicates
        super().__init__(intents=intents, max_messages=None)
        self.config = config
        self.http_client = httpx.AsyncClient()
        self.journal = Journal(config.state_db_path)
        self.tagger = Tagger(
            self,
            Classifier(
                self.http_client, config.openrouter_api_key, config.model, config.max_ai_tags
            ),
            config,
            self.journal,
        )
        self.duplicates: DuplicateFinder | None = None
        if config.duplicates:
            self.duplicates = DuplicateFinder(
                self,
                Embedder(self.http_client, config.openrouter_api_key, config.embedding_model),
                DuplicateIndex(
                    config.duplicate_db_path, config.duplicate_db_key, config.embedding_model
                ),
                config,
                asyncio.Semaphore(config.index_concurrency),
                DuplicateJudge(self.http_client, config.openrouter_api_key, config.model),
                self.journal,
            )
        self.tree = app_commands.CommandTree(self)
        self.tag_commands = TagCommands(self)
        self.duplicate_commands = DuplicateCommands(self)
        self.tree.add_command(self.tag_commands)
        self.tree.add_command(self.duplicate_commands)
        self.tree.add_command(HistoryCommands(self))
        self.tree.on_error = self.command_error
        self.recovery = Recovery(self)
        self._recovery_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        if self.duplicates:
            removed = self.duplicates.index.keep_forums(self.config.forum_channel_ids)
            log.info(
                "Duplicate index: %d posts (%d removed from unconfigured forums)",
                len(self.duplicates.index),
                removed,
            )
        await self.tree.sync()

    async def close(self) -> None:
        try:
            if self._recovery_task:
                self._recovery_task.cancel()
            self.recovery.mark_seen()  # Catch-up starts from here next time.
            await super().close()
        finally:
            await self.http_client.aclose()
            if self.duplicates:
                self.duplicates.index.close()
            self.journal.close()

    async def on_ready(self) -> None:
        log.info("ArtCraft connected; %d forum(s) configured", len(self.config.forum_channel_ids))
        # on_ready repeats after reconnects; recover only once per process.
        if self._recovery_task is None:
            self._recovery_task = asyncio.create_task(self.recovery.start())

    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id not in self.config.forum_channel_ids:
            return
        log.info("New post %s in forum %s", thread.id, thread.parent_id)
        await self.process_new_post(thread)

    async def process_new_post(self, thread: discord.Thread) -> None:
        """Tag and duplicate-check a new post, then remember it was handled."""
        # Tagging and duplicate checks are independent: neither waits for nor
        # depends on the other's success.
        jobs = [self.tag_new_post(thread)]
        if self.duplicates:
            jobs.append(self.check_new_post_duplicates(thread))
        await asyncio.gather(*jobs)
        self.journal.mark_handled(thread.id)

    async def tag_new_post(self, thread: discord.Thread) -> None:
        try:
            await self.tagger.tag(thread.id, new_post=True)
        except (ClassificationError, discord.HTTPException) as exc:
            log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)

    async def check_new_post_duplicates(self, thread: discord.Thread) -> None:
        try:
            await self.duplicates.check_new_post(thread)
        except (EmbeddingError, ClassificationError, discord.HTTPException) as exc:
            # A failed AI check never produces a reply; the post is still indexed.
            log.warning("Thread %s duplicate check failed (%s)", thread.id, type(exc).__name__)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if not self.duplicates:
            return
        thread_id = payload.thread_id
        if thread_id in self.duplicates.index:
            log.info("Thread %s removed from the duplicate index (deleted on Discord)", thread_id)
        try:
            # Also drops it from its duplicate group and updates the others' notices.
            await self.duplicates.links.forget(thread_id)
        except discord.HTTPException as exc:
            log.warning("Thread %s: updating related notices failed (%s)", thread_id, exc.status)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id == payload.channel_id:  # A forum post's starter message.
            await self.reindex_starter_change(payload.channel_id, "deleted")

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.channel_id in payload.message_ids:
            await self.reindex_starter_change(payload.channel_id, "deleted")

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.message_id == payload.channel_id:
            await self.reindex_starter_change(payload.channel_id, "edited")

    async def reindex_starter_change(self, thread_id: int, change: str) -> None:
        """Re-embed an indexed post after its starter changed. A deleted starter
        leaves a title-only entry, or none if the title is too short."""
        if not self.duplicates or thread_id not in self.duplicates.index:
            return
        try:
            thread = await self.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread):
                raise TypeError
            status = await self.duplicates.index_post(thread, refresh=True)
            log.info(
                "Thread %s re-indexed after its starter was %s (%s)", thread_id, change, status
            )
        except (EmbeddingError, discord.HTTPException, TypeError) as exc:
            # Never keep a vector derived from content that may be gone or changed.
            self.duplicates.index.remove(thread_id)
            log.warning(
                "Thread %s removed from the duplicate index; re-index after starter %s failed (%s)",
                thread_id,
                change,
                type(exc).__name__,
            )

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        # Log the traceback for debugging, but never the event arguments, which
        # contain Discord content.
        log.exception("Discord event %s failed", event_method)

    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        command = getattr(interaction.command, "qualified_name", "unknown")
        if isinstance(error, app_commands.CommandInvokeError):
            log.error(
                "/%s by user %s failed (%s)",
                command,
                interaction.user.id,
                type(error.original).__name__,
                exc_info=error.original,
            )
        else:
            log.warning(
                "/%s by user %s refused (%s)", command, interaction.user.id, type(error).__name__
            )
        message = (
            "Command failed. Check bot permissions and configuration, then inspect the thread."
        )
        if isinstance(error, app_commands.MissingPermissions):
            message = "This command requires Manage Threads in a server channel."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You are not allowed to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class ModeratorGroup(app_commands.Group):
    """Shared checks, thread lookup, and background forum runs for command groups.

    Defines no commands itself: discord.py collects a group's commands only when its
    base classes have none.
    """

    def __init__(self, bot: ArtCraftBot):
        super().__init__()
        self.bot = bot
        self.running_batches: set[int] = set()
        # Background full-forum runs by forum ID; holding the task keeps it alive.
        self.full_runs: dict[int, asyncio.Task] = {}
        self.stop_requests: set[int] = set()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Runs before every subcommand; a False result raises CheckFailure.
        return interaction.user.id in self.bot.config.command_user_ids

    def can_manage(self, interaction: discord.Interaction, channel) -> bool:
        if interaction.guild_id != channel.guild.id or not isinstance(
            interaction.user, discord.Member
        ):
            return False
        permissions = channel.permissions_for(interaction.user)
        return permissions.view_channel and permissions.manage_threads

    async def parse_thread_id(
        self, interaction: discord.Interaction, thread_id: str | None
    ) -> int | None:
        """Parse a thread ID option (default: the current channel), replying on error."""
        try:
            target_id = int(thread_id) if thread_id is not None else interaction.channel_id
        except ValueError:
            await interaction.followup.send("Supply a numeric thread ID.", ephemeral=True)
            return None
        if target_id is None or not 0 < target_id < 2**64:
            await interaction.followup.send("Supply a valid thread ID.", ephemeral=True)
            return None
        return target_id

    async def resolve_thread(
        self, interaction: discord.Interaction, thread_id: str | None, command: str
    ) -> discord.Thread | None:
        """Fetch a thread the caller can manage in this server, replying on error."""
        target_id = await self.parse_thread_id(interaction, thread_id)
        if target_id is None:
            return None
        log.info(
            "/%s %s by user %s on thread %s", self.name, command, interaction.user.id, target_id
        )
        target = await self.bot.fetch_channel(target_id)
        if not isinstance(target, discord.Thread) or not self.can_manage(interaction, target):
            log.info("Thread %s refused: not a thread the user can manage", target_id)
            await interaction.followup.send(
                "Choose a thread in this server where you have View Channel and Manage Threads.",
                ephemeral=True,
            )
            return None
        return target

    async def check_forum(self, interaction: discord.Interaction, forum) -> bool:
        """Allow an enabled forum the caller manages with no batch running, else reply."""
        if forum.id not in self.bot.config.forum_channel_ids or not self.can_manage(
            interaction, forum
        ):
            log.info("Forum %s refused: not enabled or user cannot manage it", forum.id)
            await interaction.followup.send(
                "Choose an enabled forum in this server where you have "
                "View Channel and Manage Threads.",
                ephemeral=True,
            )
            return False
        if forum.id in self.running_batches:
            await interaction.followup.send(
                "A batch is already running for this forum.", ephemeral=True
            )
            return False
        return True

    async def run_workers(
        self,
        threads: list[discord.Thread],
        handle: Callable[[discord.Thread], Awaitable[str | tuple[str, Any]]],
        counts: dict[str, int],
        workers: int,
        should_stop: Callable[[], bool] = lambda: False,
        progress: str | None = None,
        run_id: int | None = None,
    ) -> None:
        """Run handle over threads with parallel workers, roughly in order, counting
        each returned status (or (status, data) pair). With a run ID, each finished
        post is journaled so an interrupted run can resume; failures are not, so a
        resumed run retries them."""
        pending = iter(threads)  # Shared: each worker takes the next unclaimed thread.
        total = sum(counts.values()) + len(threads)

        async def worker() -> None:
            for thread in pending:
                if should_stop():
                    return
                try:
                    result = await handle(thread)
                    status, data = result if isinstance(result, tuple) else (result, None)
                    counts[status] += 1
                    if run_id is not None:
                        self.bot.journal.record_item(run_id, thread.id, status, data)
                except (ClassificationError, EmbeddingError, discord.HTTPException) as exc:
                    counts["failed"] += 1
                    log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)
                done = sum(counts.values())
                if progress and done % 25 == 0:
                    log.info("%s: %d/%d done", progress, done, total)

        async with asyncio.TaskGroup() as group:
            for _ in range(workers):
                group.create_task(worker())

    def make_job(self, kind: str, forum: discord.ForumChannel, options: dict, prior: dict) -> "Job":
        """What a background run of `kind` does with each post; `prior` holds the
        items an interrupted run already finished."""
        raise NotImplementedError

    async def start_full_run(
        self,
        interaction: discord.Interaction | None,
        forum: discord.ForumChannel,
        kind: str,
        options: dict,
        resume: Run | None = None,
        user_id: int | None = None,
    ) -> None:
        """Start (or, after a restart, resume) a journaled background run over every
        post in a forum, newest first."""
        self.running_batches.add(forum.id)
        try:
            active = [t for t in await forum.guild.active_threads() if t.parent_id == forum.id]
            archived = (
                [t async for t in forum.archived_threads(limit=None)]
                if options.get("include_archived", True)
                else []
            )
        except BaseException:
            self.running_batches.discard(forum.id)
            raise
        threads = sorted(active + archived, key=lambda thread: thread.id, reverse=True)
        if resume is None:
            run_id = self.bot.journal.start_run(kind, forum.id, options, user_id)
            prior: dict = {}
        else:
            run_id, prior = resume.id, self.bot.journal.items(resume.id)
        job = self.make_job(kind, forum, options, prior)
        todo = [t for t in threads if t.id not in prior]
        self.stop_requests.discard(forum.id)
        self.full_runs[forum.id] = asyncio.create_task(
            self.run_full(interaction, forum, todo, len(threads), job, run_id, prior, user_id)
        )
        log.info(
            "Full %s run #%s on forum %s %s: %d posts (%d active, %d archived), %d to do",
            job.verb,
            run_id,
            forum.id,
            "resumed" if resume else "started",
            len(threads),
            len(active),
            len(archived),
            len(todo),
        )
        if resume is None:
            text = (
                f"Started {job.verb} {len(threads)} posts ({len(active)} active, "
                f"{len(archived)} archived), newest first, {job.workers} at a time. "
                "New posts are still handled meanwhile. I'll send the summary when done "
                f"(by DM if it takes over 15 minutes). Use `/{self.name} stop` to cancel. "
                f"This is run #{run_id}; `/history undo run:{run_id}` can undo it."
            )
        else:
            text = (
                f"The bot restarted during run #{run_id} ({job.verb} <#{forum.id}>). "
                f"Resuming: {len(prior)} of {len(threads)} posts were already done; "
                f"{len(todo)} to go. I'll send the summary when done."
            )
        await self.report(interaction, text, user_id)

    async def run_full(
        self,
        interaction: discord.Interaction | None,
        forum: discord.ForumChannel,
        threads: list[discord.Thread],
        total: int,
        job: "Job",
        run_id: int,
        prior: dict,
        user_id: int | None,
    ) -> None:
        counts = new_counts(job.statuses)
        for status, _ in prior.values():
            if status in counts:
                counts[status] += 1
        extra = ""
        interrupted = False
        token = current_run.set(run_id)  # Attributes every action to this run.
        try:
            await self.run_workers(
                threads,
                job.handle,
                counts,
                job.workers,
                should_stop=lambda: forum.id in self.stop_requests,
                progress=f"Full {job.verb} run #{run_id} on forum {forum.id}",
                run_id=run_id,
            )
            stopped = forum.id in self.stop_requests
            if job.finish:
                extra = " " + await job.finish(stopped)
        except asyncio.CancelledError:
            # The bot is shutting down: leave the run marked running so it resumes.
            interrupted = True
            raise
        finally:
            current_run.reset(token)
            stopped = forum.id in self.stop_requests
            self.running_batches.discard(forum.id)
            self.stop_requests.discard(forum.id)
            self.full_runs.pop(forum.id, None)
            if not interrupted:
                self.bot.journal.finish_run(run_id, "stopped" if stopped else "finished")
        state = "stopped" if stopped else "finished"
        done = sum(counts.values())
        breakdown = ", ".join(f"{counts[key]} {key}" for key in job.statuses)
        log.info(
            "Full %s run #%s on forum %s %s: %d/%d examined, %s",
            job.verb,
            run_id,
            forum.id,
            state,
            done,
            total,
            breakdown,
        )
        await self.report(
            interaction,
            f"Full {job.verb} run on <#{forum.id}> {state}. "
            f"Examined {done}/{total} posts: {breakdown}.{extra}",
            user_id,
        )

    async def request_stop(self, interaction: discord.Interaction, forum) -> None:
        log.info("/%s stop by user %s on forum %s", self.name, interaction.user.id, forum.id)
        if forum.id not in self.full_runs:
            await interaction.response.send_message(
                "No full run is active for this forum.", ephemeral=True
            )
            return
        self.stop_requests.add(forum.id)
        await interaction.response.send_message(
            "Stopping after the current post; the summary follows.", ephemeral=True
        )

    async def report(
        self,
        interaction: discord.Interaction | None,
        message: str,
        user_id: int | None = None,
    ) -> None:
        """Reply to the command, or DM the user once the interaction has expired or
        when a run resumed after a restart (no interaction)."""
        if len(message) > 2000:  # Discord's message limit.
            message = message[:1990].rsplit("\n", 1)[0] + "\n…"
        if interaction is not None:
            # Interaction follow-ups expire after 15 minutes; fall back to a DM.
            try:
                await interaction.followup.send(message, ephemeral=True)
                return
            except discord.HTTPException:
                pass
            user, user_id = interaction.user, interaction.user.id
        elif user_id is None:
            log.info("No one to notify: %s", message.splitlines()[0])
            return
        else:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                log.warning("Could not find user %s to deliver a summary", user_id)
                return
        try:
            await user.send(message)
        except discord.HTTPException:
            log.warning("Could not deliver the summary to user %s", user_id)

    @contextlib.contextmanager
    def journaled_run(self, kind: str, forum_id: int, options: dict, user_id: int) -> Iterator[int]:
        """Record a single command as a run, so `/history undo run:<id>` can undo it."""
        run_id = self.bot.journal.start_run(kind, forum_id, options, user_id)
        token = current_run.set(run_id)
        status = "failed"
        try:
            yield run_id
            status = "finished"
        finally:
            current_run.reset(token)
            self.bot.journal.finish_run(run_id, status)


@app_commands.guild_only()
# Visible to members with Manage Threads; a server admin can narrow this further in
# Server Settings > Integrations. The user allowlist is enforced regardless.
@app_commands.default_permissions(manage_threads=True)
class TagCommands(ModeratorGroup, name="autotag", description="AI forum tagging for moderators"):
    async def tag(self, thread: discord.Thread, include_archived: bool = False) -> str:
        return (await self.bot.tagger.tag(thread.id, include_archived)).status

    @app_commands.command(name="thread", description="AI-tag this thread or a thread ID")
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_thread(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = await self.resolve_thread(interaction, thread_id, "thread")
        if target is None:
            return
        try:
            # Includes waiting for a free slot when other tagging work is running.
            with self.journaled_run(
                "tag_thread", target.parent_id, {"thread_id": target.id}, interaction.user.id
            ):
                async with asyncio.timeout(60):
                    result = await self.bot.tagger.tag(target.id)
        except TimeoutError:
            log.warning("Thread %s timed out waiting for other tagging work", target.id)
            await interaction.followup.send(
                "Timed out; other tagging work may be queued. Try again shortly.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(result.reason, ephemeral=True)

    @app_commands.command(name="active", description="AI-tag active threads in an enabled forum")
    @app_commands.describe(forum="Enabled forum", limit="Maximum threads to examine (default 20)")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_active(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        limit: app_commands.Range[int, 1, 100] = 20,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info(
            "/autotag active by user %s on forum %s (limit %s)",
            interaction.user.id,
            forum.id,
            limit,
        )
        if not await self.check_forum(interaction, forum):
            return
        self.running_batches.add(forum.id)
        counts = new_counts()
        selected = []
        available = 0
        timed_out = False
        try:
            with self.journaled_run("tag_active", forum.id, {"limit": limit}, interaction.user.id):
                # Discord interaction tokens last 15 minutes; leave time for the summary.
                async with asyncio.timeout(600):
                    threads = await forum.guild.active_threads()
                    eligible = sorted(
                        (thread for thread in threads if thread.parent_id == forum.id),
                        key=lambda thread: thread.id,
                        reverse=True,
                    )
                    available = len(eligible)
                    selected = eligible[:limit]
                    await self.run_workers(
                        selected, self.tag, counts, self.bot.config.tag_concurrency
                    )
        except TimeoutError:
            timed_out = True
        finally:
            self.running_batches.discard(forum.id)
        log.info(
            "Batch on forum %s %s: %d/%d examined, %d updated, %d unchanged, %d skipped, %d failed",
            forum.id,
            "timed out" if timed_out else "finished",
            sum(counts.values()),
            len(selected),
            counts["updated"],
            counts["unchanged"],
            counts["skipped"],
            counts["failed"],
        )
        await interaction.followup.send(
            f"Batch {'stopped at the 10-minute deadline' if timed_out else 'finished'}. "
            f"Examined {sum(counts.values())}/{len(selected)} selected threads "
            f"({available} active in forum): {counts['updated']} updated, "
            f"{counts['unchanged']} unchanged, {counts['skipped']} skipped, "
            f"{counts['failed']} failed. "
            "Newest threads are examined first; archived threads are excluded.",
            ephemeral=True,
        )

    @app_commands.command(
        name="all", description="AI-tag every post in an enabled forum, including archived ones"
    )
    @app_commands.describe(
        forum="Enabled forum", include_archived="Also tag archived posts (default: yes)"
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_all(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        include_archived: bool = True,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info(
            "/autotag all by user %s on forum %s (include archived: %s)",
            interaction.user.id,
            forum.id,
            include_archived,
        )
        if not await self.check_forum(interaction, forum):
            return
        await self.start_full_run(
            interaction,
            forum,
            "tag_all",
            {"include_archived": include_archived},
            user_id=interaction.user.id,
        )

    def make_job(self, kind: str, forum: discord.ForumChannel, options: dict, prior: dict) -> "Job":
        include_archived = options.get("include_archived", True)

        async def handle(thread: discord.Thread) -> str:
            return await self.tag(thread, include_archived)

        return Job("tagging", TAG_STATUSES, self.bot.config.tag_concurrency, handle)

    @app_commands.command(name="stop", description="Stop a running /autotag all for a forum")
    @app_commands.describe(forum="Forum whose full run should stop")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def stop(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        await self.request_stop(interaction, forum)


@app_commands.guild_only()
@app_commands.default_permissions(manage_threads=True)
class DuplicateCommands(
    ModeratorGroup, name="duplicates", description="Duplicate post detection for moderators"
):
    async def finder(self, interaction: discord.Interaction) -> DuplicateFinder | None:
        """The duplicate finder, or None after telling the caller detection is off."""
        if self.bot.duplicates is None:
            await interaction.followup.send(
                "Duplicate detection is off. Set DUPLICATES=on and DUPLICATE_DB_KEY in "
                ".env, then restart the bot.",
                ephemeral=True,
            )
        return self.bot.duplicates

    @app_commands.command(
        name="index", description="Index every post in a forum for duplicate detection"
    )
    @app_commands.describe(
        forum="Enabled forum", include_archived="Also index archived posts (default: yes)"
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def index(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        include_archived: bool = True,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info(
            "/duplicates index by user %s on forum %s (include archived: %s)",
            interaction.user.id,
            forum.id,
            include_archived,
        )
        finder = await self.finder(interaction)
        if finder is None or not await self.check_forum(interaction, forum):
            return
        # Includes locked posts and forums without tags; already indexed posts are
        # skipped, so re-runs only embed new posts. Full runs never reply in posts.
        await self.start_full_run(
            interaction,
            forum,
            "index",
            {"include_archived": include_archived},
            user_id=interaction.user.id,
        )

    @app_commands.command(name="stop", description="Stop a running /duplicates index for a forum")
    @app_commands.describe(forum="Forum whose indexing run should stop")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def stop(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        await self.request_stop(interaction, forum)

    @app_commands.command(
        name="similar", description="Show the posts most similar to this thread or a thread ID"
    )
    @app_commands.describe(
        thread_id="Optional thread ID; defaults to the thread you are in",
        limit="How many posts to list (default 10)",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def similar(
        self,
        interaction: discord.Interaction,
        thread_id: str | None = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        finder = await self.finder(interaction)
        if finder is None:
            return
        target = await self.resolve_thread(interaction, thread_id, "similar")
        if target is None:
            return
        try:
            found = await finder.review(target, limit)
        except (EmbeddingError, ClassificationError) as exc:
            log.warning("Thread %s similarity lookup failed (%s)", target.id, exc)
            await interaction.followup.send(
                "Could not compute similarity; try again shortly.", ephemeral=True
            )
            return
        if not found:
            await interaction.followup.send(
                "No other indexed posts in this forum yet.", ephemeral=True
            )
            return
        floor = self.bot.config.duplicate_threshold
        header = (
            "Most similar posts by embedding score. ✅ = confirmed as a duplicate by the AI "
            "check; a new post like this would get a reply linking those "
            f"(only candidates scoring at least {floor:.0%} are checked)."
        )
        lines = []
        length = len(header)
        for match in sorted(found, key=lambda m: m.score, reverse=True):
            line = (
                f"\n• {post_link(target.guild.id, match)} {match.score:.0%}"
                f"{' ✅' if match.confirmed else ''}"
                f"{' (below floor)' if match.score < floor else ''}"
            )
            if length + len(line) > 1990:  # Discord's 2,000-character message limit.
                break
            lines.append(line)
            length += len(line)
        await interaction.followup.send(header + "".join(lines), ephemeral=True)

    @app_commands.command(
        name="forget", description="Delete a post's duplicate-detection data and never index it"
    )
    @app_commands.describe(thread_id="Thread ID whose stored data should be deleted")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def forget(self, interaction: discord.Interaction, thread_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        finder = await self.finder(interaction)
        if finder is None:
            return
        target_id = await self.parse_thread_id(interaction, thread_id)
        if target_id is None:
            return
        removed = finder.index.exclude(target_id)
        try:
            await finder.links.forget(target_id)  # Remove it from its duplicate group.
        except discord.HTTPException as exc:
            log.warning("Thread %s: updating related notices failed (%s)", target_id, exc.status)
        log.info(
            "/duplicates forget by user %s: thread %s excluded (data %s)",
            interaction.user.id,
            target_id,
            "deleted" if removed else "was not stored",
        )
        await interaction.followup.send(
            f"Deleted stored data for <#{target_id}>; it will not be indexed again."
            if removed
            else f"No data was stored for <#{target_id}>; it will not be indexed in future.",
            ephemeral=True,
        )

    @app_commands.command(
        name="link", description="Find duplicates of this thread or a thread ID and cross-link them"
    )
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def link(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        finder = await self.finder(interaction)
        if finder is None:
            return
        target = await self.resolve_thread(interaction, thread_id, "link")
        if target is None:
            return
        if target.parent_id not in self.bot.config.forum_channel_ids:
            await interaction.followup.send("That post is not in an enabled forum.", ephemeral=True)
            return
        try:
            with self.journaled_run(
                "link", target.parent_id, {"thread_id": target.id}, interaction.user.id
            ):
                async with asyncio.timeout(120):
                    found = await finder.link_post(target)
        except TimeoutError:
            await interaction.followup.send("Timed out; try again shortly.", ephemeral=True)
            return
        except (EmbeddingError, ClassificationError) as exc:
            log.warning("Thread %s link check failed (%s)", target.id, exc)
            await interaction.followup.send(
                "Could not check this post; try again shortly.", ephemeral=True
            )
            return
        if not found:
            await interaction.followup.send(
                "No duplicates confirmed for this post; nothing was posted.", ephemeral=True
            )
            return
        lines = "\n".join(f"• {post_link(target.guild.id, m)}" for m in found)
        where = (
            "Posted a related-posts notice in this post only (DUPLICATE_CROSS_LINK=off)."
            if self.bot.config.duplicate_cross_link == "off"
            else "Related-posts notices were posted or updated in this post and these posts."
        )
        await interaction.followup.send(
            f"Linked with {len(found)} confirmed duplicate(s):\n{lines}\n{where}", ephemeral=True
        )

    @app_commands.command(
        name="link_all",
        description="Find and cross-link duplicates across every indexed post in a forum",
    )
    @app_commands.describe(
        forum="Enabled forum",
        mode="preview: only report the groups found; post: cross-link them in Discord",
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def link_all(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        mode: Literal["preview", "post"],
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info(
            "/duplicates link_all by user %s on forum %s (mode %s)",
            interaction.user.id,
            forum.id,
            mode,
        )
        finder = await self.finder(interaction)
        if finder is None or not await self.check_forum(interaction, forum):
            return
        if mode == "post" and self.bot.config.duplicate_cross_link == "off":
            await interaction.followup.send(
                "Cross-linking is off (DUPLICATE_CROSS_LINK=off); use mode:preview or turn it on.",
                ephemeral=True,
            )
            return
        if not finder.index.candidates({forum.id}, exclude=0):
            await interaction.followup.send(
                "No indexed posts in this forum yet; run `/duplicates index` first.",
                ephemeral=True,
            )
            return
        await self.start_full_run(
            interaction, forum, "link_all", {"mode": mode}, user_id=interaction.user.id
        )

    def make_job(self, kind: str, forum: discord.ForumChannel, options: dict, prior: dict) -> "Job":
        finder = self.bot.duplicates
        if kind == "index":
            return Job(
                "indexing", INDEX_STATUSES, self.bot.config.index_concurrency, finder.index_post
            )
        return self.link_job(finder, forum, options["mode"], prior)

    def link_job(
        self, finder: DuplicateFinder, forum: discord.ForumChannel, mode: str, prior: dict
    ) -> "Job":
        cache: dict = {}  # Candidate text, in memory for this run only.
        titles: dict[int, str] = {}
        # Groups found before a restart are journaled with their run items.
        groups: list[list[int]] = [data for status, data in prior.values() if status == "grouped"]

        async def handle(thread: discord.Thread) -> str | tuple[str, list[int]]:
            if thread.id not in finder.index:
                return "skipped"
            found = await finder.find(thread, cache)
            if not found:
                return "unique"
            titles[thread.id] = thread.name
            titles.update((m.thread_id, m.title) for m in found)
            group = [thread.id, *(m.thread_id for m in found)]
            groups.append(group)
            return "grouped", group

        async def finish(stopped: bool) -> str:
            clusters = merge_groups(groups)
            covered = sum(len(c) for c in clusters)
            if mode == "preview" or stopped:
                head = (
                    "Stopped before posting; nothing was posted or stored."
                    if stopped and mode == "post"
                    else "Preview only; nothing was posted or stored."
                )
                for cluster in clusters:
                    log.info("Duplicate group preview in forum %s: %s", forum.id, cluster)
                shown = "\n".join(
                    "• "
                    + " ↔ ".join(
                        post_link(forum.guild.id, Match(t, titles.get(t, str(t)), 0.0, True))
                        for t in cluster
                    )
                    for cluster in clusters
                )
                return f"{head} Found {len(clusters)} group(s) covering {covered} posts:\n{shown}"
            for group in groups:
                finder.links.relate(group)  # Journaled, so the run can be undone.
            failed = 0
            # Each post's notice is written once, after all groups are known.
            for thread_id in sorted({t for c in clusters for t in c}, reverse=True):
                try:
                    await finder.links.refresh(thread_id)
                except discord.HTTPException as exc:
                    failed += 1
                    log.warning("Thread %s notice update failed (%s)", thread_id, exc.status)
            note = f" {failed} notice update(s) failed; see the log." if failed else ""
            return (
                f"Cross-linked {len(clusters)} group(s) covering {covered} posts; "
                f"each post's related-posts notice was posted or updated once.{note}"
            )

        return Job("linking", LINK_STATUSES, self.bot.config.index_concurrency, handle, finish)


UNDOABLE = ("tags", "relate", "notice_post")


def describe_actions(actions: list[Action]) -> str:
    counts = {kind: sum(1 for a in actions if a.kind == kind) for kind in UNDOABLE}
    parts = [
        f"{counts['tags']} tag change(s)",
        f"{counts['relate']} duplicate link(s)",
        f"{counts['notice_post']} posted related-posts notice(s)",
    ]
    return ", ".join(p for p in parts if not p.startswith("0 ")) or "nothing"


@app_commands.guild_only()
@app_commands.default_permissions(manage_threads=True)
class HistoryCommands(
    ModeratorGroup, name="history", description="Bot action history and undo for moderators"
):
    def __init__(self, bot: ArtCraftBot):
        super().__init__(bot)
        self.undo_tasks: set[asyncio.Task] = set()

    @app_commands.command(name="runs", description="List the bot's recent runs")
    @app_commands.describe(limit="How many runs to list (default 10)")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def runs(
        self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        journal = self.bot.journal
        runs = journal.runs(limit)
        if not runs:
            await interaction.followup.send("No runs recorded yet.", ephemeral=True)
            return
        lines = []
        for run in runs:
            items = ", ".join(f"{n} {s}" for s, n in sorted(journal.item_counts(run.id).items()))
            changes = describe_actions(journal.actions(run_id=run.id))
            who = f" by <@{run.user_id}>" if run.user_id else ""
            lines.append(
                f"**#{run.id}** `{run.kind}` <#{run.forum_id}> · {run.status} · "
                f"{run.started[:16].replace('T', ' ')} UTC{who} · changes: {changes}"
                + (f" · posts: {items}" if items else "")
            )
        await interaction.followup.send(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="thread", description="Show the bot's recent actions on a post")
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def thread(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        target_id = await self.parse_thread_id(interaction, thread_id)
        if target_id is None:
            return
        actions = self.bot.journal.actions(thread_id=target_id, include_undone=True, limit=15)
        if not actions:
            await interaction.followup.send(
                "No bot actions recorded for this post.", ephemeral=True
            )
            return
        names = self.tag_names()
        lines = []
        for a in actions:
            if a.kind == "tags":
                before = ", ".join(names.get(t, str(t)) for t in a.before) or "none"
                after = ", ".join(names.get(t, str(t)) for t in a.after) or "none"
                what = f"tags {before} → {after}"
            elif a.kind == "relate":
                what = f"linked as a duplicate group ({len(a.after)} link(s))"
            else:
                what = a.kind.replace("_", " ").replace("notice", "related-posts notice")
            run = f"run #{a.run_id}" if a.run_id else "new post"
            undone = " (undone)" if a.undone else ""
            lines.append(f"• {a.at[:16].replace('T', ' ')} UTC · {run} · {what}{undone}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    def tag_names(self) -> dict[int, str]:
        names = {}
        for forum_id in self.bot.config.forum_channel_ids:
            forum = self.bot.get_channel(forum_id)
            for tag in getattr(forum, "available_tags", []):
                names[tag.id] = tag.name
        return names

    @app_commands.command(name="undo", description="Undo one run's changes (shows a preview first)")
    @app_commands.describe(
        run="Run number from /history runs", confirm="Set to True to actually undo"
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def undo(self, interaction: discord.Interaction, run: int, confirm: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.bot.journal.run(run) is None:
            await interaction.followup.send(f"There is no run #{run}.", ephemeral=True)
            return
        actions = self.bot.journal.actions(run_id=run)
        await self.undo_actions(interaction, actions, f"run #{run}", confirm)

    @app_commands.command(
        name="undo_recent",
        description="Undo all bot changes from the last N minutes (shows a preview first)",
    )
    @app_commands.describe(
        minutes="How far back to undo (1-1440)", confirm="Set to True to actually undo"
    )
    @app_commands.checks.has_permissions(manage_threads=True)
    async def undo_recent(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 1440],
        confirm: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        since = now() - datetime.timedelta(minutes=minutes)
        actions = self.bot.journal.actions(since=since)
        await self.undo_actions(interaction, actions, f"the last {minutes} minute(s)", confirm)

    async def undo_actions(
        self, interaction: discord.Interaction, actions: list[Action], label: str, confirm: bool
    ) -> None:
        undoable = [a for a in actions if a.kind in UNDOABLE]
        if not undoable:
            await interaction.followup.send(f"Nothing to undo for {label}.", ephemeral=True)
            return
        summary = describe_actions(undoable)
        if not confirm:
            await interaction.followup.send(
                f"Preview: undoing {label} would revert {summary}. Tags someone changed "
                "since are left alone. Run again with `confirm:True` to undo.",
                ephemeral=True,
            )
            return
        log.info("Undo of %s by user %s: %s", label, interaction.user.id, summary)
        await interaction.followup.send(
            f"Undoing {label} ({summary}); I'll report when done.", ephemeral=True
        )

        async def run_undo() -> None:
            result = await self.bot.recovery.undo(undoable, interaction.user.id, label)
            await self.report(interaction, f"Undo of {label} finished: {result}")

        task = asyncio.create_task(run_undo())
        self.undo_tasks.add(task)
        task.add_done_callback(self.undo_tasks.discard)


def merge_groups(groups: list[list[int]]) -> list[list[int]]:
    """Connected components of overlapping groups, each newest post first."""
    parent: dict[int, int] = {}

    def root(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for group in groups:
        for other in group[1:]:
            parent[root(other)] = root(group[0])
    clusters: dict[int, list[int]] = {}
    for node in list(parent):
        clusters.setdefault(root(node), []).append(node)
    return sorted(
        (sorted(c, reverse=True) for c in clusters.values()), key=lambda c: c[0], reverse=True
    )
