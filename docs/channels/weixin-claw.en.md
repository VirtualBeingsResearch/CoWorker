# Weixin Claw

[中文](weixin-claw.md) · English

[← Back to Channels and Clients](README.en.md)

The Weixin Claw channel connects Coworker through Tencent's personal-Weixin iLink ClawBot API. It is separate from the WeCom intelligent bot: one iLink Bot instance can bind only one personal Weixin account and owns an independent long-polling task. One Coworker may create multiple independent Bot instances.

## Participants and connections

Each bound Bot instance is one communication participant:

```text
weixin:<bot_instance_id>
```

The Weixin-side user ID, credentials, cursor, and context token are internal protocol state owned by that instance and do not appear in the participant ID. `list_connections` also exposes a stable management endpoint:

```text
weixin:control
```

Their `ConnectionInfo.kind` values are `weixin:direct` and `weixin:control`; no additional participant role is needed.

## Agent-managed connections

Weixin pairing is not a dedicated model tool. When the channel is enabled, it contributes a short guide to the system prompt, and the agent continues to use the generic `communicate` tool.

Create a pairing session:

```json
{
  "participant_id": "weixin:control",
  "extra": {"action": "connect"}
}
```

The result returns a `session_id` and local `qrcode_path`. The QR code is not sent to any participant automatically. The agent selects a recipient from the current conversation and sends that path as an `image` attachment in a separate `communicate` call. To show connection status in the identity-card ChatDock, it may also include presentation-only metadata:

```json
{
  "connection_status": {
    "channel": "weixin",
    "status": "wait",
    "session_id": "..."
  }
}
```

The channel polls pairing in the background, so the agent does not repeatedly invoke a tool or
`list_connections`. To inspect the current state:

```json
{
  "participant_id": "weixin:control",
  "extra": {"action": "status"}
}
```

If the phone requests a verification code, submit it separately:

```json
{
  "participant_id": "weixin:control",
  "extra": {
    "action": "verify",
    "session_id": "...",
    "verify_code": "number shown on the phone"
  }
}
```

Confirmation yields a new `weixin:<bot_instance_id>`. The QR recipient is not bound to the new connection; the agent organizes contact relationships itself.

Remove a local connection only after an explicit user request and confirmation:

```json
{
  "participant_id": "weixin:control",
  "extra": {
    "action": "remove",
    "bot_instance_id": "...",
    "confirm": true
  }
}
```

Removal stops polling and deletes Coworker's local credentials and runtime state. It does not remotely revoke Weixin-side authorization or delete existing chat history.

## Administration and runtime behavior

Weixin Claw under `/admin` can also scan, rename, disable, or remove instances. The frontend uses the
generic `/api/admin/channels/{channel}/management` interface contributed by the module; the Admin
backend does not interpret Weixin commands. The backend owns an unfinished pairing session, so leaving and returning to the page restores its QR code and current state. Pairing sessions are temporary and do not survive a Coworker process restart.

Bound instances are stored in `MEMORY__DB_PATH/weixin_connections.json` rather than
`admin_config.json`. Successful empty polls and administration-page status reads are DEBUG-only. The first message-polling failure is WARNING, repeated retries are DEBUG, and recovery emits one INFO record. Authentication and protocol errors remain visible. Logs never include tokens, QR contents, context tokens, or message bodies.

The identity-page ChatDock keeps the QR image as chat presentation data in localStorage, so it survives page navigation without adding QR resources to the terminal chat interface.

Inbound Weixin Claw messages currently extract text and voice transcripts; images, files, and video reach the agent as localized placeholders. Normal outbound traffic is currently text-only.

Protocol compatibility follows Tencent's [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) implementation.
