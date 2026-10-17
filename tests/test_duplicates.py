import logging
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from artcraft import duplicates as duplicates_module
from artcraft.duplicates import DuplicateIndex

SIMILAR = [0.95, 0.312, 0.0]  # Cosine similarity with [1, 0, 0] is about 0.95.


def add_thread(world, thread_id, name, **metadata):
    world.channels[thread_id] = deepcopy(world.channels[200])
    world.channels[thread_id].update(id=str(thread_id), name=name)
    world.channels[thread_id]["thread_metadata"].update(metadata)


def read_bytes(path):
    return Path(path).read_bytes()


async def index(world, finder, thread_id):
    return await finder.index_post(await world.bot.fetch_channel(thread_id))


async def test_duplicate_detection_is_off_by_default(world):
    assert world.bot.duplicates is None
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert world.embeddings == []
    assert world.sent == []


async def test_new_post_gets_a_reply_linking_similar_posts_and_is_indexed(world, duplicates):
    add_thread(world, 201, "Export loses gradients")
    add_thread(world, 202, "Unrelated question")
    world.vectors = {
        "Export loses gradients": [1.0, 0.0, 0.0],
        "Unrelated question": [0.0, 0.0, 1.0],
        "My digital painting": SIMILAR,
    }
    assert await index(world, duplicates, 201) == "indexed"
    assert await index(world, duplicates, 202) == "indexed"
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))

    [(channel_id, payload)] = world.sent
    assert channel_id == 200
    assert "<#201> (95% similar)" in payload["content"]
    assert "<#202>" not in payload["content"]
    assert payload["allowed_mentions"]["parse"] == []
    assert 200 in duplicates.index
    request = world.embeddings[-1]
    assert request["model"] == "openai/text-embedding-3-small"
    assert request["provider"] == {"zdr": True, "data_collection": "deny"}
    assert request["input"].startswith("My digital painting\n\nA digital landscape")


async def test_reply_lists_up_to_the_configured_number_of_matches(world, duplicates):
    titles = [f"Gradient export bug {n}" for n in range(7)]
    for offset, title in enumerate(titles):
        add_thread(world, 201 + offset, title)
        # Descending similarity, all above the threshold.
        world.vectors[title] = [1.0, 0.05 * offset, 0.0]
    world.vectors["My digital painting"] = [1.0, 0.0, 0.0]
    for offset in range(7):
        await index(world, duplicates, 201 + offset)
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    [(_, payload)] = world.sent
    links = [line for line in payload["content"].splitlines() if line.startswith("•")]
    assert duplicates.config.duplicate_max_matches == 5
    assert [line.split()[1] for line in links] == [f"<#{t}>" for t in range(201, 206)]


async def test_tagging_failure_does_not_block_the_duplicate_check(world, duplicates):
    add_thread(world, 201, "Export loses gradients")
    world.vectors = {"Export loses gradients": [1.0, 0.0, 0.0], "My digital painting": SIMILAR}
    await index(world, duplicates, 201)
    world.status = 503  # Classification fails.
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert world.writes == []
    assert len(world.sent) == 1


async def test_duplicate_failure_does_not_block_tagging(world, duplicates):
    world.embedding_status = 503
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert world.channels[200]["applied_tags"] == ["14", "11", "12"]
    assert world.sent == []


async def test_no_reply_when_nothing_reaches_the_threshold(world, duplicates):
    add_thread(world, 201, "Unrelated question")
    world.vectors = {"Unrelated question": [0.0, 0.0, 1.0], "My digital painting": SIMILAR}
    await index(world, duplicates, 201)
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert world.sent == []
    assert 200 in duplicates.index


async def test_index_stores_only_ids_and_encrypted_vectors(world, duplicates):
    await index(world, duplicates, 200)
    path = duplicates.config.duplicate_db_path
    db = sqlite3.connect(path)
    columns = [row[1] for row in db.execute("PRAGMA table_info(posts)")]
    [(thread_id, forum_id, model, blob)] = db.execute("SELECT * FROM posts").fetchall()
    db.close()
    assert columns == ["thread_id", "forum_id", "model", "vector"]
    assert (thread_id, forum_id) == (200, 100)
    assert Fernet(duplicates.config.duplicate_db_key).decrypt(blob)  # Only with the key.
    raw = read_bytes(path)
    assert b"My digital painting" not in raw
    assert b"digital landscape" not in raw


