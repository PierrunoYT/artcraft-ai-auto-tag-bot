import asyncio
import dataclasses
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from cryptography.fernet import Fernet

from artcraft import posts
from artcraft.bot import ArtCraftBot
from artcraft.classifier import DuplicateJudge
from artcraft.config import Config
from artcraft.duplicates import DuplicateFinder, DuplicateIndex, Embedder


@pytest.fixture
async def world(monkeypatch, tmp_path):
    """Real discord.py models/merging over in-memory Discord and OpenRouter transports."""
    bot = ArtCraftBot(
        Config(
            "fake-token",
            "fake-key",
            frozenset({100}),
            command_user_ids=frozenset({2}),
            state_db_path=str(tmp_path / "state.db"),
        )
    )
    bot._connection.user = discord.ClientUser(
        state=bot._connection,
        data={"id": "1", "username": "ArtCraft", "discriminator": "0", "avatar": None},
    )
    forum = {
        "id": "100",
        "guild_id": "10",
        "type": 15,
        "name": "art",
        "position": 0,
        "permission_overwrites": [],
        "available_tags": [
            {"id": "11", "name": "Painting", "moderated": False},
            {"id": "12", "name": "Digital", "moderated": False},
            {"id": "13", "name": "Critique", "moderated": False},
            {"id": "14", "name": "Approved", "moderated": True},
        ],
    }
    for tag in forum["available_tags"]:
        tag.update(emoji_id=None, emoji_name=None)
    thread = {
        "id": "200",
        "guild_id": "10",
        "parent_id": "100",
        "owner_id": "2",
        "type": 11,
        "name": "My digital painting",
        "rate_limit_per_user": 0,
        "message_count": 1,
        "member_count": 1,
        "applied_tags": ["14"],
        "thread_metadata": {
            "archived": False,
            "locked": False,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-10-16T00:00:00+00:00",
        },
    }
    # Populate fixture members so tests can supply the Member Discord normally
    # embeds in an interaction, even though the production bot has no member cache.
    bot._connection.member_cache_flags = discord.MemberCacheFlags.all()
    permissions = discord.Permissions(view_channel=True, manage_threads=True)
    guild = discord.Guild(
        state=bot._connection,
        data={
            "id": "10",
            "name": "Test",
            "owner_id": "1",
            "roles": [
                {"id": "10", "name": "@everyone", "permissions": "1024"},
                {"id": "7", "name": "Moderator", "permissions": str(permissions.value)},
            ],
            "channels": [deepcopy(forum)],
            "members": [
                {
                    "user": {
                        "id": str(i),
                        "username": f"user{i}",
                        "discriminator": "0",
                        "avatar": None,
                    },
                    "roles": ["7"] if i == 2 else [],
                    "flags": 0,
                    "joined_at": "2026-10-16T00:00:00+00:00",
                }
                for i in (1, 2, 3)
            ],
        },
    )
    bot._connection._add_guild(guild)
    world = SimpleNamespace(
        bot=bot,
        guild=guild,
        channels={100: forum, 200: thread},
        requests=[],
        writes=[],
        suggestions=["11", "12"],
        after_inference=lambda: None,
        status=200,
        finish="stop",
        content=None,
        fail_title=None,
        in_flight=0,
        max_in_flight=0,
        embeddings=[],  # Embedding request payloads.
        embedding_status=200,
        embed_in_flight=0,
        max_embed_in_flight=0,
        judge_requests=[],  # AI duplicate-check request payloads.
        judge=None,  # IDs the AI check confirms; None confirms every candidate.
        judge_status=200,
        vectors={},  # Post title -> embedding returned by the fake model.
        sent=[],  # (channel_id, payload) for messages the bot sends.
        messages={},  # Message ID -> current content of the bot's live messages.
        message_channels={},  # Message ID -> thread it was posted in.
        edited=[],  # (channel_id, message_id, payload) for message edits.
        removed_messages=[],  # (channel_id, message_id) for deleted messages.
        deleted_starters=set(),
        lagging_starters={},  # Thread ID -> fetches that 404 before the starter appears.
    )

    async def get_channel(channel_id):
        if channel_id not in world.channels:  # Deleted on Discord.
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "")
        return deepcopy(world.channels[channel_id])

    async def edit_channel(channel_id, **fields):
        channel = world.channels[channel_id]
        metadata = channel["thread_metadata"]
        # Like Discord: archived threads must be unarchived in the editing request.
        assert not metadata["archived"] or "archived" in fields, "edit of archived thread"
        world.writes.append(channel_id)
        if "applied_tags" in fields:
            channel["applied_tags"] = fields["applied_tags"]
        if "archived" in fields:
            metadata["archived"] = fields["archived"]
        return deepcopy(channel)

    async def get_message(channel_id, message_id):
        assert channel_id == message_id  # Only the starter, never replies.
        lagging = world.lagging_starters.get(channel_id, 0)  # Not yet readable.
        if lagging:
            world.lagging_starters[channel_id] = lagging - 1
        if channel_id in world.deleted_starters or lagging:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "")
        return {
            "id": str(message_id),
            "channel_id": str(channel_id),
            "type": 0,
            "content": "A digital landscape, feedback welcome.",
            "author": {"id": "2", "username": "artist", "discriminator": "0", "avatar": None},
            "attachments": [],
            "embeds": [],
            "edited_timestamp": None,
        }

    def threads(archived):
        return [
            deepcopy(c)
            for c in world.channels.values()
            if c["type"] == 11 and c["thread_metadata"]["archived"] == archived
        ]

    async def active_threads(guild_id):
        return {"threads": threads(False), "members": []}

    async def archived_threads(channel_id, before=None, limit=50):
        forum_threads = [t for t in threads(True) if t["parent_id"] == str(channel_id)]
        return {"threads": forum_threads, "members": [], "has_more": False}

    def bot_message(message_id, channel_id, content):
        return {
            "id": str(message_id),
            "channel_id": str(channel_id),
            "type": 0,
            "content": content,
            "author": {"id": "1", "username": "ArtCraft", "discriminator": "0", "avatar": None},
            "attachments": [],
            "embeds": [],
            "mentions": [],
            "mention_roles": [],
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "timestamp": "2026-10-17T00:00:00+00:00",
            "edited_timestamp": None,
        }

    def assert_writable(channel_id):
        # Like Discord: messages cannot be sent or changed in archived threads.
        assert not world.channels[channel_id]["thread_metadata"]["archived"], "archived"

    async def send_message(channel_id, *, params):
        assert_writable(channel_id)
        world.sent.append((channel_id, params.payload))
        message_id = 900 + len(world.sent)
        world.messages[message_id] = params.payload["content"]
        world.message_channels[message_id] = channel_id
        return bot_message(message_id, channel_id, params.payload["content"])

    async def edit_message(channel_id, message_id, *, params):
        assert_writable(channel_id)
        if message_id not in world.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "")
        world.edited.append((channel_id, message_id, params.payload))
        world.messages[message_id] = params.payload["content"]
        return bot_message(message_id, channel_id, params.payload["content"])

    async def delete_message(channel_id, message_id, *, reason=None):
        assert_writable(channel_id)
        world.messages.pop(message_id, None)
        world.removed_messages.append((channel_id, message_id))

    async def logs_from(channel_id, limit, before=None, after=None, around=None):
        # A thread's history: the author's starter plus the bot's live messages.
        starter = bot_message(channel_id, channel_id, "Starter text")
        starter["author"] = {"id": "2", "username": "artist", "discriminator": "0", "avatar": None}
        mine = [
            bot_message(m, channel_id, content)
            for m, content in world.messages.items()
            if world.message_channels.get(m) == channel_id
        ]
        return sorted([starter, *mine], key=lambda m: int(m["id"]), reverse=True)

    monkeypatch.setattr(bot.http, "send_message", send_message)
    monkeypatch.setattr(bot.http, "edit_message", edit_message)
    monkeypatch.setattr(bot.http, "delete_message", delete_message)
    monkeypatch.setattr(bot.http, "logs_from", logs_from)
    monkeypatch.setattr(bot.http, "get_public_archived_threads", archived_threads)
    monkeypatch.setattr(bot.http, "get_channel", get_channel)
    monkeypatch.setattr(bot.http, "edit_channel", edit_channel)
    monkeypatch.setattr(bot.http, "get_message", get_message)
    monkeypatch.setattr(bot.http, "get_active_threads", active_threads)

    async def inference(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            world.embeddings.append(payload)
            world.embed_in_flight += 1
            world.max_embed_in_flight = max(world.max_embed_in_flight, world.embed_in_flight)
            await asyncio.sleep(0)  # Permit overlap like a real network call.
            world.embed_in_flight -= 1
            title = payload["input"].split("\n\n")[0]
            vector = world.vectors.get(title, [0.0, 1.0, 0.0])
            return httpx.Response(
                world.embedding_status, json={"data": [{"embedding": vector, "index": 0}]}
            )
        if payload["response_format"]["json_schema"]["name"] == "duplicate_posts":
            world.judge_requests.append(payload)
            allowed = payload["response_format"]["json_schema"]["schema"]["properties"][
                "duplicate_ids"
            ]["items"]["enum"]
            # By default the AI check confirms every candidate it is shown.
            chosen = allowed if world.judge is None else world.judge
            return httpx.Response(
                world.judge_status,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"duplicate_ids": chosen})},
                        }
                    ]
                },
            )
        world.requests.append(payload)
        world.in_flight += 1
        world.max_in_flight = max(world.max_in_flight, world.in_flight)
        await asyncio.sleep(0)  # Permit overlapping events like a real network call.
        world.after_inference()
        world.in_flight -= 1
        fail = world.fail_title and world.fail_title in payload["messages"][1]["content"]
        return httpx.Response(
            503 if fail else world.status,
            json={
                "choices": [
                    {
                        "finish_reason": world.finish,
                        "message": {
                            "content": world.content
                            if world.content is not None
                            else json.dumps({"tag_ids": world.suggestions})
                        },
                    }
                ]
            },
        )

    await bot.http_client.aclose()
    bot.http_client = httpx.AsyncClient(transport=httpx.MockTransport(inference))
    bot.tagger.classifier.http = bot.http_client
    yield world
    await bot.close()


