# ArtCraft AI Auto-Tag Bot

A Python 3.12+ Discord bot that classifies new posts in selected forum channels
using OpenRouter. Default model: **`openai/gpt-6-luna`**.

[Terms of Service](TERMS_OF_SERVICE.md) · [Privacy Policy](PRIVACY_POLICY.md)

- Uses existing forum tags only; never creates tags.
- By default adds AI-selected tags and keeps the tags already applied. Set
  `TAG_MODE=replace` to replace member-selectable tags with the AI selection instead.
- Never applies or removes moderator-only tags.
- Enforces OpenRouter zero-data-retention routing on every request.
- No message text is stored or logged; Discord message caching is disabled.
- Optional duplicate detection (off by default): replies in new posts with links to
  similar earlier posts, using an encrypted index of post embeddings (no text).
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
   Channel**, **Read Message History**, and **Manage Threads** in each selected forum,
   plus **Send Messages in Threads** if duplicate detection is enabled.
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
| `TAG_CONCURRENCY` | No | `4`, from 1 to 10; posts tagged at the same time across new posts and batches |
| `DUPLICATES` | No | `off` (default) or `on`; enables duplicate detection |
| `DUPLICATE_DB_KEY` | If `DUPLICATES=on` | Fernet key encrypting stored embeddings; keep it secret and backed up |
| `DUPLICATE_DB_PATH` | No | `duplicates.db`; SQLite file for the encrypted index |
| `DUPLICATE_THRESHOLD` | No | `0.45`, from 0.2 to 0.95; minimum embedding similarity to shortlist a candidate for the AI check |
| `DUPLICATE_CANDIDATES` | No | `10`, from 1 to 25; candidates shortlisted per new post |
| `DUPLICATE_MAX_MATCHES` | No | `5`, from 1 to 10; most AI-confirmed duplicates linked in a reply |
| `DUPLICATE_CROSS_LINK` | No | `on` (every post in a group lists the others), `active` (skip archived posts), or `off` (new post only) |
| `DUPLICATE_SCOPE` | No | `all` (compare posts across every forum in `FORUM_CHANNEL_IDS`) or `forum` (only within the same forum) |
| `STATE_DB_PATH` | No | `artcraft_state.db`; SQLite action journal used for resume, catch-up, and undo |
| `ACTION_RETENTION_DAYS` | No | `90`, from 0 to 3650; journal entries older than this are deleted; `0` keeps them forever |
| `CATCH_UP` | No | `on` (default) or `off`; handle posts created while the bot was offline |
| `INDEX_CONCURRENCY` | No | `10`, from 1 to 20; posts indexed at the same time |
| `EMBEDDING_MODEL` | No | `openai/text-embedding-3-small`; must have a ZDR endpoint |

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
admin can open **Server Settings → Integrations →** your bot, deny `/autotag` and
`/duplicates` for `@everyone`, and allow the listed users. The user ID allowlist is enforced even if
someone can see the commands.

Tagging and duplicate detection are separate: `/autotag` commands only tag, and
`/duplicates` commands only index and compare posts. On new posts, both run
independently, so a failure in one never blocks the other.

### `/autotag` — tagging

- **`/autotag thread [thread_id]`** — tag the current thread, or supply a thread ID.
  Tagging runs one thread at a time; if other work is queued, the command can time
  out after a minute and asks you to retry.
- **`/autotag active forum:<forum> [limit:20]`** — tag up to 100 active threads,
  newest first, `TAG_CONCURRENCY` at a time. Reports updated, unchanged, skipped (ineligible, such
  as locked or missing a starter message), and failed counts.
  Only one batch per forum can run at a time. A batch stops after ten minutes,
  reporting partial progress; individual thread IDs can handle remaining posts.
  Re-running a limited batch examines the newest threads again, not a saved cursor.
- **`/autotag all forum:<forum> [include_archived:True]`** — tag **every** post in
  the forum, active and archived, newest first, `TAG_CONCURRENCY` at a time. It runs in the
  background with no post or time limit; new posts are still tagged meanwhile, and
  progress is logged every 25 posts. The summary arrives as a command reply, or by
  DM if the run outlasts Discord's 15-minute reply window. Archived posts are
  unarchived and retagged in one edit, then archived again immediately.
- **`/autotag stop forum:<forum>`** — stop a running `/autotag all` after the
  current post.

### `/duplicates` — duplicate detection

These reply "Duplicate detection is off" unless `DUPLICATES=on`.

- **`/duplicates index forum:<forum> [include_archived:True]`** — index every post
  in the forum for duplicate detection, in the background like `/autotag all`, with
  no tagging, `INDEX_CONCURRENCY` posts at a time. Includes locked posts and forums without tags. Already indexed posts
  are skipped, so re-running only embeds new posts. Never replies in posts.
