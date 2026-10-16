import asyncio
import json
import logging

import pytest

from artcraft.classifier import ClassificationError


async def test_new_thread_adds_tags_and_enforces_zdr(world):
    thread = await world.bot.fetch_channel(200)
    await world.bot.on_thread_create(thread)
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    payload = world.requests[0]
    assert payload["model"] == "openai/gpt-6-luna"
    assert payload["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert payload["plugins"] == []
    prompt = json.loads(payload["messages"][1]["content"])
    # Moderator-only tag 14 is never offered to the model.
    assert prompt == {
        "title": "My digital painting",
        "body": "A digital landscape, feedback welcome.",
        "available_tags": {"11": "Painting", "12": "Digital", "13": "Critique"},
    }


async def test_add_mode_keeps_author_tags_within_discord_limit(world):
    # 91-93 are manually applied IDs missing from the cached forum.
    world.channels[200]["applied_tags"] = ["13", "91", "92", "93"]
    result = await world.bot.tagger.tag(200)
    assert (result.status, result.added, result.removed) == ("updated", 1, 0)
    assert world.channels[200]["applied_tags"] == ["13", "91", "92", "93", "11"]


async def test_add_mode_never_writes_when_thread_is_full(world):
    world.channels[200]["applied_tags"] = ["13", "14", "91", "92", "93"]
    result = await world.bot.tagger.tag(200)
    assert result.status == "unchanged"
    assert world.writes == []


async def test_replace_mode_retags_including_manual_edits_during_inference(world, replace_mode):
    world.channels[200]["applied_tags"] = ["11", "12", "13", "14", "91"]

    def manual_edit():
        world.channels[200]["applied_tags"] = ["14", "91", "92", "93"]

    world.after_inference = manual_edit
    result = await world.bot.tagger.tag(200)
    assert (result.status, result.added, result.removed) == ("updated", 2, 3)
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]


async def test_replace_mode_keeps_moderator_only_tags(world, replace_mode):
    world.channels[200]["applied_tags"] = ["11", "12", "14"]
    world.suggestions = ["13"]
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (1, 2)
    assert world.channels[200]["applied_tags"] == ["14", "13"]


@pytest.mark.parametrize("change", ["deleted", "moderated"])
async def test_candidate_changed_during_inference_skips_without_editing(world, change):
    def edit_forum():
        world.channels[200]["applied_tags"] = []
        tags = world.channels[100]["available_tags"]
        if change == "deleted":
            tags[:] = [tag for tag in tags if tag["id"] != "11"]
        else:
            tags[0]["moderated"] = True

    world.after_inference = edit_forum
    result = await world.bot.tagger.tag(200)
    assert result.status == "skipped"
    assert world.channels[200]["applied_tags"] == []
    assert world.writes == []


@pytest.mark.parametrize("state", ["archived", "locked", "other-forum"])
async def test_ineligible_threads_never_send_content_or_write(world, state):
    if state in ("archived", "locked"):
        world.channels[200]["thread_metadata"][state] = True
    else:
        world.channels[200]["parent_id"] = "101"
    result = await world.bot.tagger.tag(200)
    assert result.status == "skipped"
    assert world.requests == []
    assert world.writes == []


async def test_thread_locked_while_inference_runs_is_not_changed(world):
    def lock():
        world.channels[200]["thread_metadata"]["locked"] = True

    world.after_inference = lock
    assert (await world.bot.tagger.tag(200)).status == "skipped"
    assert world.writes == []


async def test_concurrent_duplicate_events_do_not_repeat_the_same_retag(world):
    results = await asyncio.gather(world.bot.tagger.tag(200), world.bot.tagger.tag(200))
    assert sorted(result.status for result in results) == ["unchanged", "updated"]
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert len(world.writes) == 1


