import asyncio
import dataclasses
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest

from artcraft.bot import ArtCraftBot
from artcraft.config import Config


@pytest.fixture
async def world(monkeypatch):
    """Real discord.py models/merging over in-memory Discord and OpenRouter transports."""
    bot = ArtCraftBot(
        Config("fake-token", "fake-key", frozenset({100}), command_user_ids=frozenset({2}))
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
    )

    async def get_channel(channel_id):
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

    monkeypatch.setattr(bot.http, "get_public_archived_threads", archived_threads)
    monkeypatch.setattr(bot.http, "get_channel", get_channel)
    monkeypatch.setattr(bot.http, "edit_channel", edit_channel)
    monkeypatch.setattr(bot.http, "get_message", get_message)
    monkeypatch.setattr(bot.http, "get_active_threads", active_threads)

    async def inference(request):
        payload = json.loads(request.content)
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