- **`/duplicates stop forum:<forum>`** — stop a running `/duplicates index`.
- **`/duplicates similar [thread_id] [limit:10]`** — list up to 25 of the most
  similar indexed posts with their live titles and scores, running the same AI
  check a new post gets and marking confirmed duplicates with ✅. Posts below the
  shortlist floor are labeled. Useful for checking detection quality.
- **`/duplicates forget thread_id:<id>`** — delete a post's stored duplicate data and
  never index it again, for example on a member's deletion request.
- **`/duplicates link [thread_id]`** — run the new-post check on one existing post
  and, if duplicates are confirmed, cross-link the group (posting or updating
  notices). Good for testing on a single post.
- **`/duplicates link_all forum:<forum> mode:<preview|post>`** — run the check for
  every indexed post in a forum in the background (`INDEX_CONCURRENCY` at a time,
  with `/duplicates stop`). `preview` posts and stores nothing and reports the
  groups it found with titled links. `post` first finds all groups, then writes
  each post's notice once. Stopping before the end posts nothing. Costs about
  $0.0005 per post (roughly $1.40 for 2,750 posts); each candidate is read from
  Discord once per run and kept only in memory. Linking an archived post bumps it
  once; consider `DUPLICATE_CROSS_LINK=active` to leave archived posts alone.

Each group allows one batch or full run per forum at a time; a tagging run and an
indexing run can run side by side. Tagging uses `TAG_CONCURRENCY` (default 4) and
indexing uses `INDEX_CONCURRENCY` (default 10), because embeddings are much faster
and cheaper than classification: indexing handles about 13 posts per second, so
2,750 posts take roughly 3½ minutes.

Each examined post costs one AI call. Tagging one post takes about 8 seconds, so
with the default of 4 in parallel,
1,000 posts take roughly 35 minutes. Raising `TAG_CONCURRENCY` speeds up full runs
but sends more requests to Discord and OpenRouter at once; Discord rate limits are
handled automatically by waiting. New posts share the same slots, so during a full
run they wait at most for one post to finish.

Locked threads are never changed. Archived threads are changed only by
`/autotag all`; new posts, `/autotag thread`, and `/autotag active` skip them.
Classification uses
the title and up to 6,000 characters of the **starter message**, not replies,
attachment contents, embeds, or linked pages. An image-only post is classified
from its title.

If a post's starter message was deleted (Discord shows "Original message was
deleted"), the bot uses the post's title alone, which stays visible on Discord. Titles
under three words ("Bug", "Question") say too little, so those posts are skipped.
Replies are never used instead. New posts first retry for about 15 seconds, because
Discord can make a new starter readable a few seconds after the post appears; older
posts are not retried.

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

## Duplicate detection

Off by default. To enable it, generate a key and add three lines to `.env`:

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```sh
DUPLICATES=on
DUPLICATE_DB_KEY=<the generated key>
```

Back up the key: without it the index cannot be read, and a new key means rebuilding
the index. Update your Privacy Policy and tell members before enabling it.

- **New posts:** detection works in two steps.
  1. **Shortlist:** the title and up to 6,000 characters of the starter message are
     turned into an embedding (via OpenRouter, with the same ZDR routing) and
     compared with other indexed posts: by default in every configured forum, so
     an app request can match a feedback post (`DUPLICATE_SCOPE=forum` limits this
     to the same forum). Up to `DUPLICATE_CANDIDATES` posts scoring at least
     `DUPLICATE_THRESHOLD` are shortlisted. Groups can span forums, and each post's
     notice links to posts in either forum.
  2. **AI check:** the bot reads each candidate's title and up to 1,000 characters
     of its starter live from Discord (nothing extra is stored) and asks
     `OPENROUTER_MODEL`, with the same ZDR routing and strict structured output,
     which candidates ask for or report the same thing. Related but different
     requests are rejected.

  The new post and its confirmed duplicates (up to `DUPLICATE_MAX_MATCHES`) form a
  group. If the AI check fails, nothing is posted. Candidates whose post no longer
  exists are dropped and removed from the index. This costs one AI call per new post
  with candidates, about $0.0005.
- **Cross-linking:** every post in a group gets one "🔁 Related posts" message from
  the bot listing all the others with their live titles, newest first (up to 10,
  then "…and N more"). When the group grows, the bot **edits** its existing
  message instead of posting again, so older posts are not spammed. Archived posts
  are unarchived, updated, and archived again; posting a post's first notice
  bumps it once in "recent activity", later edits do not. Locked posts are never
  posted in. If a moderator deletes a notice, the bot does not post it again in
  that post. Deleting a post removes it from the other posts' notices, and a notice
  with nothing left to list is deleted. Mentions are disabled. Set
  `DUPLICATE_CROSS_LINK=active` to leave archived posts alone, or `off` to post only
  in the new post. Existing posts are cross-linked when a new duplicate arrives or
  through `/duplicates link` and `/duplicates link_all`; indexing never posts.
