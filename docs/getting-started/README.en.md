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
| Run the Docker image directly | Fastest first evaluation | Docker; the image includes the source and runtime dependencies |
| From source | Development and code changes | Python 3.13+, uv; Chromium for browser tools |
| Docker Compose plus the current checkout | Run local source in the image environment | Docker; clone the repository first |
| Desktop | A local collaboration workbench | A separately running Coworker service is still required |

See [Platform Support and Component Compatibility](platform-support.en.md) for operating systems,
CPU architectures, Desktop artifacts, and protocol notes.

Coworker Desktop is not an installer for the Coworker service. It connects to an
existing instance and integrates Local, Codex, and Claude Code conversations.

Current PyTorch builds do not provide an Intel macOS wheel. Run the Coworker
service through the [Dev Container](../development/development.en.md#dev-container)
or Docker. Desktop itself can still use the Intel macOS package.

## 2. Start Coworker

Choose one of the following three ways to start the service.

### Run the Docker image directly (recommended for first use)

```bash
docker run --name coworker \
  -p 127.0.0.1:8000:8000 \
  -e API__HOST=0.0.0.0 \
  ghcr.io/virtualbeingsresearch/coworker:offline
```

You do not need to clone the repository. The image includes the Coworker source, Python
environment, Chromium, FFmpeg, and embedding model. Docker automatically creates data volumes for
the Git workspace, runtime state, and model cache. The command stays attached so you can read the
administrator token and logs. After stopping it with `Ctrl+C`, run `docker start -a coworker` to
start the same container again. Do not remove this container or run `docker rm -v` before recording
its volume names or creating a backup; see
[Inspect and back up a direct Docker run](../operations/backup-and-restore.en.md#run-the-docker-image-directly).
For long-running use, you can migrate to
[Long-running Deployment](../operations/deployment.en.md#docker-compose-plus-the-current-checkout)
and use the Compose configuration to manage volumes, restart policy, and backups explicitly.

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

### Run the current checkout in the image environment

```bash
git clone https://github.com/VirtualBeingsResearch/CoWorker.git
cd CoWorker
docker compose up --pull always --no-build
```

The published image supplies Linux, Python, Chromium, FFmpeg, and the preloaded embedding model.
The current checkout is mounted directly at `/app` as both the running source and the Agent
workspace. This avoids installing Python dependencies on the host or building an image for the
first run. Restart the container after source changes. When dependency or lock files change,
follow [Develop with the offline image](../development/development.en.md#develop-with-the-offline-image)
to rebuild the execution environment.

> [!WARNING]
> On startup, Compose points `/app/data` at the separate state volume. If the current checkout has
> a non-empty `data/` directory created by a source run, the entrypoint exits instead of replacing
> it. Follow [Migrate an existing checkout data directory](../operations/upgrading.en.md#migrate-an-existing-checkout-data-directory)
> to preserve and transfer that data; do not delete it merely to make startup succeed.

The `offline` image blocks automatic downloads of missing Hugging Face content and prevents the
startup initializer from cloning a workspace from a Git remote, but it is not a network sandbox.
Your configured model provider and user-authorized Agent tasks that use Git, search, a browser, or
integrations may still access the network. Do not delete volumes as a first response to an
ordinary startup problem.

After startup, the default management URL is <http://127.0.0.1:8000/admin>. The Docker commands
above bind the host side only to `127.0.0.1:8000`, and a source run also binds the API to
`127.0.0.1` by default. Do not publish port `8000` directly to the public internet.

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

![Coworker first-time setup wizard](../assets/screenshots/admin-first-run-en.png)

<p align="center"><sub>First-time setup wizard · Configure runtime language, Provider, and startup model.</sub></p>

Models in the recommended catalog declare tool-calling support. When you enter a
model outside the catalog, confirm that the model and API gateway support
tool/function calling. The wizard does not run an online capability probe that
might incur charges.

After saving, Coworker performs a clean restart. A brief page disconnect is
normal. Wait for reconnection instead of saving repeatedly or starting
additional processes.

## 5. Confirm the instance is ready

After setup finishes and the page reconnects, open <http://127.0.0.1:8000/>.
Use the “Chat with Coworker” entry in the lower-right corner of the identity
page:

1. On first use, enter your display name and select “Start chatting.”
2. Send “Hello, who are you?”
3. Receiving a reply confirms that the frontend, message channel, and current
   model are working.

The display name establishes your connection identity in this browser; it is
not administrator authentication. Web chat history is stored only in the
current browser and does not automatically follow you after browser data is
cleared or when you switch devices.

You can also open the [Management Console](../guides/README.en.md) and check
that:

- Life Overview no longer reports first-time setup;
- the current model and runtime state are correct;
- Diagnostics and Audit does not show a continuously growing set of failed
  tasks;
- the terminal is not repeating the same startup error.

<details>
<summary>Verify through the API in a headless environment or while
troubleshooting</summary>

First request status:

```bash
curl http://127.0.0.1:8000/status
```

Then send a message:

```bash
curl -X POST http://127.0.0.1:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "alice", "content": "Hello, who are you?"}'
```

If the response is an authentication error, confirm whether the endpoint
requires a communication token. Do not work around it by exposing the service
or disabling production authentication.

</details>

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

For a source run, runtime data lives in `data/` by default, while user capability content normally
lives in `.coworker/`. In a container, they live in the corresponding workspace and state volume.
Before long-term use, read [Data and Trust Boundaries](../architecture/data-boundaries.en.md) and
[Backup and Restore](../operations/backup-and-restore.en.md), then establish a backup policy for
both the workspace and runtime state.

When startup, setup, model calls, or client connections fail, start with
[Troubleshooting](../operations/troubleshooting.en.md). Do not immediately
delete `data/`, configuration, or Docker volumes.

[← Back to the project home](../../README.en.md)
