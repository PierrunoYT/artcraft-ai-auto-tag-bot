import dataclasses
from copy import deepcopy
from types import SimpleNamespace

import discord
import pytest

LINK = "https://discord.com/channels/10/"
SIMILAR = [0.95, 0.312, 0.0]


def add_thread(world, thread_id, name, **metadata):
    world.channels[thread_id] = deepcopy(world.channels[200])
    world.channels[thread_id].update(id=str(thread_id), name=name)
    world.channels[thread_id]["thread_metadata"].update(metadata)


async def index(world, finder, thread_id):
    return await finder.index_post(await world.bot.fetch_channel(thread_id))


def notice(world, duplicates, thread_id):
    """Current content of the bot's notice in a thread, or None."""
    message_id = duplicates.index.notice(thread_id)
    return world.messages.get(message_id) if message_id else None


def linked(world, duplicates, thread_id):
    content = notice(world, duplicates, thread_id) or ""
    return {int(part.split(")")[0]) for part in content.split(LINK)[1:]}


async def mail_group(world, duplicates, **metadata):
    """Index two older mail requests (201, 202), then create new post 200."""
    world.channels[200]["name"] = "Please make an email app"
    add_thread(world, 201, "Apple Mail replacement", **metadata)
    add_thread(world, 202, "EmailCraft")
    world.vectors = {
        "Apple Mail replacement": [1.0, 0.0, 0.0],
        "EmailCraft": [0.98, 0.2, 0.0],
        "Please make an email app": SIMILAR,
    }
    await index(world, duplicates, 201)
    await index(world, duplicates, 202)
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))


def cross_link(duplicates, mode):
    duplicates.links.config = dataclasses.replace(duplicates.config, duplicate_cross_link=mode)


async def test_every_post_in_the_group_lists_all_the_others(world, duplicates):
    await mail_group(world, duplicates)
    assert linked(world, duplicates, 200) == {201, 202}
    assert linked(world, duplicates, 201) == {200, 202}
    assert linked(world, duplicates, 202) == {200, 201}
    assert "[Apple Mail replacement]" in notice(world, duplicates, 200)
    assert duplicates.index.related(201) == [202, 200]  # Newest first.


async def test_a_later_duplicate_edits_existing_notices_instead_of_posting(world, duplicates):
    await mail_group(world, duplicates)
    sent_before = len(world.sent)
    add_thread(world, 203, "Mail client with multiple accounts")
    world.vectors["Mail client with multiple accounts"] = [1.0, 0.01, 0.0]
    world.judge = ["201"]  # Confirmed only against the Apple Mail request.
    await world.bot.on_thread_create(await world.bot.fetch_channel(203))
    # One new notice (in 203); existing notices were edited, not reposted.
    assert [channel for channel, _ in world.sent[sent_before:]] == [203]
    assert {channel for channel, _, _ in world.edited} == {200, 201, 202}
    # 203 joined the whole group, not just the post it matched.
    assert linked(world, duplicates, 203) == {200, 201, 202}
    for member, others in {200: {201, 202, 203}, 201: {200, 202, 203}}.items():
        assert linked(world, duplicates, member) == others


async def test_archived_posts_are_reopened_briefly_and_archived_again(world, duplicates):
    await mail_group(world, duplicates, archived=True)
    assert linked(world, duplicates, 201) == {200, 202}
    assert world.channels[201]["thread_metadata"]["archived"] is True


async def test_active_mode_skips_archived_posts(world, duplicates):
    cross_link(duplicates, "active")
    await mail_group(world, duplicates, archived=True)
    assert notice(world, duplicates, 201) is None
    assert linked(world, duplicates, 202) == {200, 201}
    assert linked(world, duplicates, 200) == {201, 202}


async def test_off_mode_only_replies_in_the_new_post(world, duplicates):
    cross_link(duplicates, "off")
    await mail_group(world, duplicates)
    assert {channel for channel, _ in world.sent} == {200}
    assert linked(world, duplicates, 200) == {201, 202}


async def test_locked_posts_are_never_posted_in(world, duplicates):
    await mail_group(world, duplicates, locked=True)
    assert notice(world, duplicates, 201) is None
    assert linked(world, duplicates, 200) == {201, 202}


