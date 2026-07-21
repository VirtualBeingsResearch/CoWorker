# Coworker Desktop

[中文](desktop.md) · English

[← Back to Channels and Clients](README.en.md)

Coworker Desktop is a local collaboration workbench. It brings the local user, Codex, Claude Code, and one or more Coworker instances into one interface while keeping their identities, projects, and conversation contexts distinct. You can inspect connection health, switch actors, resume existing conversations, and deliberately send results to Coworker when needed.

## Desktop at a glance

![Coworker Desktop conversation workspace showing a local user, Codex, Claude Code, and Coworker](../assets/screenshots/desktop-conversations-en.png)

<p align="center"><sub>Manage runtime health and Coworker instances on the left, switch actors and conversations in the middle, and inspect messages and tool activity on the right.</sub></p>

This screenshot uses isolated synthetic demo data and contains no real users, secrets, conversations, or runtime records.

The local user, Codex, and Claude Code appear as three independent `actor` identities. A `participant` selects the target identity, while `actor` and `conversation_id` jointly address a session. Ordinary AI `final` output remains in the local session; only an explicit `send_to_coworker` call notifies Coworker.

Coworker Desktop has two distribution and runtime modes:

- **CLI**: `coworker-desktop`, suited to development, scripting, services, and troubleshooting.
- **Desktop application**: `apps/coworker-desktop/desktop`, built with Tauri. It provides configuration, start/stop controls, status, logs, and diagnostics and is distributed as an installer for ordinary local users.

The desktop application does not bundle the Coworker Python service, Codex CLI, or Claude Code CLI. Its only configuration entry point is the schema-v2 `coworker_desktop.json`. Codex and Claude are health-checked independently; either can be missing without preventing local chat or other available actors from starting.

## On this page

