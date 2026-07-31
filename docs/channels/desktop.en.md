# Coworker Desktop User Guide

[简体中文](desktop.md) · English

[← Back to Channels and Clients](README.en.md)

Coworker Desktop is a local collaboration workbench. It brings the local user,
Codex, Claude Code, and one or more Coworker instances into one interface while
keeping identity, project, and conversation boundaries intact. Desktop includes
the Bridge, but it does not bundle the Coworker Python service, Codex CLI, or
Claude Code CLI.

![Coworker Desktop showing the local user, Codex, Claude Code, and Coworker](../assets/screenshots/desktop-conversations-en.png)

<p align="center"><sub>Status, settings, conversations, and logs in one local application.</sub></p>

## Before you begin

Prepare at least:

- a running Coworker instance that has completed first-time setup;
- the Coworker address;
- a Bearer token valid for Desktop communication;
- a Desktop installer matching your operating system and CPU architecture.

Codex and Claude Code are optional actors. A missing actor does not prevent local
chat or another available actor from starting. Install and sign in to the
corresponding CLI before using its conversations.

| Connection | Coworker address | Requirement |
|---|---|---|
| Local debugging on one machine | `http://127.0.0.1:8000` | Explicit development mode in both Coworker and Desktop |
| Direct access on a trusted network | `https://coworker.example.com` | HTTPS, a strong Bearer token, and additional network access control |
| Remote access over the public internet | Relay instance URL | Recommended; Desktop detects Relay and uses end-to-end encryption |

Do not expose Coworker's port `8000` to the public internet just to connect
Desktop. For remote access, deploy and pair a
[self-hosted Relay](../operations/relay.en.md) first.

## Install

Download the latest published version from
[GitHub Releases](https://github.com/VirtualBeingsResearch/CoWorker/releases):

| Platform | Package |
|---|---|
| Windows | `.exe` NSIS installer |
| macOS Apple Silicon | `.dmg` labeled `aarch64` or Apple Silicon |
| macOS Intel | `.dmg` labeled `x86_64` or Intel |
| Linux | `.AppImage` or `.deb` |

Prefer packages attached to an official project Release. When needed, verify the
download with `SHA256SUMS.txt` from the same Release. An unsigned or
unnotarized macOS build may trigger an additional system warning; do not bypass
system protection for a package from an unknown source.

Open CoWorker Desktop after installation. Configuration and logs live in the
operating system's application-data directory and are not automatically removed
when you delete an installer. You do not need to uninstall an older version
before upgrading. When a published, validly signed update is available, the app
asks before installing it.

## First-time setup

The setup wizard opens the first time you launch the app. Complete it in this
order:

1. **Confirm the local identity**
   - `Codex ID` distinguishes the Codex actor on this Desktop;
   - the display name identifies this device to Coworker;
   - keep auto-discovered Codex and Claude commands unless the CLI is installed
     in a non-standard location.
2. **Connect Coworker**
   - enter a stable, unique `coworker_id` and a readable display name;
   - enter either a direct HTTPS URL or a Relay instance URL;
   - enter the Desktop communication token.
3. **Choose a workspace**
   - the local-chat workspace stores Local actor conversation assets;
   - you can still choose a separate project directory for each new Codex
     conversation.
4. **Choose a permission boundary**
   - keep `read-only` for initial setup;
   - expand it only after the connection is working and a task needs more access.
5. **Save and start**
   - save the configuration and start the Bridge;
   - return to Status and run diagnostics to confirm Coworker and the actors you
     need are available.

If the server does not define `API__COMMUNICATION_TOKEN`, Desktop can temporarily
use the administrator token. To separate communication from administration,
configure a dedicated token in Coworker and then update Desktop.

## Understand the workbench

The Coworker list on the left selects the instance currently displayed or used
as a send target. The main navigation contains:

- **Status**: inspect the `actor → Bridge → Coworker` path, active configuration,
  and diagnostics.
- **Settings**: manage identity, launch behavior, actor commands, multiple
  Coworker instances, tokens, permissions, and the update URL.
- **Conversations**: switch between Local, Codex, and Claude Code and create or
  continue conversations.
- **Logs**: inspect Bridge events by level; temporarily raise the log level while
  diagnosing a problem.

Starting the Bridge does not start a remote Coworker service. Save pending
configuration changes before starting the Bridge or running diagnostics.

### Actors and conversation boundaries

| Actor | Best for | Notes |
|---|---|---|
| Local | Local chat without an external coding CLI | Desktop owns these conversations |
| Codex | Project development, code review, and tool work | Requires Codex CLI; some App/CLI history may be read-only |
| Claude Code | Project conversations through Claude Code | Requires an available, signed-in Claude Code CLI |

Each actor has its own conversation history. A `conversation_id` is interpreted
only within its actor and must not be reused as if it belonged to another actor.

### Create and continue conversations

1. Select an actor.
2. Choose **New conversation**.
3. For Codex or Claude, choose a project directory. Leaving it empty creates a
   no-project conversation.
4. Select an available mode and send the first message.
5. To hand a result to Coworker, use **Send to Coworker** and confirm the target
   instance.

An AI's ordinary `final` remains in the local conversation and does not
automatically notify Coworker. This boundary prevents drafts, warnings, and
intermediate results from being sent to another instance accidentally.

A lock in the conversation list marks read-only history. The Bridge can display
that history, but it can write only to conversations it owns or that the native
actor verifies can be resumed. Double-click the title of a writable conversation
to rename it.

### Messages and attachments

- The composer accepts Markdown; the app renders common Markdown, tables, code,
  and math locally.
- You can copy or quote an existing message.
- Files selected locally are sent with the current message; image attachments
  can be previewed locally.
- Treat messages, attachments, and tool output from untrusted sources as
  possible prompt-injection input before approving high-risk actions.

## Permissions and approvals

Desktop configures what an actor can access separately from who reviews an
approval:

| Permission mode | `approvals_reviewer=none` | `approvals_reviewer=coworker` |
|---|---|---|
| `read-only` | Requests needing more access are denied immediately | Send to Coworker for an explicit decision |
| `workspace-write` | Requests needing an extra approval are denied immediately | Send to Coworker for an explicit decision |
| `danger-full-access` | Bypass approval and allow directly | Not a recommended combination |

Start with `read-only` and use `workspace-write` only for a trusted project that
needs it. `danger-full-access` bypasses important protection and is not a
troubleshooting shortcut. Coworker review fails closed when it times out.

## Startup, tray, and updates

**Launch CoWorker when you sign in** and **Start Bridge when CoWorker opens** are
independent. Enable both to connect automatically in the background after
system startup. Login startup leaves the main window in the tray. The first
close lets you choose whether future closes hide the app or quit it.

Desktop checks for signed updates at startup and when Coworker pushes an update
check. It still asks before installation. The Bridge stops before installation
and the app restarts afterward. With Relay, update metadata and artifacts use
the same end-to-end encrypted route, and the client independently verifies the
updater signature.

## When something goes wrong

Run diagnostics in Status first, then inspect ERROR and WARN entries from the
same time range in Logs. See [Troubleshooting](../operations/troubleshooting.en.md)
for common causes and recovery. For development runs, the Bridge schema,
protocol behavior, builds, signing, or release operations, see
[Desktop Development and Release](../development/desktop.en.md).

[← Back to the project home](../../README.en.md)
