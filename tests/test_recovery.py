import asyncio
import contextlib
import datetime
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from artcraft.journal import now, stamp

SIMILAR = [0.95, 0.312, 0.0]


def add_thread(world, thread_id, name="Another painting", **metadata):
    world.channels[thread_id] = deepcopy(world.channels[200])
    world.channels[thread_id].update(id=str(thread_id), name=name)
    world.channels[thread_id]["thread_metadata"].update(metadata)


def message(interaction):
    return interaction.followup.send.call_args.args[0]


async def tag_thread(world, interaction):
    group = world.bot.tree.get_command("autotag")
    await group.tag_thread.callback(group, interaction)
    return world.bot.journal.runs(1)[0]


async def test_tag_changes_are_journaled_with_their_run(world, interaction):
    run = await tag_thread(world, interaction)
    assert (run.kind, run.status, run.user_id) == ("tag_thread", "finished", 2)
    [action] = world.bot.journal.actions(run_id=run.id)
    assert (action.kind, action.thread_id) == ("tags", 200)
    assert (action.before, action.after) == ([14], [14, 11, 12])


async def test_undo_previews_first_then_restores_tags(world, interaction):
    run = await tag_thread(world, interaction)
    group = world.bot.tree.get_command("history")
    await group.undo.callback(group, interaction, run.id)
    assert "Preview: undoing run" in message(interaction)
    assert "1 tag change(s)" in message(interaction)
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]  # Unchanged.

    await group.undo.callback(group, interaction, run.id, True)
    await asyncio.gather(*group.undo_tasks)
    assert world.channels[200]["applied_tags"] == ["14"]
    assert "1 change(s) undone" in message(interaction)
    await group.undo.callback(group, interaction, run.id)
    assert "Nothing to undo" in message(interaction)


async def test_undo_leaves_tags_alone_if_someone_changed_them_since(world, interaction):
    run = await tag_thread(world, interaction)
    world.channels[200]["applied_tags"] = ["13"]  # A moderator changed them.
    result = await world.bot.recovery.undo(
        world.bot.journal.actions(run_id=run.id), 2, f"run #{run.id}"
    )
    assert "1 left alone" in result
    assert world.channels[200]["applied_tags"] == ["13"]


async def test_undo_recent_covers_new_post_tagging(world, interaction):
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    group = world.bot.tree.get_command("history")
    await group.undo_recent.callback(group, interaction, 5, True)
    await asyncio.gather(*group.undo_tasks)
    assert world.channels[200]["applied_tags"] == ["14"]


async def test_undo_removes_duplicate_links_and_their_notices(world, duplicates, interaction):
    add_thread(world, 201, "Export loses gradients")
    world.vectors = {"Export loses gradients": [1.0, 0.0, 0.0], "My digital painting": SIMILAR}
    await duplicates.index_post(await world.bot.fetch_channel(201))
    group = world.bot.tree.get_command("duplicates")
    await group.link.callback(group, interaction)
    run = world.bot.journal.runs(1)[0]
    assert run.kind == "link" and duplicates.index.related(200) == [201]
    notices = {duplicates.index.notice(200), duplicates.index.notice(201)}

    await world.bot.recovery.undo(world.bot.journal.actions(run_id=run.id), 2, "test")
    assert duplicates.index.related(200) == duplicates.index.related(201) == []
    assert {m for _, m in world.removed_messages} == notices


async def test_interrupted_full_run_resumes_where_it_stopped(world, interaction, monkeypatch):
    add_thread(world, 201)
    journal = world.bot.journal
    run_id = journal.start_run("tag_all", 100, {"include_archived": True}, 2)
    journal.record_item(run_id, 201, "updated")  # Finished before the restart.
    dm = SimpleNamespace(send=AsyncMock())
    monkeypatch.setattr(world.bot, "fetch_user", AsyncMock(return_value=dm))

    await world.bot.recovery.resume_runs()
    group = world.bot.tree.get_command("autotag")
    await group.full_runs[100]
    assert len(world.requests) == 1  # Only post 200 was classified.
    assert world.channels[201]["applied_tags"] == ["14"]  # Not touched again.
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert journal.run(run_id).status == "finished"
    texts = [call.args[0] for call in dm.send.call_args_list]
    assert "Resuming: 1 of 2 posts were already done" in texts[0]
    assert "Examined 2/2 posts: 2 updated" in texts[-1]


async def test_runs_that_cannot_resume_are_marked_interrupted(world):
    journal = world.bot.journal
    run_id = journal.start_run("tag_thread", 100, {"thread_id": 200}, 2)
    await world.bot.recovery.resume_runs()
    assert journal.run(run_id).status == "interrupted"


async def test_a_run_cancelled_by_shutdown_stays_resumable(world, interaction):
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    task = group.full_runs[100]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert world.bot.journal.runs(1)[0].status == "running"


def new_post_id(minutes_ago=0):
    moment = now() - datetime.timedelta(minutes=minutes_ago)
    return discord.utils.time_snowflake(moment)


async def test_catch_up_handles_posts_created_while_offline_once(world):
    missed = new_post_id(minutes_ago=5)
    add_thread(world, missed, "Posted while the bot was offline")
    last_seen = stamp(now() - datetime.timedelta(hours=1))
    await world.bot.recovery.catch_up(last_seen)
    assert world.channels[missed]["applied_tags"] == ["14", "11", "12"]
    assert world.channels[200]["applied_tags"] == ["14"]  # Old post: not caught up.
    assert world.bot.journal.is_handled(missed)
    await world.bot.recovery.catch_up(last_seen)
    assert len(world.requests) == 1  # Not handled twice.


