# Weixin Claw

[中文](weixin-claw.md) · English

[← Back to Channels and Clients](README.en.md)

The Weixin Claw channel connects Coworker through Tencent's personal-Weixin iLink ClawBot API. It is separate from the WeCom intelligent bot: each personal account is authorized by QR code and receives its own long-polling task for direct messages.

## Adding and managing accounts

Open Weixin Claw under `/admin`, enable the channel, and choose Scan to connect Weixin. The administration page generates and displays a real QR-code PNG locally. After confirmation, credentials are written to the administration override and hot-reloaded without a restart. Repeat the flow to add multiple accounts, then rename, disable, or remove each account on the same page.

Every account has a stable UUID. Participant IDs use:

```text
weixin:<account_uuid>:<weixin_user_id>
```

This keeps cursor, context, and reply routing isolated even when the same contact is visible through two bound accounts. Tokens are masked in administration API responses and stored only in the administration configuration file.

## Agent-controlled pairing invitations

When a known direct-message participant explicitly asks to add Weixin Claw, the agent keeps using the generic `communicate` tool with `extra.channel_action` set to `{"channel":"weixin","type":"connect"}`:

1. the Channel Action Registry creates a one-time QR code for the selected known `participant_id`;
2. Coworker sends the QR-code PNG attachment and fallback link only to that direct chat, returning a connection `session_id` in the tool result;
3. another `communicate` call with `{"channel":"weixin","type":"poll","session_id":"..."}` checks the scan result; include `verify_code` when the phone requests a number;
4. after the scanner sends the first message from Weixin, the channel creates a new `weixin:*` participant and the agent decides how to recognize and organize its relationship with existing contacts.

The recipient `participant_id` controls only where the invitation is delivered. It is never bound at the system level to the new ClawBot or to later Weixin participants. Weixin connection is a registered generic channel action, not a dedicated model tool, and the backend rejects group-chat recipients. An administrator can still scan independently in the administration page. The agent decides whether to send an invitation and how to maintain contact relationships from conversation context.

Inbound Weixin Claw messages currently extract text and voice transcripts; images, files, and video reach the agent as localized placeholders. Normal outbound traffic is currently text-only. The pairing QR image uses the attachment support of the channel where the invitation originated; when that channel has no attachment support, the fallback link is still delivered.

Protocol compatibility follows Tencent's [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) implementation.
