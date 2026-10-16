import asyncio
import logging
from collections.abc import Callable

import discord
import httpx
from discord import app_commands

from .classifier import ClassificationError, Classifier
from .config import Config
from .duplicates import DuplicateFinder, DuplicateIndex, Embedder, EmbeddingError
from .tagging import Tagger

log = logging.getLogger(__name__)


def new_counts() -> dict[str, int]:
    return {"updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}


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
        self.tagger = Tagger(
            self,
            Classifier(
                self.http_client, config.openrouter_api_key, config.model, config.max_ai_tags
            ),
            config,
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
                self.tagger.slots,
            )
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(TagCommands(self))
        self.tree.on_error = self.command_error

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
            await super().close()
        finally:
            await self.http_client.aclose()
            if self.duplicates:
                self.duplicates.index.close()

    async def on_ready(self) -> None:
        log.info("ArtCraft connected; %d forum(s) configured", len(self.config.forum_channel_ids))

    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id not in self.config.forum_channel_ids:
            return
        log.info("New post %s in forum %s; tagging", thread.id, thread.parent_id)
        try:
            await self.tagger.tag(thread.id)
        except (ClassificationError, discord.HTTPException) as exc:
            log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)
        if self.duplicates:
            try:
                await self.duplicates.check_new_post(thread)
            except (EmbeddingError, discord.HTTPException) as exc:
                log.warning("Thread %s duplicate check failed (%s)", thread.id, type(exc).__name__)

    def forget_deleted(self, thread_id: int) -> None:
        if self.duplicates and self.duplicates.index.remove(thread_id):
            log.info("Thread %s removed from the duplicate index (deleted on Discord)", thread_id)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        self.forget_deleted(payload.thread_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id == payload.channel_id:  # A forum post's starter message.
            self.forget_deleted(payload.channel_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.channel_id in payload.message_ids:
            self.forget_deleted(payload.channel_id)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not self.duplicates or payload.message_id != payload.channel_id:
            return
        if payload.channel_id not in self.duplicates.index:
            return
        try:
            thread = await self.fetch_channel(payload.channel_id)
            if isinstance(thread, discord.Thread):
                await self.duplicates.index_post(thread, refresh=True)
                log.info("Thread %s re-indexed after its starter was edited", thread.id)
        except (EmbeddingError, discord.HTTPException) as exc:
            log.warning("Thread %s re-index failed (%s)", payload.channel_id, type(exc).__name__)

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


@app_commands.guild_only()
# Visible to members with Manage Threads; a server admin can narrow this further in
# Server Settings > Integrations. The user allowlist below is enforced regardless.
@app_commands.default_permissions(manage_threads=True)
class TagCommands(
    app_commands.Group, name="autotag", description="AI forum tagging for moderators"
):
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
        log.info("/autotag %s by user %s on thread %s", command, interaction.user.id, target_id)
        target = await self.bot.fetch_channel(target_id)
        if not isinstance(target, discord.Thread) or not self.can_manage(interaction, target):
            log.info("Thread %s refused: not a thread the user can manage", target_id)
            await interaction.followup.send(
                "Choose a thread in this server where you have View Channel and Manage Threads.",
                ephemeral=True,
            )
            return None
        return target

    @app_commands.command(name="thread", description="AI-tag this thread or a thread ID")
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_thread(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = await self.resolve_thread(interaction, thread_id, "thread")
        if target is None:
            return
        try:
            # Includes waiting for other tagging work, which runs one thread at a time.
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
        if forum.id not in self.bot.config.forum_channel_ids or not self.can_manage(
            interaction, forum
        ):
            log.info("Forum %s refused: not enabled or user cannot manage it", forum.id)
            await interaction.followup.send(
                "Choose an enabled forum in this server where you have "
                "View Channel and Manage Threads.",
                ephemeral=True,
            )
            return
        if forum.id in self.running_batches:
            await interaction.followup.send(
                "A batch is already running for this forum.", ephemeral=True
            )
            return
        self.running_batches.add(forum.id)
        counts = new_counts()
        selected = []
        available = 0
        timed_out = False
        try:
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
                await self.tag_threads(selected, counts)
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
        if forum.id not in self.bot.config.forum_channel_ids or not self.can_manage(
            interaction, forum
        ):
            log.info("Forum %s refused: not enabled or user cannot manage it", forum.id)
            await interaction.followup.send(
                "Choose an enabled forum in this server where you have "
                "View Channel and Manage Threads.",
                ephemeral=True,
            )
            return
        if forum.id in self.running_batches:
            await interaction.followup.send(
                "A batch is already running for this forum.", ephemeral=True
            )
            return
        self.running_batches.add(forum.id)
        try:
            active = [t for t in await forum.guild.active_threads() if t.parent_id == forum.id]
            archived = (
                [t async for t in forum.archived_threads(limit=None)] if include_archived else []
            )
        except BaseException:
            self.running_batches.discard(forum.id)
            raise
        threads = sorted(active + archived, key=lambda thread: thread.id, reverse=True)
        self.stop_requests.discard(forum.id)
        self.full_runs[forum.id] = asyncio.create_task(
            self.run_all(interaction, forum, threads, include_archived)
        )
        log.info(
            "Full run on forum %s started: %d posts (%d active, %d archived)",
            forum.id,
            len(threads),
            len(active),
            len(archived),
        )
        await interaction.followup.send(
            f"Started tagging {len(threads)} posts ({len(active)} active, "
            f"{len(archived)} archived), newest first, "
            f"{self.bot.config.tag_concurrency} at a time. New posts are still "
            "tagged meanwhile. I'll send the summary when done (by DM if it takes over "
            "15 minutes). Use `/autotag stop` to cancel.",
            ephemeral=True,
        )

    async def run_all(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        threads: list[discord.Thread],
        include_archived: bool,
    ) -> None:
        counts = new_counts()
        indexed = 0
        try:
            indexed = await self.tag_threads(
                threads,
                counts,
                include_archived,
                should_stop=lambda: forum.id in self.stop_requests,
                progress=f"Full run on forum {forum.id}",
            )
        finally:
            stopped = forum.id in self.stop_requests
            self.running_batches.discard(forum.id)
            self.stop_requests.discard(forum.id)
            self.full_runs.pop(forum.id, None)
        completed, failed = sum(counts.values()), counts["failed"]
        summary = (
            f"Full run on <#{forum.id}> {'stopped' if stopped else 'finished'}. "
            f"Examined {completed}/{len(threads)} posts: {counts['updated']} updated, "
            f"{counts['unchanged']} unchanged, {counts['skipped']} skipped, {failed} failed."
        )
        if self.bot.duplicates:
            summary += f" {indexed} newly indexed for duplicate detection."
        log.info(
            "Full run on forum %s %s: %d/%d examined, %d updated, %d unchanged, "
            "%d skipped, %d failed, %d indexed",
            forum.id,
            "stopped" if stopped else "finished",
            completed,
            len(threads),
            counts["updated"],
            counts["unchanged"],
            counts["skipped"],
            failed,
            indexed,
        )
        await self.report(interaction, summary)

    async def tag_threads(
        self,
        threads: list[discord.Thread],
        counts: dict[str, int],
        include_archived: bool = False,
        should_stop: Callable[[], bool] = lambda: False,
        progress: str | None = None,
    ) -> int:
        """Tag (and index, if enabled) threads with parallel workers, roughly newest
        first, updating counts. Returns how many posts were added to the duplicate index."""
        pending = iter(threads)  # Shared: each worker takes the next unclaimed thread.
        duplicates = self.bot.duplicates
        indexed = 0

        async def worker() -> None:
            nonlocal indexed
            for thread in pending:
                if should_stop():
                    return
                try:
                    result = await self.bot.tagger.tag(thread.id, include_archived)
                    counts[result.status] += 1
                except (ClassificationError, discord.HTTPException) as exc:
                    counts["failed"] += 1
                    log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)
                if duplicates:
                    # Indexes locked posts and forums without tags too; already
                    # indexed posts are skipped, so re-runs cost nothing extra.
                    try:
                        indexed += await duplicates.index_post(thread)
                    except (EmbeddingError, discord.HTTPException) as exc:
                        log.warning("Thread %s indexing failed (%s)", thread.id, type(exc).__name__)
                done = sum(counts.values())
                if progress and done % 25 == 0:
                    log.info("%s: %d/%d done", progress, done, len(threads))

        async with asyncio.TaskGroup() as group:
            for _ in range(self.bot.config.tag_concurrency):
                group.create_task(worker())
        return indexed

    async def report(self, interaction: discord.Interaction, message: str) -> None:
        # Interaction follow-ups expire after 15 minutes; fall back to a DM.
        try:
            await interaction.followup.send(message, ephemeral=True)
            return
        except discord.HTTPException:
            pass
        try:
            await interaction.user.send(message)
        except discord.HTTPException:
            log.warning("Could not deliver the summary to user %s", interaction.user.id)

    @app_commands.command(name="stop", description="Stop a running /autotag all for a forum")
    @app_commands.describe(forum="Forum whose full run should stop")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def stop(self, interaction: discord.Interaction, forum: discord.ForumChannel):
        log.info("/autotag stop by user %s on forum %s", interaction.user.id, forum.id)
        if forum.id not in self.full_runs:
            await interaction.response.send_message(
                "No full run is active for this forum.", ephemeral=True
            )
            return
        self.stop_requests.add(forum.id)
        await interaction.response.send_message(
            "Stopping after the current post; the summary follows.", ephemeral=True
        )

    @app_commands.command(
        name="similar", description="Show the posts most similar to this thread or a thread ID"
    )
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def similar(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.bot.duplicates:
            await interaction.followup.send("Duplicate detection is off.", ephemeral=True)
            return
        target = await self.resolve_thread(interaction, thread_id, "similar")
        if target is None:
            return
        try:
            found = await self.bot.duplicates.similar_to(target)
        except EmbeddingError as exc:
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
        threshold = self.bot.config.duplicate_threshold
        lines = "\n".join(
            f"• <#{t}> {score:.0%}{' ✅' if score >= threshold else ''}" for t, score in found
        )
        await interaction.followup.send(
            f"Most similar indexed posts (✅ = at or above the {threshold:.0%} reply "
            f"threshold):\n{lines}",
            ephemeral=True,
        )

    @app_commands.command(
        name="forget", description="Delete a post's duplicate-detection data and never index it"
    )
    @app_commands.describe(thread_id="Thread ID whose stored data should be deleted")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def forget(self, interaction: discord.Interaction, thread_id: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.bot.duplicates:
            await interaction.followup.send("Duplicate detection is off.", ephemeral=True)
            return
        target_id = await self.parse_thread_id(interaction, thread_id)
        if target_id is None:
            return
        removed = self.bot.duplicates.index.exclude(target_id)
        log.info(
            "/autotag forget by user %s: thread %s excluded (data %s)",
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
