import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands


def test_tagging_and_duplicate_commands_are_separate_groups(world):
    autotag = {c.name for c in world.bot.tree.get_command("autotag").commands}
    duplicates = {c.name for c in world.bot.tree.get_command("duplicates").commands}
    assert autotag == {"thread", "active", "all", "stop"}
    assert duplicates == {"index", "stop", "similar", "forget"}


@pytest.mark.parametrize("name", ["autotag", "duplicates"])
async def test_commands_are_hidden_and_restricted_to_allowed_users(world, interaction, name):
    group = world.bot.tree.get_command(name)
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


@pytest.mark.parametrize("name", ["autotag", "duplicates"])
async def test_commands_require_manage_threads_at_runtime(world, interaction, name):
    group = world.bot.tree.get_command(name)
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
    slots = world.bot.tagger.slots
    for _ in range(world.bot.config.tag_concurrency):  # Other work fills every slot.
        await slots.acquire()
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
    world.fail_title = "Fail this classification"
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


def add_threads(world, archived=(), other_forum=()):
    for thread_id in (201, 202, 203, 204):
        world.channels[thread_id] = deepcopy(world.channels[200])
        world.channels[thread_id]["id"] = str(thread_id)
        world.channels[thread_id]["thread_metadata"]["archived"] = thread_id in archived
        if thread_id in other_forum:
            world.channels[thread_id]["parent_id"] = "101"


async def test_all_tags_every_post_including_archived_in_background(world, interaction):
    add_threads(world, archived=(202, 203, 204), other_forum=(204,))
    world.channels[201]["thread_metadata"]["locked"] = True
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    assert (
        "Started tagging 4 posts (2 active, 2 archived)"
        in (interaction.followup.send.call_args.args[0])
    )
    await group.full_runs[100]
    for thread_id in (200, 202, 203):
        assert world.channels[thread_id]["applied_tags"] == ["14", "11", "12"]
    for thread_id in (202, 203):
        assert world.channels[thread_id]["thread_metadata"]["archived"] is True
    assert world.channels[201]["applied_tags"] == ["14"]  # Locked.
    assert world.channels[204]["applied_tags"] == ["14"]  # Other forum.
    summary = interaction.followup.send.call_args.args[0]
    assert "finished. Examined 4/4 posts: 3 updated, 0 unchanged, 1 skipped, 0 failed" in summary
    assert 100 not in group.full_runs and 100 not in group.running_batches


async def test_full_run_tags_posts_in_parallel_up_to_the_concurrency_limit(world, interaction):
    add_threads(world)
    for thread_id in range(205, 215):
        world.channels[thread_id] = deepcopy(world.channels[200])
        world.channels[thread_id]["id"] = str(thread_id)
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    await group.full_runs[100]
    assert world.max_in_flight == world.bot.config.tag_concurrency == 4
    assert len(world.requests) == 15
    assert "Examined 15/15 posts: 15 updated" in interaction.followup.send.call_args.args[0]


async def test_all_can_exclude_archived_posts(world, interaction):
    add_threads(world, archived=(202,))
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100), False)
    await group.full_runs[100]
    assert world.channels[202]["applied_tags"] == ["14"]
    assert "Examined 4/4 posts" in interaction.followup.send.call_args.args[0]


async def test_stop_ends_a_full_run_before_the_next_post(world, interaction):
    add_threads(world)
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    task = group.full_runs[100]
    interaction.response.send_message = AsyncMock()
    await group.stop.callback(group, interaction, world.guild.get_channel(100))
    assert "Stopping" in interaction.response.send_message.call_args.args[0]
    await task
    assert world.writes == []
    assert "stopped. Examined 0/5 posts" in interaction.followup.send.call_args.args[0]
    await group.stop.callback(group, interaction, world.guild.get_channel(100))
    assert "No full run" in interaction.response.send_message.call_args.args[0]


async def test_summary_falls_back_to_dm_after_the_interaction_expires(world, interaction):
    expired = discord.HTTPException(SimpleNamespace(status=401, reason="Unauthorized"), "")
    interaction.followup.send = AsyncMock(side_effect=expired)
    interaction.user = SimpleNamespace(id=2, send=AsyncMock())
    group = world.bot.tree.get_command("autotag")
    await group.report(interaction, "Full run finished.")
    interaction.user.send.assert_awaited_once_with("Full run finished.")
