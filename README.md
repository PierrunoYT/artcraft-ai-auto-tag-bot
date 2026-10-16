# ArtCraft AI Auto-Tag Bot

A Python 3.12+ Discord bot that classifies new posts in selected forum channels
using OpenRouter. Default model: **`openai/gpt-4.1-mini`**.

[Terms of Service](TERMS_OF_SERVICE.md) · [Privacy Policy](PRIVACY_POLICY.md)

- Uses existing forum tags only; never creates tags.
- Replaces the entire applied tag set with the AI selection, adding and removing
  tags as needed. Manual and moderator-only tags are not protected.
- Enforces OpenRouter zero-data-retention routing on every request.
- No database, message files, or message-content logs; Discord message caching is disabled.
- Moderator-only slash commands for one thread or a batch of active threads.

## Setup

1. Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then run:

   ```sh
   uv sync --locked
   cp .env.example .env
   ```

2. Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Enable **Message Content Intent** in its Bot settings (approval may be required
   for verified bots). No Members or Presence intent is needed.
3. Invite it with the **`bot`** and **`applications.commands`** scopes. Grant **View
   Channel**, **Read Message History**, and **Manage Threads** in each selected forum.
   Administrator is not needed. Make sure channel overwrites allow those permissions.
4. Create the forum's tags in Discord. Enable Developer Mode in Discord, copy the
   forum channel IDs, and set `FORUM_CHANNEL_IDS` in `.env`.
5. Set `DISCORD_TOKEN` and `OPENROUTER_API_KEY` locally in `.env`. Never commit or
   share these values. Environment variables take precedence over `.env`.
6. Run:

   ```sh
   uv run artcraft-bot
   ```

Commands are registered globally on startup; Discord may take time to display them.
Keep one bot process running. New posts are handled while it is online; there is
no persistent job queue or automatic replay after downtime. Use the batch command
to process missed posts. Restart after configuration changes.

## Discord policy URLs

In your application's **General Information** page in the
[Discord Developer Portal](https://discord.com/developers/applications), use:

- **Terms of Service URL:** https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot/blob/main/TERMS_OF_SERVICE.md
- **Privacy Policy URL:** https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot/blob/main/PRIVACY_POLICY.md

These documents describe the bot's current behavior; they are not a claim of
Discord approval or legal compliance for every deployment. Operators must also
provide their identity, private contact route, and hosting/log-retention details
to affected users. If you modify the bot or its data handling, update the policies
and use URLs for your own deployment. See also the official
[Discord Terms of Service](https://discord.com/terms) and
[Discord Developer Terms of Service](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service).

## Configuration

| Variable | Required | Default / meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key with credits |
| `FORUM_CHANNEL_IDS` | Yes | Comma-separated forum IDs; no other forums are processed |
| `OPENROUTER_MODEL` | No | `openai/gpt-4.1-mini` |
| `MAX_AI_TAGS` | No | `3`, from 1 to 5; maximum tags in the replacement set |

Replacement models need a ZDR-eligible endpoint supporting JSON-schema structured
outputs. Requests fail closed if no compatible endpoint exists. The bot never
falls back to non-ZDR routing. Web-search `:online` models are rejected; do not
configure OpenRouter presets or account integrations that enable external tools
or prompt logging.

## Moderator commands

Both commands require **Manage Threads**, checked at runtime as well as in default
command permissions. The caller must also have access and Manage Threads in the
target channel in the same server. Replies are ephemeral.

- **`/autotag thread [thread_id]`** — replace all tags on the current thread, or supply a thread ID.
- **`/autotag active forum:<forum> [limit:20]`** — retag up to 100 active threads,
  newest first, sequentially. Reports updated, unchanged, and failed counts.
  Only one batch per forum can run at a time. A batch stops after ten minutes,
  reporting partial progress; individual thread IDs can handle remaining posts.
  Re-running a limited batch examines the newest threads again, not a saved cursor.

Archived and locked threads are never reopened or changed. Classification uses
the title and up to 6,000 characters of the **starter message**, not replies,
attachment contents, embeds, or linked pages. An image-only post is classified
from its title. Missing starter messages are skipped after brief retries.

All existing forum tags, including moderator-only tags, are eligible for selection.
Every run replaces **all applied tags**, including ones added manually. Full
five-tag posts are reclassified too. A valid empty AI selection clears all applied
tags; an invalid response or failed classification leaves them unchanged.
Single-thread results report additions and removals; removal-only changes count as
updated in batch results. Archived/locked threads and forums without any available
tags are skipped. If a selected tag is deleted during classification, the bot skips
the write so a stale selection cannot accidentally clear the thread.

## Privacy and retagging boundaries

Each OpenRouter request includes:

```json
{"provider": {"zdr": true, "data_collection": "deny", "require_parameters": true}}
```

The bot sends the title, starter text, and eligible tag names/IDs to OpenRouter and
the selected inference provider. No author IDs, message IDs, attachment downloads,
or chat history are intentionally included (the text itself can contain personal
information). Data exists transiently in process memory. There is no application
database, on-disk message persistence, or prompt/response logging. Logs contain
operational status, thread IDs, and exception types only.

[ZDR is a provider-routing policy](https://openrouter.ai/docs/guides/features/zdr),
not a claim that data never leaves Discord. OpenRouter can retain usage metadata;
disable opt-in prompt logging in its privacy settings. Discord's own retention is
unchanged. No plugins or tools are requested. Invalid model output, unavailable
providers, and permission failures do not cause destructive tag edits or weaker
privacy retries. API calls can incur charges; there are no automatic inference retries.

The bot serializes its own work and fetches fresh tags before replacing them.
**Manual edits made during classification can be overwritten intentionally** by
the complete AI selection. Discord offers no conditional write, so edits by other
clients can race with the final write too. Use one instance and avoid concurrent
tag automation. Raw tag IDs are used to count removals even when discord.py's parent
cache is stale; the dependency is bounded to the tested 2.7 series because this
uses its internal raw-ID representation together with its public `edit` method.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use mocked HTTP and Discord state; they do not send Discord messages, call
paid models, or require credentials. For a live smoke test, use a private test
server: create a post with an irrelevant manual tag, confirm it is replaced by
relevant existing tags, try a full five-tag post and a valid empty selection, and
verify a member without Manage Threads cannot run
either command. Check OpenRouter activity/settings for the selected ZDR provider.