async def test_deleted_post_is_removed_from_the_others_notices(world, duplicates):
    await mail_group(world, duplicates)
    del world.channels[202]
    await world.bot.on_raw_thread_delete(SimpleNamespace(thread_id=202))
    assert linked(world, duplicates, 200) == {201}
    assert linked(world, duplicates, 201) == {200}
    assert duplicates.index.related(202) == []


async def test_notice_is_deleted_when_no_related_posts_remain(world, duplicates):
    world.judge = ["201"]
    await mail_group(world, duplicates)
    message_id = duplicates.index.notice(201)
    del world.channels[200]
    await world.bot.on_raw_thread_delete(SimpleNamespace(thread_id=200))
    assert (201, message_id) in world.removed_messages
    assert duplicates.index.notice(201) is None


async def test_a_notice_deleted_by_a_moderator_is_not_reposted(world, duplicates):
    await mail_group(world, duplicates)
    world.messages.pop(duplicates.index.notice(201))  # A moderator deleted it.
    add_thread(world, 203, "Mail client with multiple accounts")
    world.vectors["Mail client with multiple accounts"] = [1.0, 0.01, 0.0]
    world.judge = ["201"]
    await world.bot.on_thread_create(await world.bot.fetch_channel(203))
    assert 201 not in [channel for channel, _ in world.sent[3:]]
    assert duplicates.index.notice(201) == 0


async def test_forget_removes_the_post_from_its_group(world, duplicates, interaction):
    await mail_group(world, duplicates)
    group = world.bot.tree.get_command("duplicates")
    await group.forget.callback(group, interaction, "202")
    assert linked(world, duplicates, 200) == {201}
    assert linked(world, duplicates, 201) == {200}
    assert duplicates.index.related(202) == []


@pytest.mark.parametrize("value", ["on", "active", "off"])
def test_cross_link_setting_is_configurable(monkeypatch, value):
    from artcraft.config import Config

    for name, setting in {
        "DISCORD_TOKEN": "t",
        "OPENROUTER_API_KEY": "k",
        "FORUM_CHANNEL_IDS": "100",
        "DUPLICATE_CROSS_LINK": value,
    }.items():
        monkeypatch.setenv(name, setting)
    assert Config.from_env().duplicate_cross_link == value


async def indexed_mail_posts(world, duplicates):
    """Three existing, already indexed mail requests (200, 201, 202); nothing posted."""
    world.channels[200]["name"] = "Please make an email app"
    add_thread(world, 201, "Apple Mail replacement")
    add_thread(world, 202, "EmailCraft")
    world.vectors = {
        "Apple Mail replacement": [1.0, 0.0, 0.0],
        "EmailCraft": [0.98, 0.2, 0.0],
        "Please make an email app": SIMILAR,
    }
    for thread_id in (200, 201, 202):
        await index(world, duplicates, thread_id)
    assert world.sent == []


async def test_link_command_cross_links_one_existing_post(world, duplicates, interaction):
    await indexed_mail_posts(world, duplicates)
    group = world.bot.tree.get_command("duplicates")
    await group.link.callback(group, interaction)  # In thread 200.
    reply = interaction.followup.send.call_args.args[0]
    assert "Linked with 2 confirmed duplicate(s)" in reply
    assert "[Apple Mail replacement]" in reply and "[EmailCraft]" in reply
    assert linked(world, duplicates, 200) == {201, 202}
    assert linked(world, duplicates, 201) == {200, 202}


async def test_link_command_posts_nothing_without_confirmed_duplicates(
    world, duplicates, interaction
):
    await indexed_mail_posts(world, duplicates)
    world.judge = []
    group = world.bot.tree.get_command("duplicates")
    await group.link.callback(group, interaction)
    assert "nothing was posted" in interaction.followup.send.call_args.args[0]
    assert world.sent == []
    assert duplicates.index.related(200) == []


async def run_link_all(world, interaction, mode):
    group = world.bot.tree.get_command("duplicates")
    await group.link_all.callback(group, interaction, world.guild.get_channel(100), mode)
    task = group.full_runs.get(100)
    if task:
        await task
    return interaction.followup.send.call_args.args[0]


async def test_link_all_preview_reports_groups_without_posting_or_storing(
    world, duplicates, interaction
):
    await indexed_mail_posts(world, duplicates)
    summary = await run_link_all(world, interaction, "preview")
    assert "Preview only; nothing was posted or stored." in summary
    assert "Found 1 group(s) covering 3 posts" in summary
    assert "[EmailCraft]" in summary
    assert world.sent == [] and world.edited == []
    assert duplicates.index.related(200) == []


