import asyncio
import logging

import discord
import httpx
from discord import app_commands

from .classifier import ClassificationError, Classifier
from .config import Config
from .tagging import Tagger

log = logging.getLogger(__name__)


class ArtCraftBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
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
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(TagCommands(self))
        self.tree.on_error = self.command_error

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.http_client.aclose()

    async def on_ready(self) -> None:
        log.info("ArtCraft connected; %d forum(s) configured", len(self.config.forum_channel_ids))

    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.parent_id not in self.config.forum_channel_ids:
            return
        try:
            result = await self.tagger.tag(thread.id)
            log.info("Thread %s: %s", thread.id, result.reason)
        except (ClassificationError, discord.HTTPException) as exc:
            log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        # Do not log exception bodies or event arguments containing Discord content.
        log.error("Discord event %s failed", event_method)

    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.warning("Command failed (%s)", type(error).__name__)
        message = (
            "Command failed. Check bot permissions and configuration, then inspect the thread."
        )
        if isinstance(error, app_commands.CheckFailure):
            message = "This command requires Manage Threads in a server channel."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


@app_commands.guild_only()
@app_commands.default_permissions(manage_threads=True)
class TagCommands(
    app_commands.Group, name="autotag", description="AI forum tagging for moderators"
):
    def __init__(self, bot: ArtCraftBot):
        super().__init__()
        self.bot = bot
        self.running_batches: set[int] = set()

    def can_manage(self, interaction: discord.Interaction, channel) -> bool:
        if interaction.guild_id != channel.guild.id or not isinstance(
            interaction.user, discord.Member
        ):
            return False
        permissions = channel.permissions_for(interaction.user)
        return permissions.view_channel and permissions.manage_threads

    @app_commands.command(
        name="thread", description="Replace all tags on this thread or a thread ID"
    )
    @app_commands.describe(thread_id="Optional thread ID; defaults to the thread you are in")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_thread(self, interaction: discord.Interaction, thread_id: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target_id = int(thread_id) if thread_id is not None else interaction.channel_id
        except ValueError:
            await interaction.followup.send("Supply a numeric thread ID.", ephemeral=True)
            return
        if target_id is None or not 0 < target_id < 2**64:
            await interaction.followup.send("Supply a valid thread ID.", ephemeral=True)
            return
        target = await self.bot.fetch_channel(target_id)
        if not isinstance(target, discord.Thread) or not self.can_manage(interaction, target):
            await interaction.followup.send(
                "Choose a thread in this server where you have View Channel and Manage Threads.",
                ephemeral=True,
            )
            return
        async with asyncio.timeout(60):
            result = await self.bot.tagger.tag(target.id)
        await interaction.followup.send(result.reason, ephemeral=True)

    @app_commands.command(
        name="active", description="Replace all tags on active threads in an enabled forum"
    )
    @app_commands.describe(forum="Enabled forum", limit="Maximum threads to examine (default 20)")
    @app_commands.checks.has_permissions(manage_threads=True)
    async def tag_active(
        self,
        interaction: discord.Interaction,
        forum: discord.ForumChannel,
        limit: app_commands.Range[int, 1, 100] = 20,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if forum.id not in self.bot.config.forum_channel_ids or not self.can_manage(
            interaction, forum
        ):
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
        changed = skipped = failed = completed = 0
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
                for thread in selected:
                    try:
                        result = await self.bot.tagger.tag(thread.id)
                        if result.added or result.removed:
                            changed += 1
                        else:
                            skipped += 1
                    except (ClassificationError, discord.HTTPException) as exc:
                        failed += 1
                        log.warning("Thread %s failed (%s)", thread.id, type(exc).__name__)
                    completed += 1
        except TimeoutError:
            timed_out = True
        finally:
            self.running_batches.discard(forum.id)
        await interaction.followup.send(
            f"Batch {'stopped at the 10-minute deadline' if timed_out else 'finished'}. "
            f"Examined {completed}/{len(selected)} selected threads ({available} active in forum): "
            f"{changed} updated, {skipped} unchanged, {failed} failed. "
            "Newest threads are examined first; archived threads are excluded.",
            ephemeral=True,
        )