DB_KEY = Fernet.generate_key().decode()


@pytest.fixture
def duplicates(world, tmp_path):
    """Enable duplicate detection with an encrypted index in a temporary directory."""
    config = dataclasses.replace(
        world.bot.config,
        duplicates=True,
        duplicate_db_key=DB_KEY,
        duplicate_db_path=str(tmp_path / "duplicates.db"),
    )
    world.bot.config = config
    world.bot.duplicates = DuplicateFinder(
        world.bot,
        Embedder(world.bot.http_client, "fake-key", config.embedding_model),
        DuplicateIndex(config.duplicate_db_path, DB_KEY, config.embedding_model),
        config,
        asyncio.Semaphore(config.index_concurrency),
        DuplicateJudge(world.bot.http_client, "fake-key", config.model),
        world.bot.journal,
    )
    return world.bot.duplicates


@pytest.fixture
def no_retry_wait(monkeypatch):
    """Keep new-post starter retries but without real sleeps."""
    monkeypatch.setattr(posts, "NEW_POST_RETRY_DELAYS", (0, 0, 0, 0))


@pytest.fixture
def replace_mode(world):
    world.bot.tagger.config = dataclasses.replace(world.bot.config, tag_mode="replace")


@pytest.fixture
def interaction(world):
    return SimpleNamespace(
        guild_id=10,
        user=world.guild.get_member(2),
        channel_id=200,
        command=None,
        permissions=discord.Permissions(manage_threads=True),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