async def test_link_all_post_writes_each_notice_exactly_once(world, duplicates, interaction):
    await indexed_mail_posts(world, duplicates)
    summary = await run_link_all(world, interaction, "post")
    assert "Cross-linked 1 group(s) covering 3 posts" in summary
    assert sorted(channel for channel, _ in world.sent) == [200, 201, 202]
    assert world.edited == []  # Notices are written after all groups are known.
    for thread_id, others in {200: {201, 202}, 201: {200, 202}, 202: {200, 201}}.items():
        assert linked(world, duplicates, thread_id) == others


async def test_link_all_reads_each_candidate_from_discord_once(
    world, duplicates, interaction, monkeypatch
):
    await indexed_mail_posts(world, duplicates)
    reads = []
    real = world.bot.http.get_message

    async def counting(channel_id, message_id):
        reads.append(channel_id)
        return await real(channel_id, message_id)

    monkeypatch.setattr(world.bot.http, "get_message", counting)
    await run_link_all(world, interaction, "preview")
    # Each post: one read as the post being checked, at most one as a candidate.
    assert all(reads.count(t) <= 2 for t in (200, 201, 202))


async def test_link_all_refuses_to_post_when_cross_linking_is_off(world, duplicates, interaction):
    await indexed_mail_posts(world, duplicates)
    world.bot.config = dataclasses.replace(world.bot.config, duplicate_cross_link="off")
    summary = await run_link_all(world, interaction, "post")
    assert "Cross-linking is off" in summary
    assert world.sent == []


async def test_link_all_requires_an_index(world, duplicates, interaction):
    summary = await run_link_all(world, interaction, "preview")
    assert "run `/duplicates index` first" in summary
    assert world.judge_requests == []


async def test_stopped_link_all_posts_nothing(world, duplicates, interaction):
    await indexed_mail_posts(world, duplicates)
    group = world.bot.tree.get_command("duplicates")
    await group.link_all.callback(group, interaction, world.guild.get_channel(100), "post")
    group.stop_requests.add(100)  # Stopped before any post is examined.
    await group.full_runs[100]
    assert (
        "Stopped before posting; nothing was posted or stored."
        in (interaction.followup.send.call_args.args[0])
    )
    assert world.sent == []
    assert duplicates.index.related(200) == []


def test_merge_groups_joins_overlapping_matches():
    from artcraft.bot import merge_groups

    assert merge_groups([[5, 3], [3, 1], [9, 8]]) == [[9, 8], [5, 3, 1]]


def enable_second_forum(world, duplicates, scope="all"):
    """Add forum 101 (like #app-requests) next to forum 100 and enable both."""
    data = deepcopy(world.channels[100])
    data.update(id="101", name="app-requests")
    world.channels[101] = data
    world.guild._add_channel(
        discord.ForumChannel(state=world.bot._connection, guild=world.guild, data=data)
    )
    config = dataclasses.replace(
        duplicates.config, forum_channel_ids=frozenset({100, 101}), duplicate_scope=scope
    )
    world.bot.config = duplicates.config = duplicates.links.config = config


async def cross_forum_posts(world, duplicates, scope):
    """An FL Studio request in each forum; the new one (203) arrives in forum 101."""
    enable_second_forum(world, duplicates, scope)
    add_thread(world, 201, "DAWCraft - FL Studio alternative")  # Forum 100.
    add_thread(world, 203, "BeatsCraft FL Studio Clone")
    world.channels[203]["parent_id"] = "101"
    world.vectors = {
        "DAWCraft - FL Studio alternative": [1.0, 0.0, 0.0],
        "BeatsCraft FL Studio Clone": SIMILAR,
    }
    await index(world, duplicates, 201)
    await world.bot.on_thread_create(await world.bot.fetch_channel(203))


async def test_posts_are_matched_across_forums_by_default(world, duplicates):
    assert duplicates.config.duplicate_scope == "all"
    await cross_forum_posts(world, duplicates, "all")
    assert linked(world, duplicates, 203) == {201}
    assert linked(world, duplicates, 201) == {203}  # Cross-linked into the other forum.