async def test_no_catch_up_on_first_start(world):
    add_thread(world, new_post_id(), "Brand new")
    await world.bot.recovery.catch_up(None)
    assert world.requests == []


async def test_new_posts_are_marked_handled(world):
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert world.bot.journal.is_handled(200)


async def test_old_journal_entries_are_purged(world):
    journal = world.bot.journal
    journal.record("tags", 200, before=[], after=[11])
    old = stamp(now() - datetime.timedelta(days=100))
    journal.db.execute("UPDATE actions SET at = ?", (old,))
    journal.db.commit()
    assert journal.purge(90) == 1
    assert journal.actions(include_undone=True) == []


async def test_history_lists_runs_and_thread_actions(world, interaction):
    run = await tag_thread(world, interaction)
    group = world.bot.tree.get_command("history")
    await group.runs.callback(group, interaction)
    assert f"**#{run.id}** `tag_thread`" in message(interaction)
    assert "1 tag change(s)" in message(interaction)
    await group.thread.callback(group, interaction)
    assert "tags Approved → Approved, Painting, Digital" in message(interaction)


async def test_history_commands_are_moderator_only(world, interaction):
    group = world.bot.tree.get_command("history")
    assert {c.name for c in group.commands} == {
        "runs",
        "thread",
        "undo",
        "undo_recent",
        "undo_thread",
    }
    interaction.user = world.guild.get_member(3)
    assert not await group.interaction_check(interaction)
    for command in group.commands:
        assert command.checks


@pytest.mark.parametrize(
    ("name", "value"),
    [("STATE_DB_PATH", " "), ("ACTION_RETENTION_DAYS", "0"), ("CATCH_UP", "maybe")],
)
def test_invalid_recovery_settings_fail(monkeypatch, name, value):
    from artcraft.config import Config

    for key, setting in {
        "DISCORD_TOKEN": "t",
        "OPENROUTER_API_KEY": "k",
        "FORUM_CHANNEL_IDS": "100",
        name: value,
    }.items():
        monkeypatch.setenv(key, setting)
    with pytest.raises(ValueError, match=name):
        Config.from_env()


async def linked_group(world, duplicates, **metadata):
    """New post 200 is tagged and cross-linked with older posts 201 and 202."""
    world.channels[200]["name"] = "Please make an email app"
    add_thread(world, 201, "Apple Mail replacement", **metadata)
    add_thread(world, 202, "EmailCraft")
    world.vectors = {
        "Apple Mail replacement": [1.0, 0.0, 0.0],
        "EmailCraft": [0.98, 0.2, 0.0],
        "Please make an email app": SIMILAR,
    }
    for thread_id in (201, 202):
        await duplicates.index_post(await world.bot.fetch_channel(thread_id))
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert {channel for channel, _ in world.sent} == {200, 201, 202}


def bot_messages_in(world, thread_id):
    return [m for m, c in world.message_channels.items() if c == thread_id and m in world.messages]


async def undo_thread(world, interaction, thread_id=None, confirm=True):
    group = world.bot.tree.get_command("history")
    await group.undo_thread.callback(
        group, interaction, None if thread_id is None else str(thread_id), confirm
    )
    await asyncio.gather(*group.undo_tasks)
    return message(interaction)


async def test_undo_thread_previews_without_changing_anything(world, duplicates, interaction):
    await linked_group(world, duplicates)
    text = await undo_thread(world, interaction, confirm=False)
    assert "Preview for <#200>" in text
    assert "unlink it from 2 related post(s)" in text
    assert "in 3 post(s)" in text
    assert world.removed_messages == []
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]


async def test_undo_thread_removes_the_whole_group_the_way_it_was_posted(
    world, duplicates, interaction
):
    await linked_group(world, duplicates)
    text = await undo_thread(world, interaction)  # Run inside post 200.
    for thread_id in (200, 201, 202):
        assert bot_messages_in(world, thread_id) == []
        assert duplicates.index.related(thread_id) == []
        assert duplicates.index.notice(thread_id) is None
    assert len(world.removed_messages) == 3  # One notice per post; starters untouched.
    assert world.channels[200]["applied_tags"] == ["14"]  # Tags restored too.
    assert "deleted 3 bot message(s) across 3 post(s)" in text
    assert "1 tag change(s) restored" in text


async def test_undo_thread_works_from_any_post_in_the_group(world, duplicates, interaction):
    await linked_group(world, duplicates)
    await undo_thread(world, interaction, thread_id=202)
    assert all(bot_messages_in(world, t) == [] for t in (200, 201, 202))
    assert duplicates.index.related(200) == []


async def test_undo_thread_reopens_archived_posts_briefly(world, duplicates, interaction):
    await linked_group(world, duplicates, archived=True)
    await undo_thread(world, interaction)
    assert bot_messages_in(world, 201) == []
    assert world.channels[201]["thread_metadata"]["archived"] is True


async def test_member_linked_elsewhere_keeps_an_updated_notice(world, duplicates, interaction):
    await linked_group(world, duplicates)
    duplicates.index.relate([201, 205])  # 201 is also in an unrelated group.
    add_thread(world, 205, "Mail merge in GridCraft")
    await undo_thread(world, interaction)
    remaining = world.messages[duplicates.index.notice(201)]
    assert "Mail merge in GridCraft" in remaining
    assert "Please make an email app" not in remaining
    assert bot_messages_in(world, 200) == bot_messages_in(world, 202) == []


async def test_undo_thread_deletes_unjournaled_bot_posts_without_duplicates(world, interaction):
    world.messages[777] = "🔁 Possibly related posts: (from an older bot version)"
    world.message_channels[777] = 200
    text = await undo_thread(world, interaction)
    assert world.removed_messages == [(200, 777)]
    assert "deleted 1 bot message(s) across 1 post(s)" in text