- [Running the CLI](#running-the-cli): configure and start the bridge.
- [Running and packaging the desktop application](#running-and-packaging-the-desktop-application): development and installer builds.
- [Product version management](#product-version-management): synchronize versions and generate changelog entries.
- [Desktop automatic-update releases](#desktop-automatic-update-releases): signing, manifests, and releases.

## Running the CLI

1. Prepare the bridge configuration:

```bash
cargo run --bin coworker-desktop
```

If the current directory has no `coworker_desktop.json`, the CLI launches the first-run setup wizard. Production mode is the default and requires HTTPS plus a Bearer token for every Coworker. Authentication-free HTTP is allowed only for local debugging after explicitly setting `security.development_mode=true`.

You can also copy the example configuration manually or select another file with `--config`:

```bash
cp coworker_desktop.json.example coworker_desktop.json
cargo run --bin coworker-desktop -- --config coworker_desktop.json
```

Example with multiple Coworker instances:

```json
{
  "schema_version": 2,
  "desktop_id": "desktop-local",
  "display_name": "My Desktop",
  "storage_dir": "data/coworker_desktop",
  "coworkers": [
    {
      "coworker_id": "cw_01",
      "display_name": "Partner A",
      "base_url": "https://coworker.example.com",
      "bearer_token": "replace-with-a-long-random-token"
    },
    {
      "coworker_id": "cw_02",
      "display_name": "Partner B",
      "base_url": "https://coworker-2.example.com",
      "bearer_token": "replace-with-another-long-random-token"
    }
  ],
  "actors": {
    "local": {"enabled": true},
    "codex": {
      "enabled": true,
      "codex_id": "codex-local",
      "snapshot_thread_limit": 20,
      "snapshot_scan_thread_limit": 200
    },
    "claude": {"enabled": true}
  },
  "security": {"development_mode": false}
}
```

For local HTTP debugging, first confirm that the service listens only on a loopback address. Then change the setting manually to `"security": {"development_mode": true}` and set `API__DEVELOPMENT_MODE=true` on Coworker as well. Never use this configuration on a shared network.

The configuration must use `schema_version=2` and contain a non-empty `coworkers` array. Legacy top-level Coworker fields are not used when generating a new configuration.

The bridge writes both console and file logs. Both levels default to `INFO`. Files are written daily as `logs_dir/coworker_desktop.YYYY-MM-DD.log`, with the seven most recent files retained. These logs cover startup, SSE, commands, threads, approval and user input, dynamic tools, and forwarding failures. The Tauri desktop application always uses the operating system's application log directory so switching configurations does not redirect reads and writes to different locations.

`desktop.actor.snapshot` scans at most `snapshot_scan_thread_limit` actor conversations, then writes them by project to `projects[].recent_conversations`, actively showing at most `snapshot_thread_limit` conversations per project. Snapshots no longer expose a flat `conversations` array; query the full list passively through `communicate(..., extra={"operation":"list_conversations"})`.

When a Codex turn ends with status `interrupted`, the bridge starts one continuation automatically by default using `auto_continue_interrupted_message`. A single thread can retry at most `auto_continue_interrupted_max_attempts` times in a row. Set `auto_continue_interrupted_turns=false` to disable this behavior completely.

2. Start Coworker first, then start the bridge:

```bash
uv run coworker
cargo run --bin coworker-desktop
```

After Desktop starts, it registers a `coworker-desktop` participant only for identities whose health check passed and begins periodic `desktop.actor.snapshot` publication. Each actor scans recent conversations once per cycle, preferring native project identifiers for grouping and limiting the number actively displayed per project. Conversations without a project are grouped under `“对话”` (“Conversations”). Identical snapshots are not republished, although a recovery heartbeat is sent at least once every five minutes. A publication failure for one Coworker does not block the others. Coworker writes the three identities' connection status and project conversations into pinned context and automatically loads the `coworker-desktop` Skill for Desktop-originated messages. The complete list remains available through `list_conversations`.

## Running and packaging the desktop application

The Tauri desktop application lives in `apps/coworker-desktop/desktop`, with its Rust entry point in `apps/coworker-desktop/desktop/src-tauri`. The CLI and bridge core live in `apps/coworker-desktop/bridge`.

## Product version management

The root `VERSION` file is the single source of truth for the product version. The Coworker Python package, Rust workspace, Coworker Desktop, web packages, and Tauri configuration must all match it.

```bash
# Update VERSION and manifests/package-lock top-level versions, then add changelog entries
uv run python scripts/bump_version.py 0.2.0

# Run the same check as CI; tag builds also verify coworker-desktop-vX.Y.Z against VERSION
uv run python scripts/check_version.py
```

`bump_version.py` first collects commits after the most recent `coworker-desktop-vX.Y.Z` tag for `CHANGELOG.md`. If no tag exists, it falls back to commits after the last change to `VERSION`. A version section with manually written content is not overwritten. After reviewing `CHANGELOG.md`, push a `coworker-desktop-vX.Y.Z` tag to trigger the desktop build.

Run in development:

```bash
cd apps/coworker-desktop/desktop
npm install
npm run tauri -- dev
```

Build the frontend:

```bash
cd apps/coworker-desktop/desktop
npm install
npm run build
```

Desktop packaging must run on the target platform or a corresponding build machine. `npm run build` creates only the frontend static assets; installers require a Tauri build.

```bash
cd apps/coworker-desktop/desktop
npm run tauri -- build
```

Select bundles explicitly by platform:

```bash
# Windows build machine: create an NSIS installer
npm run tauri -- build --bundles nsis

# macOS build machine: create Apple Silicon .app/.dmg and updater .app.tar.gz/.sig files
npm run tauri -- build --bundles app,dmg --target aarch64-apple-darwin

# macOS build machine: create Intel x86_64 .app/.dmg and updater .app.tar.gz/.sig files
npm run tauri -- build --bundles app,dmg --target x86_64-apple-darwin

# Linux build machine: create AppImage and deb packages
npm run tauri -- build --bundles appimage,deb
```

The current Tauri configuration declares Windows `nsis`, macOS `dmg`, and Linux `appimage`/`deb` bundle targets. macOS automatic updates also require building the `app` target explicitly; a dmg-only build does not produce the updater's `.app.tar.gz`. Tauri cannot create macOS or Linux installers directly on a Windows machine. Cross-platform packages should normally be generated on a matching build machine or CI runner.

Production macOS distribution requires Developer ID signing and notarization. Linux packaging requires the WebKitGTK, AppIndicator, and other system dependencies used by Tauri.

The recommended path is to generate artifacts for all three platforms with GitHub Actions:

```text
.github/workflows/coworker-desktop-release.yml
```

The workflow supports manual dispatch and runs when a `coworker-desktop-v*` tag is pushed:

- `windows-latest`: Creates the NSIS installer.
- `macos-latest`: Creates separate Apple Silicon `aarch64-apple-darwin` and Intel `x86_64-apple-darwin` `.app`, dmg, and updater artifacts. It signs and can notarize them when Apple secrets are configured.
- `ubuntu-22.04`: Creates AppImage and deb packages.

macOS signing and notarization do not require committing an Apple private key. Store the certificate and Apple credentials in GitHub Repository Secrets; the workflow imports the certificate into a temporary keychain on the macOS runner, which is destroyed with the runner after the build.

Signing secrets:

- `APPLE_CERTIFICATE`: Base64 content of the Developer ID Application `.p12` file.
- `APPLE_CERTIFICATE_PASSWORD`: Password used when exporting the `.p12`.
- `APPLE_SIGNING_IDENTITY`: Optional certificate identity name. When omitted, the workflow uses the first code-signing identity in the imported keychain.
- `KEYCHAIN_PASSWORD`: Optional password for the temporary CI keychain. When omitted, one is generated automatically.

App Store Connect API keys are recommended for notarization:

- `APPLE_API_ISSUER`: Issuer ID.
- `APPLE_API_KEY`: Key ID.
- `APPLE_API_KEY_P8_BASE64`: Base64 content of the `AuthKey_<Key ID>.p8` private-key file.

Apple ID app-specific passwords are also supported as fallback notarization credentials:

- `APPLE_ID`
- `APPLE_PASSWORD`
- `APPLE_TEAM_ID`

Without Apple secrets, the macOS job still produces an unsigned dmg, which is useful for verifying the build pipeline. When manually dispatching the workflow, disable `notarize_macos` to verify certificate import and signing without submitting a notarization request to Apple.

## Desktop automatic-update releases

The desktop application uses the Tauri v2 updater. Update signing cannot be disabled, so generate an updater key pair first. The checked-in `tauri.conf.json` contains a placeholder public key. Before a release build, the workflow uses `scripts/configure_tauri_updater.py` to inject the real public key and Coworker update endpoint. Keep the private key only in CI secrets.

```bash
cd apps/coworker-desktop/desktop
npm run tauri -- signer generate -- -w ~/.tauri/coworker-desktop.key
```

GitHub Secrets:

- `TAURI_SIGNING_PRIVATE_KEY`: The generated private-key path or its contents.
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: Optional private-key password.
- `TAURI_UPDATER_PUBLIC_KEY`: The generated public key.
- `TAURI_UPDATER_ENDPOINT`: For example, `https://coworker.example.com/api/desktop-updates/{{target}}/{{arch}}/{{current_version}}`. You may also provide only `https://coworker.example.com`; the script appends the updater path and Tauri placeholders automatically.

A Tauri release build generates updater assets and `.sig` files. Typical assets are:

- Windows: `*.exe` and `*.exe.sig`
- macOS Apple Silicon: `target/aarch64-apple-darwin/release/bundle/macos/*.app.tar.gz` and `.sig`
- macOS Intel x86_64: `target/x86_64-apple-darwin/release/bundle/macos/*.app.tar.gz` and `.sig`
- Linux: `*.AppImage` and `*.AppImage.sig`

If the updater files for both macOS architectures are named `app.tar.gz`, the upload API stores them as `darwin-aarch64-app.tar.gz` and `darwin-x86_64-app.tar.gz` to avoid collisions in the version asset directory and download URLs. Filenames that already contain an architecture identifier remain unchanged.

The Coworker server maintains a release directory and `latest.json`. Configure it with:

```env
DESKTOP_UPDATES__DIR=data/desktop_updates
DESKTOP_UPDATES__ADMIN_TOKEN=change-me
```

Upload signed assets and publish releases through the administration interface.

The Desktop Release section in `examples/api_test.html` can also create a release manually, upload one platform asset at a time, publish, or roll back.

At startup, the desktop application requests `GET /api/desktop-updates/{{target}}/{{arch}}/{{current_version}}`. A response of `204` means no update is available. When an update exists, the endpoint returns the `version`, `url`, and `signature` required by the Tauri updater.

After an operator calls `publish` or `rollback`, the server also sends a check-for-updates request over existing Desktop SSE connections to online clients that support `desktop_update_push`. The `push.eligible` and `push.enqueued` fields in the publish response count eligible desktops and those placed in the online SSE queue; they do not indicate offline delivery. If the bridge is stopped, the client is offline, or the desktop application has exited, the push is not retained. The next startup performs the check through the updater endpoint above. A push triggers only a signed update check and never installs automatically; the desktop application restarts only after the user approves installation.

1. Coworker sends a message through `communicate` to the `coworker-desktop` participant represented by a Desktop snapshot. `conversation_id` selects a conversation under that actor; omitting it creates a new conversation. If `extra.project_path` / `extra.cwd` is omitted, a new Codex thread starts as a no-project chat. If provided, it is sent to the Codex app-server as the project working directory.

Create a thread:

```python
communicate(
    participant_id=desktop_participant_id,
    message="Please review this change.",
    extra={"mode": "default", "origin_participant_id": "alice", "project_path": "D:/work/repo"},
)
```

Continue an existing thread:

```python
communicate(
    participant_id=desktop_participant_id,
    conversation_id="thr_xxx",
    message="Please continue investigating this issue.",
)
```

Send an attachment:

```python
communicate(
    participant_id=desktop_participant_id,
    conversation_id="thr_xxx",
    message="Please review this file.",
    attachments=[{"path": "reports/notes.md"}],
)
```

`extra.mode` is optional and accepts `"plan"` or `"default"`. The bridge resolves available modes from the Codex app-server mode list and sends the selection only with `turn/start`. If the thread already has an active turn, Codex must append the input instead, so that message cannot switch the mode at the same time.

Query the thread list passively without expanding the proactive snapshot:

```python
communicate(
    participant_id=desktop_participant_id,
    extra={
        "operation": "list_conversations",
        "filter": {"project_id": "coworker", "limit": 50},
    },
)
```

`limit` defaults to 50. Results are grouped under `projects[].recent_conversations`; `conversation_scan.complete` and project-level statistics describe completeness.

Include experimental fields when refreshing the Codex app-server schema; otherwise, the schema omits `collaborationMode/list` and `turn/start.collaborationMode`:

```powershell
codex app-server generate-json-schema --experimental --out scratch\codex-app-schema
```

You can also queue a mode switch that takes effect on the next `turn/start`:

```python
communicate(
    participant_id=desktop_participant_id,
    conversation_id="thr_xxx",
    extra={"operation": "set_conversation_mode", "mode": "plan"},
)
```

The bridge wraps the message for Codex in a form similar to the following. `Coworker:<id>` is the target ID Codex must use when replying:

```text
[来自Coworker:cw_01][Partner A]的消息:
Please review this change.
```

4. Codex replies to Coworker:

- The bridge registers dynamic tools through the app-server's `dynamicTools`. Codex should call `list_coworkers()` to inspect reachable Coworker instances, then call `send_to_coworker(coworker_id, message, attachments=[...])` to send a message explicitly.
- Historical threads that cannot receive dynamic tools retain the frontmatter-based text tool. Parsed messages use the same Desktop v1 outbox/ACK delivery path.

Send a message to Coworker:

```markdown
---
type: coworker_tool_call
tool: send_to_coworker
to: cw_01
---
This content will be sent to Coworker.
```

List reachable Coworker instances:

```markdown
---
type: coworker_tool_call
tool: list_coworkers
---
```

The bridge has no natural-language fallback parser. An unknown `coworker_id` fails without sending anything. Newly created Coworker threads prefer dynamic tools to prevent duplicate delivery.

5. The Codex app-server requests input from Coworker:

- `desktop.actor.snapshot` uses only `projects[].recent_conversations`; each project contains statistics such as matched, shown, truncated, and complete.
- Command, file, and permissions approvals are reported to Coworker as `desktop.approval.requested`. The bridge denies them safely by default and never approves automatically. When Coworker review is explicitly enabled, it waits for a response through `communicate(..., extra={"operation":"resolve_request", ...})`.
- `item/tool/requestUserInput` and `mcpServer/elicitation/request` are reported as `desktop.user_input.requested`.

Approval configuration follows Codex's separation between permission boundaries and the reviewer: `permissions_mode` describes the boundary, while `approvals_reviewer` selects who reviews requests. The secure default is equivalent to “notify but deny”:

```json
{
  "permissions_mode": "read-only",
  "approvals_reviewer": "none",
  "approval_timeout_seconds": 300
}
```

Allow Coworker to review approval requests:

```json
{
  "permissions_mode": "workspace-write",
  "approvals_reviewer": "coworker",
  "approval_timeout_seconds": 300
}
```

The high-risk full-access mode bypasses review and approves directly:

```json
{
  "permissions_mode": "danger-full-access",
  "approvals_reviewer": "none"
}
```

With `approvals_reviewer=none`, the bridge does not wait for a review response: `read-only` and `workspace-write` reject approval requests immediately, while `danger-full-access` approves them directly. With `approvals_reviewer=coworker`, the request waits for Coworker's response and fails closed on timeout.

When `desktop.approval.requested` has no `decision` and `status=pending`, Coworker can respond with `response`. For example, resolve a command or file approval with:

```python
communicate(
    participant_id=desktop_participant_id,
    extra={
        "operation": "resolve_request",
        "server_request_id": "srv_approval_001",
        "response": {"decision": "accept"},
    },
)
```

For a permissions approval, return the app-server permissions response directly:

```python
communicate(
    participant_id=desktop_participant_id,
    extra={
        "operation": "resolve_request",
        "server_request_id": "srv_approval_002",
        "response": {
            "permissions": {
                "fileSystem": {"entries": []},
                "network": {"enabled": False},
            },
            "scope": "turn",
            "strictAutoReview": True,
        },
    },
)
```

Respond to `item/tool/requestUserInput` with an `answers` object. Each key must match a `params.questions[].id` from the request:

```python
communicate(
    participant_id=desktop_participant_id,
    extra={
        "operation": "resolve_request",
        "server_request_id": "srv_1",
        "answers": {"choice": {"answers": ["Option A"]}},
    },
)
```

Respond to `mcpServer/elicitation/request` with a `response` object. The bridge passes the object through unchanged to the Codex app-server:

```python
communicate(
    participant_id=desktop_participant_id,
    extra={
        "operation": "resolve_request",
        "server_request_id": "srv_2",
        "response": {"action": "accept", "content": {"ok": True}},
    },
)
```

Ordinary messages returned from Codex through the bridge enter the Coworker inbox with `sender_id=codex:<codex_id>`, `conversation_id=<thread_id>`, and `source=codex`.