async def test_duplicate_and_excess_suggestions_keep_most_relevant(world, replace_mode):
    world.suggestions = ["13", "13", "11", "12"]
    await world.bot.tagger.tag(200)
    assert world.channels[200]["applied_tags"] == ["14", "13", "11", "12"]
    world.bot.tagger.classifier.max_tags = 2
    await world.bot.tagger.tag(200)
    assert world.channels[200]["applied_tags"] == ["14", "13", "11"]


@pytest.mark.parametrize(
    "content",
    [
        '{"tag_ids":["999"]}',
        '{"tag_ids":["14"]}',
        '{"tag_ids":[11]}',
        '{"tag_ids":"11"}',
        '{"tag_ids":[],"extra":true}',
        "not json",
        "null",
    ],
)
async def test_invalid_classifications_never_edit_tags(world, content):
    world.content = content
    with pytest.raises(ClassificationError, match="invalid classification"):
        await world.bot.tagger.tag(200)
    assert world.channels[200]["applied_tags"] == ["14"]
    assert world.writes == []


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_router_failure_has_no_weaker_retry_or_content_log(world, status, caplog):
    world.status = status
    world.content = "PRIVATE RESPONSE"
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert len(world.requests) == 1
    assert world.writes == []
    assert "ClassificationError" in caplog.text
    assert "PRIVATE RESPONSE" not in caplog.text
    assert "digital landscape" not in caplog.text


async def test_unexpected_event_errors_log_traceback_without_event_arguments(world, caplog):
    try:
        raise RuntimeError("bug")
    except RuntimeError:
        with caplog.at_level(logging.ERROR):
            await world.bot.on_error("on_thread_create", "PRIVATE ARGUMENT")
    assert "Traceback" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "PRIVATE ARGUMENT" not in caplog.text


async def test_truncated_response_is_not_confused_with_valid_empty_selection(world, replace_mode):
    world.channels[200]["applied_tags"] = ["11", "14"]
    world.finish = "length"
    with pytest.raises(ClassificationError):
        await world.bot.tagger.tag(200)
    assert world.writes == []
    world.finish = "stop"
    world.suggestions = []
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (0, 1)
    assert world.channels[200]["applied_tags"] == ["14"]


async def test_empty_selection_is_not_applied_when_forum_requires_a_tag(world, replace_mode):
    world.channels[100]["flags"] = 1 << 4  # REQUIRE_TAG
    world.channels[200]["applied_tags"] = ["11"]
    world.suggestions = []
    result = await world.bot.tagger.tag(200)
    assert result.status == "skipped"
    assert world.channels[200]["applied_tags"] == ["11"]
    assert world.writes == []


async def test_actions_are_logged_with_tag_names_but_no_post_content(world, caplog):
    with caplog.at_level(logging.INFO):
        await world.bot.on_thread_create(await world.bot.fetch_channel(200))
        await world.bot.tagger.tag(200)
    assert "New post 200 in forum 100" in caplog.text
    assert "Thread 200 updated: Retagged: +Painting, +Digital." in caplog.text
    assert "Thread 200 unchanged" in caplog.text
    assert "My digital painting" not in caplog.text
    assert "digital landscape" not in caplog.text


async def test_archived_thread_is_retagged_and_archived_again_when_included(world):
    world.channels[200]["thread_metadata"]["archived"] = True
    result = await world.bot.tagger.tag(200, include_archived=True)
    assert result.status == "updated"
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert world.channels[200]["thread_metadata"]["archived"] is True
    assert world.writes == [200, 200]  # Unarchive+retag, then re-archive.


async def test_locked_archived_thread_is_skipped_even_when_archived_are_included(world):
    world.channels[200]["thread_metadata"].update(archived=True, locked=True)
    result = await world.bot.tagger.tag(200, include_archived=True)
    assert result.status == "skipped"
    assert world.requests == []
    assert world.writes == []


async def test_replace_mode_logs_removed_tag_names(world, replace_mode, caplog):
    world.channels[200]["applied_tags"] = ["13", "14"]
    with caplog.at_level(logging.INFO):
        await world.bot.tagger.tag(200)
    assert "Retagged: +Painting, +Digital, -Critique." in caplog.text
