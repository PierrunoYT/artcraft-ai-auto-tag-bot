import logging
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

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
    assert await index(world, duplicates, 201)
    assert await index(world, duplicates, 202)
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


@pytest.mark.parametrize("event", ["thread", "starter", "bulk"])
async def test_deletions_on_discord_remove_stored_data(world, duplicates, event):
    await index(world, duplicates, 200)
    if event == "thread":
        await world.bot.on_raw_thread_delete(SimpleNamespace(thread_id=200))
    elif event == "starter":
        await world.bot.on_raw_message_delete(SimpleNamespace(message_id=200, channel_id=200))
    else:
        await world.bot.on_raw_bulk_message_delete(
            SimpleNamespace(message_ids={200, 555}, channel_id=200)
        )
    assert 200 not in duplicates.index
    db = sqlite3.connect(duplicates.config.duplicate_db_path)
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone() == (0,)
    db.close()


async def test_reply_deletion_does_not_remove_the_post(world, duplicates):
    await index(world, duplicates, 200)
    await world.bot.on_raw_message_delete(SimpleNamespace(message_id=777, channel_id=200))
    assert 200 in duplicates.index


async def test_starter_edit_reindexes_the_post(world, duplicates):
    await index(world, duplicates, 200)
    world.vectors = {"My digital painting": [1.0, 0.0, 0.0]}
    await world.bot.on_raw_message_edit(SimpleNamespace(message_id=200, channel_id=200))
    assert list(duplicates.index.get(200)) == [1.0, 0.0, 0.0]


async def test_full_run_indexes_locked_posts_once(world, duplicates, interaction):
    add_thread(world, 201, "Locked request", locked=True)
    group = world.bot.tree.get_command("autotag")
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    await group.full_runs[100]
    assert {200, 201} <= set(duplicates.index.forum_of)
    assert "2 newly indexed" in interaction.followup.send.call_args.args[0]
    calls = len(world.embeddings)
    await group.tag_all.callback(group, interaction, world.guild.get_channel(100))
    await group.full_runs[100]
    assert len(world.embeddings) == calls  # Already indexed posts are not re-embedded.
    assert world.sent == []  # Full runs never reply in posts.


async def test_forget_deletes_data_and_prevents_reindexing(world, duplicates, interaction):
    await index(world, duplicates, 200)
    group = world.bot.tree.get_command("autotag")
    await group.forget.callback(group, interaction, "200")
    assert "Deleted stored data" in interaction.followup.send.call_args.args[0]
    calls = len(world.embeddings)
    assert not await index(world, duplicates, 200)
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
    group = world.bot.tree.get_command("autotag")
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


async def test_new_post_without_a_starter_is_not_indexed(world, duplicates, monkeypatch):
    monkeypatch.setattr(duplicates_module, "NEW_POST_RETRY_DELAYS", (0, 0))
    world.deleted_starters.add(200)
    await duplicates.check_new_post(await world.bot.fetch_channel(200))
    assert world.embeddings == []
    assert 200 not in duplicates.index