async def test_reopening_restores_the_index_and_a_wrong_key_fails(world, duplicates):
    await index(world, duplicates, 200)
    path, key = duplicates.config.duplicate_db_path, duplicates.config.duplicate_db_key
    duplicates.index.close()
    reopened = DuplicateIndex(path, key, "openai/text-embedding-3-small")
    assert 200 in reopened
    reopened.close()
    with pytest.raises(ValueError, match="DUPLICATE_DB_KEY"):
        DuplicateIndex(path, Fernet.generate_key().decode(), "openai/text-embedding-3-small")
    # Vectors from another model are incomparable and are dropped.
    other = DuplicateIndex(path, key, "another/model")
    assert len(other) == 0
    other.close()
    world.bot.duplicates = None  # Already closed; skip teardown close.


def stored_rows(duplicates):
    db = sqlite3.connect(duplicates.config.duplicate_db_path)
    count = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    db.close()
    return count


async def delete_starter(world, event):
    world.deleted_starters.add(200)
    if event == "starter":
        await world.bot.on_raw_message_delete(SimpleNamespace(message_id=200, channel_id=200))
    else:
        await world.bot.on_raw_bulk_message_delete(
            SimpleNamespace(message_ids={200, 555}, channel_id=200)
        )


async def test_thread_deletion_removes_stored_data(world, duplicates):
    await index(world, duplicates, 200)
    await world.bot.on_raw_thread_delete(SimpleNamespace(thread_id=200))
    assert 200 not in duplicates.index
    assert stored_rows(duplicates) == 0


@pytest.mark.parametrize("event", ["starter", "bulk"])
async def test_starter_deletion_replaces_the_entry_with_a_title_only_one(world, duplicates, event):
    world.vectors = {"My digital painting": [1.0, 0.0, 0.0]}
    await index(world, duplicates, 200)
    await delete_starter(world, event)
    assert world.embeddings[-1]["input"] == "My digital painting"  # No deleted text.
    assert 200 in duplicates.index
    assert stored_rows(duplicates) == 1


@pytest.mark.parametrize("event", ["starter", "bulk"])
async def test_starter_deletion_with_a_short_title_removes_the_entry(world, duplicates, event):
    world.channels[200]["name"] = "Bug report"
    await index(world, duplicates, 200)
    calls = len(world.embeddings)
    await delete_starter(world, event)
    assert len(world.embeddings) == calls
    assert 200 not in duplicates.index
    assert stored_rows(duplicates) == 0


async def test_failed_reindex_after_starter_deletion_removes_the_entry(world, duplicates):
    await index(world, duplicates, 200)
    world.embedding_status = 503
    await delete_starter(world, "starter")
    assert 200 not in duplicates.index
    assert stored_rows(duplicates) == 0


async def test_reply_deletion_does_not_remove_the_post(world, duplicates):
    await index(world, duplicates, 200)
    await world.bot.on_raw_message_delete(SimpleNamespace(message_id=777, channel_id=200))
    assert 200 in duplicates.index


async def test_starter_edit_reindexes_the_post(world, duplicates):
    await index(world, duplicates, 200)
    world.vectors = {"My digital painting": [1.0, 0.0, 0.0]}
    await world.bot.on_raw_message_edit(SimpleNamespace(message_id=200, channel_id=200))
    assert list(duplicates.index.get(200)) == [1.0, 0.0, 0.0]


async def test_tagging_runs_do_not_index(world, duplicates, interaction):
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    await group.full_runs[100]
    assert world.embeddings == []
    assert len(duplicates.index) == 0


async def test_index_run_indexes_locked_posts_once_without_tagging(world, duplicates, interaction):
    add_thread(world, 201, "Locked request", locked=True)
    group = world.bot.tree.get_command("duplicates")
    await group.index.callback(group, interaction, world.guild.get_channel(100))
    assert "Started indexing 2 posts" in interaction.followup.send.call_args.args[0]
    await group.full_runs[100]
    assert {200, 201} <= set(duplicates.index.forum_of)
    summary = interaction.followup.send.call_args.args[0]
    assert "Examined 2/2 posts: 2 indexed, 0 unchanged, 0 skipped, 0 failed" in summary
    assert world.requests == [] and world.writes == []  # No classification or tag edits.
    calls = len(world.embeddings)
    await group.index.callback(group, interaction, world.guild.get_channel(100))
    await group.full_runs[100]
    assert len(world.embeddings) == calls  # Already indexed posts are not re-embedded.
    assert "0 indexed, 2 unchanged" in interaction.followup.send.call_args.args[0]
    assert world.sent == []  # Index runs never reply in posts.


