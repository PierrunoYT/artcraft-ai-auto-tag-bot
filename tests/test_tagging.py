import asyncio
import json

import pytest

from artcraft.classifier import ClassificationError


async def test_new_thread_replaces_manual_tags_and_enforces_zdr(world):
    thread = await world.bot.fetch_channel(200)
    await world.bot.on_thread_create(thread)
    assert world.channels[200]["applied_tags"] == ["11", "12"]
    payload = world.requests[0]
    assert payload["model"] == "openai/gpt-4.1-mini"
    assert payload["provider"] == {
        "zdr": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    assert payload["plugins"] == []
    prompt = json.loads(payload["messages"][1]["content"])
    assert prompt == {
        "title": "My digital painting",
        "body": "A digital landscape, feedback welcome.",
        "available_tags": {
            "11": "Painting",
            "12": "Digital",
            "13": "Critique",
            "14": "Approved",
        },
    }


async def test_full_thread_retagged_including_manual_edits_during_inference(world):
    world.channels[200]["applied_tags"] = ["11", "12", "13", "14", "91"]

    def manual_edit():
        # 91-93 are manually applied IDs missing from the cached forum.
        world.channels[200]["applied_tags"] = ["14", "91", "92", "93"]

    world.after_inference = manual_edit
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (2, 4)
    assert world.channels[200]["applied_tags"] == ["11", "12"]


async def test_deleted_candidate_invalidates_classification_without_editing_tags(world):
    def manual_edit():
        world.channels[200]["applied_tags"] = []
        world.channels[100]["available_tags"] = [
            tag for tag in world.channels[100]["available_tags"] if tag["id"] != "11"
        ]

    world.after_inference = manual_edit
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (0, 0)
    assert world.channels[200]["applied_tags"] == []
    assert world.writes == []


@pytest.mark.parametrize("state", ["archived", "locked", "other-forum"])
async def test_ineligible_threads_never_send_content_or_write(world, state):
    if state in ("archived", "locked"):
        world.channels[200]["thread_metadata"][state] = True
    else:
        world.channels[200]["parent_id"] = "101"
    result = await world.bot.tagger.tag(200)
    assert result.added == 0
    assert world.requests == []
    assert world.writes == []


async def test_thread_locked_while_inference_runs_is_not_changed(world):
    def lock():
        world.channels[200]["thread_metadata"]["locked"] = True

    world.after_inference = lock
    assert (await world.bot.tagger.tag(200)).added == 0
    assert world.writes == []


async def test_concurrent_duplicate_events_do_not_repeat_the_same_retag(world):
    results = await asyncio.gather(world.bot.tagger.tag(200), world.bot.tagger.tag(200))
    assert sorted(result.added for result in results) == [0, 2]
    assert sorted(result.removed for result in results) == [0, 1]
    assert world.channels[200]["applied_tags"] == ["11", "12"]
    assert len(world.writes) == 1


@pytest.mark.parametrize(
    "content",
    [
        '{"tag_ids":["999"]}',
        '{"tag_ids":[11]}',
        '{"tag_ids":"11"}',
        '{"tag_ids":["11","12","13","11"]}',
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


async def test_truncated_response_is_not_confused_with_valid_empty_selection(world):
    world.finish = "length"
    with pytest.raises(ClassificationError):
        await world.bot.tagger.tag(200)
    assert world.writes == []
    assert world.channels[200]["applied_tags"] == ["14"]
    world.finish = "stop"
    world.suggestions = []
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (0, 1)
    assert world.channels[200]["applied_tags"] == []


async def test_moderator_only_tags_can_be_selected_and_unselected_tags_removed(world):
    world.channels[200]["applied_tags"] = ["11", "12"]
    world.suggestions = ["14", "12"]
    result = await world.bot.tagger.tag(200)
    assert (result.added, result.removed) == (1, 1)
    assert world.channels[200]["applied_tags"] == ["14", "12"]
