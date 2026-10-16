import asyncio
import logging
from copy import deepcopy
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands


async def test_commands_are_hidden_and_restricted_to_allowed_users(world, interaction):
    group = world.bot.tree.get_command("autotag")
    assert group.guild_only
    # Only members with Manage Threads see the commands; the allowlist decides who runs them.
    assert group.default_permissions == discord.Permissions(manage_threads=True)
    assert await group.interaction_check(interaction)
    interaction.user = world.guild.get_member(3)
    assert not await group.interaction_check(interaction)


async def test_disallowed_user_gets_ephemeral_refusal(world, interaction):
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = lambda: False
    await world.bot.command_error(interaction, app_commands.CheckFailure())
    assert "not allowed" in interaction.response.send_message.call_args.args[0]
    assert interaction.response.send_message.call_args.kwargs["ephemeral"]


async def test_commands_require_manage_threads_at_runtime(world, interaction):
    group = world.bot.tree.get_command("autotag")
    for command in group.commands:
        assert command.checks
        interaction.permissions = discord.Permissions.none()
        with pytest.raises(app_commands.MissingPermissions):
            await discord.utils.async_all(check(interaction) for check in command.checks)
        interaction.permissions = discord.Permissions(manage_threads=True)
        assert await discord.utils.async_all(check(interaction) for check in command.checks)


@pytest.mark.parametrize("denial", ["target-permissions", "cross-guild"])
async def test_single_thread_checks_target_permissions(world, interaction, denial):
    group = world.bot.tree.get_command("autotag")
    if denial == "target-permissions":
        interaction.user = world.guild.get_member(3)
    else:
        interaction.guild_id = 999
    await group.tag_thread.callback(group, interaction, "200")
    assert world.requests == []
    assert world.writes == []
    assert "Choose a thread" in interaction.followup.send.call_args.args[0]
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_single_thread_defaults_to_current_thread(world, interaction):
    group = world.bot.tree.get_command("autotag")
    await group.tag_thread.callback(group, interaction)
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert "Retagged: +Painting, +Digital." in interaction.followup.send.call_args.args[0]


async def test_single_thread_reports_timeout_while_other_work_holds_the_lock(
    world, interaction, monkeypatch
):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: real_timeout(0.01))
    group = world.bot.tree.get_command("autotag")
    async with world.bot.tagger.lock:
        await group.tag_thread.callback(group, interaction)
    assert "Timed out" in interaction.followup.send.call_args.args[0]
    assert world.writes == []


async def test_batch_filters_forum_limits_newest_and_continues_after_failure(world, interaction):
    for thread_id in (201, 202, 203, 204):
        world.channels[thread_id] = deepcopy(world.channels[200])
        world.channels[thread_id]["id"] = str(thread_id)
    world.channels[204]["parent_id"] = "101"
    world.channels[202]["thread_metadata"]["locked"] = True
    world.channels[203]["name"] = "Fail this classification"

    # The first selected thread fails; later ones must still be processed.
    def selective_failure():
        title = world.requests[-1]["messages"][1]["content"]
        world.status = 503 if "Fail this classification" in title else 200

    world.after_inference = selective_failure
    group = world.bot.tree.get_command("autotag")
    await group.tag_active.callback(group, interaction, world.guild.get_channel(100), 3)
    assert world.channels[201]["applied_tags"] == ["14", "11", "12"]
    for thread_id in (200, 202, 203, 204):
        assert world.channels[thread_id]["applied_tags"] == ["14"]
    summary = interaction.followup.send.call_args.args[0]
    assert "Examined 3/3" in summary
    assert "1 updated, 0 unchanged, 1 skipped, 1 failed" in summary
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_batch_counts_removal_only_as_updated(world, interaction, replace_mode):
    world.channels[200]["applied_tags"] = ["11", "12", "13", "14"]
    group = world.bot.tree.get_command("autotag")
    await group.tag_active.callback(group, interaction, world.guild.get_channel(100))
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert (
        "1 updated, 0 unchanged, 0 skipped, 0 failed" in interaction.followup.send.call_args.args[0]
    )


async def test_command_use_and_refusals_are_logged_with_user_ids(world, interaction, caplog):
    group = world.bot.tree.get_command("autotag")
    with caplog.at_level(logging.INFO):
        await group.tag_thread.callback(group, interaction)
        await group.tag_active.callback(group, interaction, world.guild.get_channel(100))
        interaction.command = group.tag_thread
        await world.bot.command_error(interaction, app_commands.CheckFailure())
    assert "/autotag thread by user 2 on thread 200" in caplog.text
    assert "/autotag active by user 2 on forum 100 (limit 20)" in caplog.text
    assert "Batch on forum 100 finished: 1/1 examined, 0 updated, 1 unchanged" in caplog.text
    assert "/autotag thread by user 2 refused (CheckFailure)" in caplog.text
