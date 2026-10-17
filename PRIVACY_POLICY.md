# ArtCraft AI Auto-Tag Bot — Privacy Policy

Last updated: October 17, 2026

## Scope and responsibility

This policy describes the unmodified ArtCraft AI Auto-Tag Bot published at
[PierrunoYT/artcraft-ai-auto-tag-bot](https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot).
The person or organization running a particular installation controls its Discord
credentials, hosting, forum selection, and OpenRouter account. Ask your server
administrators who operates the installation in your server. Publishing the source
code does not mean the repository maintainer receives data from every installation.

Operators must disclose their identity, contact details, legal basis for processing,
and any additional hosting or logging practices before enabling the bot. This
source-code policy does not replace those deployment-specific disclosures.

## Data accessed and purpose

The bot accesses the following to classify posts and enforce command permissions:

- Titles and starter-message text in configured forum channels.
- Available tag names and IDs, applied tags, and thread state.
- Server, channel, thread, and message IDs needed for Discord API requests.
- Command inputs and Discord-provided member/permission information needed to
  restrict moderator commands.

It sends the post title (up to 256 characters), starter text (up to 6,000 characters),
and available tag names/IDs to OpenRouter for classification. Usernames, author IDs,
server IDs, and thread IDs are not deliberately included in the AI prompt, but
message text and tag names may themselves contain personal information.

If the operator enables duplicate detection, the same title and starter text are
also sent to OpenRouter to compute an embedding (a list of numbers representing the
post's meaning). The purpose is to point authors of new posts to similar earlier
posts. Embeddings are compared with posts in the bot's configured forums (or only
the same forum, if the operator chooses). To confirm matches, the titles and up to
1,000 characters of the starter messages of up to 25 similar earlier posts from
those forums are read from Discord when a new post
arrives and sent to OpenRouter, with the same zero-data-retention routing, together
with the new post. This text is used only for that check and is not stored.

If a post's starter message has been deleted, only the title is used. The bot does
not intentionally include replies, attachment contents, embeds, or linked-page
contents in classification or embeddings. Discord API responses can contain extra
message metadata, which may be present transiently in process memory. The bot does
not download attachments or browse links.

## External services

OpenRouter routes classification and embedding requests to an inference provider. Every request
requires `provider.zdr = true` and `data_collection = "deny"`; requests fail rather
than retrying with weaker routing settings. No external tools or plugins are
requested. The bot does not train AI models using Discord messages.

[OpenRouter's ZDR policy](https://openrouter.ai/docs/guides/features/zdr) concerns
eligible inference endpoints. It does not mean that data stays inside Discord or
that no operational metadata is retained. OpenRouter may retain usage metadata,
such as model, token counts, latency, and cost. Operators must keep opt-in prompt
logging disabled and avoid account integrations or presets that enable additional
logging or external tools. Review [OpenRouter's Privacy Policy](https://openrouter.ai/privacy).

Discord continues to store and process messages, tags, and platform records under
its own [Privacy Policy](https://discord.com/privacy). Third-party services may
process data outside your country; deployment operators must assess any applicable
international-transfer requirements. Visiting this repository or filing an issue
also involves GitHub's own privacy practices.

## Storage and retention

The bot does not write message contents to files, disables discord.py's message
cache, and does not intentionally log prompts or AI response bodies. Classification
data is used transiently in memory rather than kept as an application history.

If duplicate detection is enabled, the bot stores, for each indexed post: the post
(thread) ID, the forum ID, the embedding model name, and the post's embedding,
encrypted at rest. To cross-link duplicates, it also stores which post IDs were
found to be related and the message ID of its "Related posts" notice in each post.
It does not store titles, message text, usernames, or author IDs.
The post ID links an entry to the Discord post, so this data is pseudonymous, not
anonymous, and is treated as personal data. An entry is deleted when the post is
deleted on Discord, when its forum is removed from the bot's configuration, or on
request. When only the starter message is deleted, the entry is replaced by one
computed from the post's title, which remains visible on Discord, or deleted if the
title is too short. Editing a starter message replaces its entry. Entries
are otherwise kept while the post exists and duplicate detection stays enabled. The
data is used only for duplicate detection; it is not shared, sold, or used to train
AI models.

To resume interrupted work, handle posts created while it was offline, and undo its
own changes, the bot keeps an action journal. It records the bot's runs (type, forum
ID, options, the user ID of the moderator who started it, start and end times, and
which post IDs were processed), each change the bot made (post ID, the tag IDs
before and after, duplicate links added, and the message IDs of its notices), the
IDs of new posts it has handled, and when it was last online. It contains no titles,
message text, or author information. Journal entries are deleted automatically
after a retention period set by the operator (90 days by default).

Operational logs record each action with a timestamp and may include thread and
forum IDs, the user IDs of people who run or are refused moderator commands, tag
names added or removed, connection status, action counts, error types, and stack
traces for unexpected errors. Stack traces show code locations and error messages;
event arguments, post titles, and message contents are not logged. These logs are
written to the process output. The bot does not set a log
retention period or automatically delete logs; the hosting operator controls whether
output is stored and for how long. Hosting diagnostics or modified configurations
can introduce additional retention and must be disclosed by the operator.

Tag changes remain in Discord until changed or removed. Stopping the bot does not
delete Discord content, undo tags, or erase records held independently by Discord,
OpenRouter, GitHub, or the hosting provider.

## Your choices and privacy requests

Contact the operator through your server administrators or private server support
channel to ask about access, correction, deletion, objections, or other applicable
privacy rights. Identify the affected server/thread privately and include only the
information needed to locate the request. Do not send passwords or API tokens.

The bot stores no message text. If duplicate detection is enabled, the operator can
delete a post's stored entry and exclude it from future indexing on request. The
operator must also assess any retained operational logs and hosting records and
handle requests under applicable law. Requests concerning Discord or OpenRouter records may also
need to be directed to those services. Depending on your location, you may have
the right to complain to your local data-protection authority.

Members can ask the operator to exclude specific posts from duplicate detection.
There is no self-service opt-out command. Server administrators can disable a forum
in configuration and restart the bot, revoke access, or remove the bot entirely to
stop future processing. Do not post sensitive information in enabled forums.

For software questions, use
[GitHub Issues](https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot/issues).
Those issues are public; do not post private message contents or personal details.
Direct private requests to the operator instead. Changes to this policy are dated
above and recorded in the repository history.
