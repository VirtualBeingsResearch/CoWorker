# First Run

[简体中文](README.md) · English

[← Back to the documentation index](../README.en.md)

This guide connects runtime installation, management setup, service
verification, and client selection into one journey. At the end you will have a
Coworker instance that listens only on the local machine, has a configured
model, and can receive messages.

## 1. Choose how to run it

| Method | Best for | Main requirements |
|---|---|---|
| From source | Local evaluation, development, and code changes | Python 3.13+, uv; Chromium for browser tools |
| Docker Compose | Isolation and prebuilt dependencies | Docker; the default is the strict offline runtime image |
| Desktop | A local collaboration workbench | A separately running Coworker service is still required |

Coworker Desktop is not an installer for the Coworker service. It connects to an
existing instance and integrates Local, Codex, and Claude Code conversations.

Current PyTorch builds do not provide an Intel macOS wheel. Run the Coworker
service through the [Dev Container](../development/development.en.md#dev-container)
or Docker. Desktop itself can still use the Intel macOS package.

## 2. Start Coworker

### Run from source

```bash
git clone https://github.com/VirtualBeingsResearch/CoWorker.git
cd CoWorker
uv sync
uv run playwright install chromium
uv run coworker
```

`uv run python -m coworker` is equivalent. You do not need to create `.env`
before the first evaluation run.

### Use Docker Compose

```bash
git clone https://github.com/VirtualBeingsResearch/CoWorker.git
cd CoWorker
docker compose up --build
```

Compose stores the Git workspace, runtime state, and model cache in separate
persistent volumes. Removing a container does not remove that data. Do not
delete volumes as a first response to an ordinary startup problem.

After startup, the default management URL is
<http://127.0.0.1:8000/admin>. The API binds to `127.0.0.1` by default. Do not
publish port `8000` directly to the public internet.

## 3. Get the administrator token

When no administrator token exists, the first start:

1. creates a random token;
2. prints the effective token in the terminal;
3. stores it in `data/admin_config.json`.

Open the management URL and enter the token. It can read and modify runtime
configuration. Do not send it through chat, commit it to Git, or place it in a
shared document. Tokens and model API keys stored in configuration files rely
on operating-system permissions and disk encryption.

## 4. Complete the setup wizard

Until setup is complete, Coworker starts only the management HTTP service. It
does not start the Agent loop, message polling, or external Channels. Complete:

1. runtime language;
2. maximum output tokens per response;
3. Provider and startup model;
4. the API key and Base URL when required;
5. whether to enable Passive mode;
6. the final review and save.

Models in the recommended catalog declare tool-calling support. When you enter a
model outside the catalog, confirm that the model and API gateway support
tool/function calling. The wizard does not run an online capability probe that
might incur charges.

After saving, Coworker performs a clean restart. A brief page disconnect is
normal. Wait for reconnection instead of saving repeatedly or starting
additional processes.

## 5. Confirm the instance is ready

After setup, check that:

- Life Overview no longer reports first-time setup;
- the current model and runtime state are correct;
- Diagnostics and Audit does not show a continuously growing set of failed
  tasks;
- the terminal is not repeating the same startup error.

You can also request status:

```bash
curl http://127.0.0.1:8000/status
```

Then send the first message:

```bash
curl -X POST http://127.0.0.1:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "alice", "content": "Hello, who are you?"}'
```

If the response is an authentication error, confirm whether the endpoint
requires a communication token. Do not work around it by exposing the service
or disabling production authentication.

## 6. Complete identity and daily settings

Open the [Management Console](../guides/README.en.md):

- set name, current location, and personality in Identity Profile;
- inspect main, summary, vision, and fallback models in Model Orchestration;
- review memory, Agent, API, and Channel settings in Runtime Settings;
- inspect or create Skills, Palaces, and subconscious modes in Capability
  Content;
- use Runtime Center for tasks, alarms, logs, backups, and safe restart.

Changing runtime language, a Provider, or some low-level settings may require a
restart. The console distinguishes immediate changes, saved changes, and
restart-required settings.

## 7. Choose an entry point

- Daily care and configuration: continue with the Web identity page and
  management console.
- Local Codex or Claude Code collaboration: install and configure
  [Coworker Desktop](../channels/desktop.en.md).
- Programs and automation: use [API and Channels](../channels/api-and-channels.en.md).
- Personal WeChat: use [Weixin Claw](../channels/weixin-claw.en.md).
- Remote Desktop over the public internet: deploy a
  [self-hosted Relay](../operations/relay.en.md) first.

## Next steps and recovery

Runtime data lives in `data/` by default, while user capability content normally
lives in `.coworker/`. Before long-term use, read
[Data and Trust Boundaries](../architecture/data-boundaries.en.md) and establish
a backup policy for both the workspace and runtime state.

When startup, setup, model calls, or client connections fail, start with
[Troubleshooting](../operations/troubleshooting.en.md). Do not immediately
delete `data/`, configuration, or Docker volumes.

[← Back to the project home](../../README.en.md)