- **Existing posts:** run `/duplicates index` once per forum to build the index.
- **Stored data:** per post, only the post ID, forum ID, embedding model name, and
  the embedding vector, encrypted with `DUPLICATE_DB_KEY`; for cross-linking, which
  post IDs are related and the ID of the bot's notice message in each. No titles,
  text, usernames, or author IDs. Embeddings are derived from post text, so treat the file
  as personal data.
- **Kept in sync:** deleting a post on Discord deletes its entry. Deleting its
  starter message replaces the entry with one computed from the title alone (or
  deletes it if the title is under three words), so no stored vector reflects
  deleted text. Editing the starter re-indexes it; if re-indexing fails, the entry
  is deleted. On startup, entries for forums no longer
  in `FORUM_CHANNEL_IDS` and vectors from a different `EMBEDDING_MODEL` are deleted.
  `/duplicates forget` handles deletion requests. Title edits are picked up only when a
  post is re-indexed. Deletions that happen while the bot is offline are not seen.
- **Why the AI check:** on real posts, embedding scores could not separate
  duplicates from related requests. Email-client requests scored 50–59% with each
  other while unrelated app ideas scored 48–52%. The low floor only removes clearly
  unrelated posts; the AI check makes the final call. Use `/duplicates similar` on
  real posts to review its verdicts.

Enabling it also turns on the non-privileged Guild Messages intent so the bot sees
starter edits and deletions; message content is never cached or logged.

## Recovery and history

The bot keeps an action journal in `STATE_DB_PATH` (IDs and timestamps only, never
titles or message text) so it can recover from restarts and undo its own changes.

- **Runs:** every command that changes things (`/autotag all`, `active`, `thread`,
  `/duplicates index`, `link`, `link_all`) is recorded as a numbered run with its
  options, who started it, and which posts it has finished.
- **Actions:** every tag change (old and new tag IDs), every duplicate link added,
  and every related-posts notice posted, edited, or deleted (message IDs).
- **Resume:** if the bot stops during `/autotag all`, `/duplicates index`, or
  `/duplicates link_all`, it continues the run after restarting, skipping posts it
  already finished, and sends the summary to whoever started it by DM. Runs stopped
  with `/... stop` are not resumed. Short commands that were interrupted are marked
  interrupted; their changes stay undoable.
- **Catch-up:** the bot records when it was last online. On startup it handles
  posts created since then (tagging and duplicate checks) that it has not handled
  yet, like new posts. The very first start does not catch up.
- **Undo:** the `/history` commands, for allowed users only:
  - **`/history runs [limit:10]`** — recent runs with their status and changes.
  - **`/history thread [thread_id]`** — the bot's recent actions on one post.
  - **`/history undo run:<id> [confirm:True]`** — undo one run's changes.
  - **`/history undo_recent minutes:<n> [confirm:True]`** — undo every bot change
    from the last *n* minutes (up to 24 hours), including new-post tagging.
  - **`/history undo_thread [thread_id] [confirm:True]`** — run inside any post to
    undo everything the bot did there: restore its recorded tag changes, remove its
    duplicate group links, and delete the bot's messages in this post **and every
    post it was cross-linked with**, the same way they were posted (one per post).
    Messages are found by scanning each post, so older, unrecorded bot posts are
    removed too; members' messages are never touched. Archived posts are reopened
    briefly and archived again; locked posts are left alone. A linked post that is
    also part of another group keeps its notice, updated to drop this group.

  Without `confirm:True`, undo only shows what it would revert. Tags are restored
  only where they still match what the bot left, so later manual edits are never
  overwritten (reported as "left alone"). Undoing duplicate links removes them and
  updates or deletes the affected related-posts notices. Each undo is itself
  recorded as a run.

Journal entries older than `ACTION_RETENTION_DAYS` are deleted on startup. With
`ACTION_RETENTION_DAYS=0` nothing is deleted automatically, so runs stay undoable
indefinitely; the operator remains responsible for deleting data that is no longer
needed or that a user asks to have removed, as Discord's Developer Terms require.
Keep `artcraft_state.db` with the bot when moving it to another server.

## Privacy and retagging boundaries

Each OpenRouter request includes:

```json
{"provider": {"zdr": true, "data_collection": "deny", "require_parameters": true}}
```

The bot sends the title, starter text, and eligible tag names/IDs to OpenRouter and
the selected inference provider. No author IDs, message IDs, attachment downloads,
or chat history are intentionally included (the text itself can contain personal
information). Data exists transiently in process memory. Message text is never
written to disk or logged, and prompts and responses are not logged. The only
application storage is the optional, encrypted duplicate index described above.

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

The bot never processes the same post twice at once, limits how many posts it
processes in parallel, and fetches fresh tags before replacing them.
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
