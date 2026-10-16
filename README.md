# ArtCraft AI Auto-Tag Bot

A Python 3.12+ Discord bot that classifies new posts in selected forum channels
using OpenRouter. Default model: **`openai/gpt-6-luna`**.

[Terms of Service](TERMS_OF_SERVICE.md) · [Privacy Policy](PRIVACY_POLICY.md)

- Uses existing forum tags only; never creates tags.
- By default adds AI-selected tags and keeps the tags already applied. Set
  `TAG_MODE=replace` to replace member-selectable tags with the AI selection instead.
- Never applies or removes moderator-only tags.
- Enforces OpenRouter zero-data-retention routing on every request.
- No database, message files, or message-content logs; Discord message caching is disabled.
- Slash commands for one thread or a batch of active threads, limited to an allowlist
  of Discord user IDs.

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
| `OPENROUTER_MODEL` | No | `openai/gpt-6-luna` |
| `MAX_AI_TAGS` | No | `3`, from 1 to 5; maximum tags the AI may choose |
| `TAG_MODE` | No | `add` (keep existing tags) or `replace` (replace member-selectable tags) |
| `COMMAND_USER_IDS` | No | Comma-separated user IDs allowed to run `/autotag`; empty disables the commands |

The default model is served through a ZDR endpoint with structured outputs.
Replacement models need a ZDR-eligible endpoint supporting JSON-schema structured
outputs; check `https://openrouter.ai/api/v1/endpoints/zdr` for current options. Requests fail closed if no compatible endpoint exists. The bot never
falls back to non-ZDR routing. Web-search `:online` models are rejected; do not
configure OpenRouter presets or account integrations that enable external tools
or prompt logging.

## Moderator commands

Only users listed in `COMMAND_USER_IDS` can run the commands; everyone else gets an
ephemeral refusal. Allowed users also need **Manage Threads**, plus access and
Manage Threads in the target channel in the same server. Replies are ephemeral.

Discord decides who *sees* the commands. They default to members with Manage
Threads (administrators always see them). To hide them from everyone else, a server
admin can open **Server Settings → Integrations →** your bot, deny `/autotag` for
`@everyone`, and allow the listed users. The user ID allowlist is enforced even if
someone can see the commands.

- **`/autotag thread [thread_id]`** — tag the current thread, or supply a thread ID.
  Tagging runs one thread at a time; if other work is queued, the command can time
  out after a minute and asks you to retry.
- **`/autotag active forum:<forum> [limit:20]`** — tag up to 100 active threads,
  newest first, sequentially. Reports updated, unchanged, skipped (ineligible, such
  as locked or missing a starter message), and failed counts.
  Only one batch per forum can run at a time. A batch stops after ten minutes,
  reporting partial progress; individual thread IDs can handle remaining posts.
  Re-running a limited batch examines the newest threads again, not a saved cursor.
- **`/autotag all forum:<forum> [include_archived:True]`** — tag **every** post in
  the forum, active and archived, newest first, one at a time. It runs in the
  background with no post or time limit; new posts are still tagged meanwhile, and
  progress is logged every 25 posts. The summary arrives as a command reply, or by
  DM if the run outlasts Discord's 15-minute reply window. Archived posts are
  unarchived and retagged in one edit, then archived again immediately.
- **`/autotag stop forum:<forum>`** — stop a running `/autotag all` after the
  current post.

Only one batch or full run per forum can run at a time. Each examined post costs one
AI call, so a forum with thousands of posts takes a while; at roughly 2–5 seconds
per post, 1,000 posts take about an hour.

Locked threads are never changed. Archived threads are changed only by
`/autotag all`; new posts, `/autotag thread`, and `/autotag active` skip them.
Classification uses
the title and up to 6,000 characters of the **starter message**, not replies,
attachment contents, embeds, or linked pages. An image-only post is classified
from its title. Missing starter messages are skipped after brief retries.

Only member-selectable tags are offered to the model. Moderator-only tags are
never applied by the bot, so post text cannot talk the model into setting one, and
they are kept on the thread in both modes. If the model returns more than
`MAX_AI_TAGS` tags, duplicates are dropped and the most relevant ones are kept.
A thread never exceeds Discord's five-tag limit.

- **`add` (default):** AI tags are appended after existing tags, up to five in total.
  Nothing is removed, so a full five-tag post is left unchanged.
- **`replace`:** every run replaces member-selectable tags, including ones added
  manually, with the AI selection. A valid empty AI selection clears them, unless
  the forum requires a tag, in which case the write is skipped.

An invalid response or failed classification leaves tags unchanged. Single-thread
results report additions and removals; removal-only changes count as updated in
batch results. Locked threads, archived threads outside `/autotag all`, and forums
without member-selectable tags are skipped. If a selected tag is deleted or made moderator-only during
classification, the bot skips the write so a stale selection is never applied.

## Privacy and retagging boundaries

Each OpenRouter request includes:

```json
{"provider": {"zdr": true, "data_collection": "deny", "require_parameters": true}}
```

The bot sends the title, starter text, and eligible tag names/IDs to OpenRouter and
the selected inference provider. No author IDs, message IDs, attachment downloads,
or chat history are intentionally included (the text itself can contain personal
information). Data exists transiently in process memory. There is no application
database, on-disk message persistence, or prompt/response logging.

Every action is logged with a timestamp: new posts, each tagging result with the
tag names added or removed (or why a thread was skipped), command runs and refusals
with the caller's user ID, and batch summaries. Logs contain thread, forum, and user
IDs, tag names, and exception types, never post titles or text. Unexpected errors
also log a stack trace, without event arguments or message content. Logs go to the
console; to keep them, redirect output, for example
`uv run artcraft-bot *>> artcraft.log` in PowerShell.

[ZDR is a provider-routing policy](https://openrouter.ai/docs/guides/features/zdr),
not a claim that data never leaves Discord. OpenRouter can retain usage metadata;
disable opt-in prompt logging in its privacy settings. Discord's own retention is
unchanged. No plugins or tools are requested. Invalid model output, unavailable
providers, and permission failures do not cause destructive tag edits or weaker
privacy retries. API calls can incur charges; there are no automatic inference retries.

The bot serializes its own work and fetches fresh tags before replacing them.
Discord offers no conditional write, so **tag edits made by others during
classification can be overwritten** by the final write. Use one instance and avoid concurrent
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
server: create a post with a manual tag and confirm relevant tags are added
alongside it; with `TAG_MODE=replace`, confirm an irrelevant tag is replaced but a
moderator-only tag stays, and try a valid empty selection in a forum that requires
tags. Verify that a user missing from `COMMAND_USER_IDS` cannot run either command. Check OpenRouter activity/settings for the selected ZDR provider.
