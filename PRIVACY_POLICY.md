# ArtCraft AI Auto-Tag Bot — Privacy Policy

Last updated: October 16, 2026

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

The bot does not intentionally include replies, attachment contents, embeds, or
linked-page contents in classification. Discord API responses can contain extra
message metadata, which may be present transiently in process memory. The bot does
not download attachments or browse links.

## External services

OpenRouter routes classification requests to an inference provider. Every request
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

The bot has no message database, does not write message contents to files, disables
discord.py's message cache, and does not intentionally log prompts or AI response
bodies. Classification data is used transiently in memory rather than kept as an
application history.

Operational logs may include thread IDs, connection status, action counts, error
types, and stack traces for unexpected errors. Stack traces show code locations and
error messages; event arguments and message contents are not logged. These logs are written to the process output. The bot does not set a log
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

There is no stored message database in the bot to search or erase, but the operator
must assess any retained operational logs and hosting records and handle requests
under applicable law. Requests concerning Discord or OpenRouter records may also
need to be directed to those services. Depending on your location, you may have
the right to complain to your local data-protection authority.

The bot has no per-member opt-out command. Server administrators can disable a forum
in configuration and restart the bot, revoke access, or remove the bot entirely to
stop future processing. Do not post sensitive information in enabled forums.

For software questions, use
[GitHub Issues](https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot/issues).
Those issues are public; do not post private message contents or personal details.
Direct private requests to the operator instead. Changes to this policy are dated
above and recorded in the repository history.