async def test_forum_scope_keeps_matching_within_one_forum(world, duplicates):
    await cross_forum_posts(world, duplicates, "forum")
    assert world.judge_requests == []  # No candidates in forum 101.
    assert world.sent == []
    assert 203 in duplicates.index


async def test_similar_lists_posts_from_both_forums(world, duplicates, interaction):
    await cross_forum_posts(world, duplicates, "all")
    interaction.channel_id = 203
    group = world.bot.tree.get_command("duplicates")
    await group.similar.callback(group, interaction)
    assert "[DAWCraft - FL Studio alternative]" in interaction.followup.send.call_args.args[0]


@pytest.mark.parametrize("value", ["all", "forum"])
def test_scope_setting_is_configurable(monkeypatch, value):
    from artcraft.config import Config

    for name, setting in {
        "DISCORD_TOKEN": "t",
        "OPENROUTER_API_KEY": "k",
        "FORUM_CHANNEL_IDS": "100",
        "DUPLICATE_SCOPE": value,
    }.items():
        monkeypatch.setenv(name, setting)
    assert Config.from_env().duplicate_scope == value


async def test_notice_tells_authors_to_revive_archived_posts(world, duplicates):
    await mail_group(world, duplicates, archived=True)  # 201 is archived.
    content = notice(world, duplicates, 200)
    assert (
        f"[Apple Mail replacement]({LINK}201) (archived – reply there to bring it back)" in content
    )
    assert f"[EmailCraft]({LINK}202)\n" in content or f"[EmailCraft]({LINK}202)" in content
    assert "(archived" not in content.split(f"{LINK}202)")[1].split("\n")[0]
    assert "please continue there instead of starting a new post" in content
    assert "Archived posts come back when you reply in them." in content


async def test_notice_marks_locked_posts_as_not_revivable(world, duplicates):
    await mail_group(world, duplicates, locked=True)  # 201 is locked.
    content = notice(world, duplicates, 200)
    assert f"[Apple Mail replacement]({LINK}201) (locked)" in content
    assert "reply there to bring it back" not in content


async def test_link_all_merges_overlapping_matches_into_one_group(world, duplicates, interaction):
    # 200~201 and 201~202 (but not 200~202): one merged FL Studio group.
    world.channels[200]["name"] = "FL Studio like app next"
    add_thread(world, 201, "BeatsCraft FL Studio Clone")
    add_thread(world, 202, "FL STUDIO FOR LINUX")
    # Similarity 200~201 ≈ 0.70, 201~202 ≈ 0.88, but 200~202 ≈ 0.30 (below the
    # 0.45 floor), so 200 and 202 only end up together through merging.
    world.vectors = {
        "FL Studio like app next": [1.0, 0.0, 0.0],
        "BeatsCraft FL Studio Clone": [0.7, 0.71, 0.0],
        "FL STUDIO FOR LINUX": [0.3, 0.95, 0.0],
    }
    for thread_id in (200, 201, 202):
        await index(world, duplicates, thread_id)
    summary = await run_link_all(world, interaction, "post")
    assert "Cross-linked 1 group(s) covering 3 posts" in summary
    for member, others in {200: {201, 202}, 201: {200, 202}, 202: {200, 201}}.items():
        assert linked(world, duplicates, member) == others


async def test_link_command_joins_an_existing_group(world, duplicates, interaction):
    await indexed_mail_posts(world, duplicates)
    duplicates.links.relate([201, 202])  # An existing group of two.
    world.judge = ["201"]  # Post 200 only matches 201 directly.
    group = world.bot.tree.get_command("duplicates")
    await group.link.callback(group, interaction)
    assert linked(world, duplicates, 200) == {201, 202}
    assert linked(world, duplicates, 202) == {200, 201}


async def test_big_groups_fit_in_one_discord_message(world, duplicates):
    ids = list(range(300, 350))
    for thread_id in ids:
        add_thread(world, thread_id, f"FL Studio request with a fairly long title {thread_id}")
    duplicates.links.relate([200, *ids])
    await duplicates.links.refresh(200)
    content = notice(world, duplicates, 200)
    assert len(content) <= 2000
    shown = len(linked(world, duplicates, 200))
    assert f"…and {50 - shown} more." in content
    assert linked(world, duplicates, 200) <= set(ids[::-1][:shown])  # Newest first.
