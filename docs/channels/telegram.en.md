# Telegram

[中文](telegram.md) · English

[← Back to Channels and Clients](README.en.md)

Coworker can use one or more Telegram Bots to receive private-chat, group, forum-topic, and channel
messages and send text or attachments to known chats. Each Bot has independent credentials, Bot API
endpoint, long-poll offset, and contact set, so the same Telegram chat reached through different Bots
never becomes ambiguous.

## Create and configure Bots

Create a Bot with [@BotFather](https://t.me/BotFather) and obtain its token. Never commit the token
to Git or expose it in logs or screenshots. Add multiple Bots under **Runtime Settings → Telegram**
in the administration console. Each instance needs a stable `instance_id`: it must start with a
lowercase letter, may contain lowercase letters, digits, `_`, or `-`, and is at most 32 characters.

For unattended deployments, put multiple instances in one JSON object in `.env`:

```dotenv
TELEGRAM__BOTS={"main":{"enabled":true,"display_name":"Main bot","bot_token":"replace-with-main-token","api_base_url":"https://api.telegram.org","local_mode":false,"poll_timeout_seconds":30},"work":{"enabled":true,"display_name":"Work bot","bot_token":"replace-with-work-token"}}
```

Each instance accepts these fields:

| Field | Default | Description |
|---|---|---|
| `enabled` | `true` | Run this Bot; disabling it stops only its long poll |
| `display_name` | Empty | Administrator-facing label |
| `bot_token` | Empty | Secret token issued by BotFather; the administration API and UI never echo its value |
| `api_base_url` | `https://api.telegram.org` | Telegram Bot API root; each Bot may use a different official, self-hosted, or proxied endpoint |
| `local_mode` | `false` | Interpret file paths using local Bot API Server semantics |
| `poll_timeout_seconds` | `30` | `getUpdates` long-poll timeout, from 1 to 50 seconds |

Set `api_base_url` to the root and do not append `/bot`; Coworker builds the Bot API and file API
URLs separately. A self-hosted [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api)
usually also needs `local_mode`. Adding, removing, enabling, disabling, or editing an instance in
the administration console is hot-applied and does not require a Coworker restart.

Telegram Bot API `getUpdates` and webhooks are mutually exclusive. If the token previously had a
webhook, call `deleteWebhook` first. See Telegram's
[receiving-updates documentation](https://core.telegram.org/bots/api#getting-updates).

## Chat discovery and participant IDs

Telegram participant IDs use the compact prefix:

```text
tg:<instance_id>:<chat_id>
```

Examples are `tg:main:123456789` and `tg:work:-1001234567890`. `instance_id` selects the configured
Bot and `chat_id` is Telegram's numeric chat ID; groups and channels commonly use negative values.
This namespace keeps a shared `chat_id` distinct when multiple Bots encounter it.

Coworker sends only to chats it has discovered. A user must message the Bot first, or the Bot must
be added to a group/channel and receive a message, before the complete ID appears in
`list_connections`. The shorthand `instance_id:chat_id` resolves exactly. A bare `chat_id` works
only when it belongs to one known Bot. Telegram does not let Bots initiate a private chat with an
unknown user.

For groups, Coworker preserves the sender ID, username, and display name in the inbound body so a
group participant is not mistaken for a single member. A forum topic's `message_thread_id` becomes
the `conversation_id`; pass it back to reply in that topic. A channel must grant the Bot the
administrator permissions needed to read and send posts.

If ordinary group messages are missing, check
[Privacy Mode](https://core.telegram.org/bots/features#privacy-mode) in BotFather. With Privacy Mode
enabled, a Bot typically receives only commands, replies, and messages relevant to it. Disable it
when the deployment requires all group messages.

## Messages and attachments

Inbound handling supports text, captions, photos, documents, video, audio, voice messages, and
animations. Attachments are downloaded only after channel access checks pass. One inbound file is
limited to 20 MiB and saved under Coworker's attachment directory; images and PDFs up to 10 MiB are
also supplied inline to the model. Outbound handling supports text, images, and files. Text is split
at Telegram's 4096-character limit, and one uploaded file is limited to 50 MiB.

Treat Telegram messages and attachments as untrusted external input that may contain prompt
injection or malicious files. Grant only the group/channel permissions the Bot needs, and restrict
sources with channel access rules:

```dotenv
CHANNEL_ACCESS={"telegram":{"inbound_allow":["tg:main:*"],"inbound_deny":["tg:main:-1009999999999"],"outbound_allow":["tg:main:*"],"outbound_deny":[]}}
```

Rules use the channel name `telegram`, while participants use the `tg:` prefix. Deny takes
precedence over allow. A rejected inbound message is dropped before attachments are downloaded,
the contact is recorded, or the Agent receives it; Coworker makes a best-effort attempt to return a
generic rejection notice to the original chat.

## State and troubleshooting

Each instance stores its offset and discovered contacts in
`MEMORY__DB_PATH/telegram/<instance_id>.json`; the Bot token is never stored there. When an
`instance_id` is changed to another Bot's token, Coworker detects the new Bot user ID and clears the
old offset and contacts so the new Bot cannot send to chats discovered by the previous one.

- Bot remains offline: check `enabled`, the token, `api_base_url`, and proxy/firewall access. Logs
  report polling failures periodically, and polling resumes automatically after recovery.
- No updates arrive: make sure the token has no active webhook, then check group Privacy Mode or
  channel permissions.
- A custom API can send but cannot download: make sure `api_base_url` is the service root and the
  reverse proxy forwards both `/bot...` and `/file/bot...`; enable `local_mode` for a local Bot API
  Server.
- One chat appears under multiple IDs: this is expected isolation for multiple Bots. Select the
  complete `tg:` ID for the intended Bot.

[← Back to project home](../../README.en.md)
