# ArtCraft AI Auto-Tag Bot — Terms of Service

Last updated: October 17, 2026

## About the bot

ArtCraft AI Auto-Tag Bot is a Discord forum-tagging application maintained in the
[PierrunoYT/artcraft-ai-auto-tag-bot repository](https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot).
It is not affiliated with or endorsed by Discord or OpenRouter. These terms describe
use of the bot, not a grant of a software license or a promise of hosted service.

The bot classifies new forum posts and provides moderator commands to reclassify
individual posts, batches of active posts, or every post in a forum, including
archived posts. If the operator enables duplicate detection, the bot also replies in
new posts with links to similar earlier posts in the same forum. These suggestions
are automated and may be wrong; a suggested post is not necessarily a duplicate.

## Server administration and acceptable use

Only enable the bot in servers and forums you are authorized to administer.
Operators must inform affected members that selected forum titles and starter
messages are sent to an external AI service, and obtain any permissions or consent
required by applicable law and Discord's rules. Do not use the bot to process
sensitive or confidential information, violate others' rights, or bypass access
controls. Keep bot tokens and API keys private.

Use must comply with the [Discord Terms of Service](https://discord.com/terms),
[Community Guidelines](https://discord.com/guidelines),
[Discord Developer Terms of Service](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service),
and [Discord Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy),
as applicable. Discord's age and eligibility requirements apply.

## Automated changes and limitations

By default the bot adds tags and keeps those already applied. **If the operator
enables replace mode, the bot replaces member-selectable tags, including tags added
manually by members or moderators,** and a valid empty AI selection can remove them.
The bot never applies or removes moderator-only tags. AI classifications may be
inaccurate. Administrators are responsible for reviewing results and
correcting mistakes; the bot is not a substitute for human moderation.

The bot uses only tags already defined in the configured forums. Locked threads
are skipped. Archived threads are changed only when a moderator tags a whole forum;
they are briefly unarchived to apply tags and then archived again. There is no guarantee of availability, classification
accuracy, recovery after downtime, or compatibility with future third-party API
changes. Operators are responsible for their hosting and OpenRouter usage charges.

## Privacy

Read the [Privacy Policy](PRIVACY_POLICY.md) for the data processed, external
services involved, retention boundaries, and privacy-request instructions.
Self-hosted or modified installations may have different hosting, logging, and
provider settings; their operators must disclose those differences to users.

## Stopping use and support

Server administrators can stop processing by removing a forum from the
configuration and restarting the bot, revoking its channel access, or removing the
bot from the server. Removing the bot does not undo tag changes already made.

For deployment-specific concerns, contact your server administrators or the person
operating that installation. Report software issues or misuse concerns through
[GitHub Issues](https://github.com/PierrunoYT/artcraft-ai-auto-tag-bot/issues).
Issues are public: do not include credentials, private messages, or sensitive
personal information. For private deployment concerns, contact the operator
directly through your server's private support channel.

## Changes and mandatory rights

These terms may be updated as the bot changes; the current version and revision
history are available in this repository. Operators must notify their users of
material changes affecting their use. Nothing in these terms excludes rights or
responsibilities that cannot lawfully be excluded.