async def test_index_run_uses_its_own_higher_concurrency(world, duplicates, interaction):
    for thread_id in range(201, 215):
        add_thread(world, thread_id, f"Post {thread_id}")
    group = world.bot.tree.get_command("duplicates")
    await group.index.callback(group, interaction, world.guild.get_channel(100))
    assert (
        "15 posts (15 active, 0 archived), newest first, 10 at a time"
        in (interaction.followup.send.call_args.args[0])
    )
    await group.full_runs[100]
    assert world.bot.config.tag_concurrency == 4
    assert world.max_embed_in_flight == world.bot.config.index_concurrency == 10
    assert len(duplicates.index) == 15


async def test_index_run_can_be_stopped(world, duplicates, interaction):
    group = world.bot.tree.get_command("duplicates")
    await group.index.callback(group, interaction, world.guild.get_channel(100))
    task = group.full_runs[100]
    interaction.response.send_message = AsyncMock()
    await group.stop.callback(group, interaction, world.guild.get_channel(100))
    await task
    assert world.embeddings == []
    assert "stopped. Examined 0/1 posts" in interaction.followup.send.call_args.args[0]


async def test_duplicate_commands_explain_when_detection_is_off(world, interaction):
    group = world.bot.tree.get_command("duplicates")
    await group.index.callback(group, interaction, world.guild.get_channel(100))
    assert "Duplicate detection is off" in interaction.followup.send.call_args.args[0]
    await group.similar.callback(group, interaction)
    assert "Duplicate detection is off" in interaction.followup.send.call_args.args[0]


async def test_forget_deletes_data_and_prevents_reindexing(world, duplicates, interaction):
    await index(world, duplicates, 200)
    group = world.bot.tree.get_command("duplicates")
    await group.forget.callback(group, interaction, "200")
    assert "Deleted stored data" in interaction.followup.send.call_args.args[0]
    calls = len(world.embeddings)
    assert await index(world, duplicates, 200) == "skipped"
    await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert len(world.embeddings) == calls
    assert 200 not in duplicates.index


async def test_similar_command_marks_matches_above_the_threshold(world, duplicates, interaction):
    add_thread(world, 201, "Export loses gradients")
    add_thread(world, 202, "Unrelated question")
    world.vectors = {
        "Export loses gradients": [1.0, 0.0, 0.0],
        "Unrelated question": [0.0, 0.0, 1.0],
        "My digital painting": SIMILAR,
    }
    for thread_id in (200, 201, 202):
        await index(world, duplicates, thread_id)
    group = world.bot.tree.get_command("duplicates")
    await group.similar.callback(group, interaction)
    message = interaction.followup.send.call_args.args[0]
    assert "<#201> 95% ✅" in message
    assert "<#202> 0%" in message and "<#202> 0% ✅" not in message


async def test_unconfigured_forums_are_purged(world, duplicates):
    duplicates.index.add(300, 999, duplicates_module.normalize([1.0, 0.0]))
    await index(world, duplicates, 200)
    assert duplicates.index.keep_forums(frozenset({100})) == 1
    assert 300 not in duplicates.index and 200 in duplicates.index


async def test_embedding_failures_are_logged_safely(world, duplicates, caplog):
    world.embedding_status = 503
    with caplog.at_level(logging.WARNING):
        await world.bot.on_thread_create(await world.bot.fetch_channel(200))
    assert "duplicate check failed (EmbeddingError)" in caplog.text
    assert "digital landscape" not in caplog.text
    assert world.sent == []


async def test_new_post_without_a_starter_and_a_short_title_is_not_indexed(
    world, duplicates, no_retry_wait
):
    world.channels[200]["name"] = "Bug"
    world.deleted_starters.add(200)
    await duplicates.check_new_post(await world.bot.fetch_channel(200))
    assert world.embeddings == []
    assert 200 not in duplicates.index


async def test_post_with_a_deleted_starter_is_indexed_from_its_title(world, duplicates):
    world.deleted_starters.add(200)
    assert await index(world, duplicates, 200) == "indexed"
    assert world.embeddings[-1]["input"] == "My digital painting"


async def test_new_post_waits_for_a_lagging_starter(world, duplicates, no_retry_wait):
    world.lagging_starters[200] = 2  # Readable on the third attempt.
    await duplicates.check_new_post(await world.bot.fetch_channel(200))
    assert world.embeddings[-1]["input"].startswith("My digital painting\n\nA digital")
