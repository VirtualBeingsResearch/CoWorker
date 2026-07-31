<a id="readme-top"></a>

<div align="center">
  <img src="apps/coworker-desktop/desktop/src-tauri/icons/128x128@2x.png" width="128" alt="Coworker logo">
  <h1>Coworker</h1>
  <p><strong>A persistent virtual lifeform that perceives, remembers, acts, and grows</strong></p>
  <p>
    <a href="README.md">简体中文</a>
    <span> · </span>
    <strong>English</strong>
  </p>
  <p>
    <a href="https://github.com/VirtualBeingsResearch/CoWorker/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/VirtualBeingsResearch/CoWorker/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;logo=githubactions&amp;logoColor=white" alt="CI status"></a>
    <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.13+"></a>
    <a href="#quick-start"><img src="https://img.shields.io/badge/deployment-self--hosted-6f42c1?style=flat-square" alt="Self-hosted"></a>
    <a href="LICENSE"><img src="https://img.shields.io/github/license/VirtualBeingsResearch/CoWorker?style=flat-square&amp;color=2ea44f" alt="MIT License"></a>
    <a href="https://github.com/VirtualBeingsResearch/CoWorker/stargazers"><img src="https://img.shields.io/github/stars/VirtualBeingsResearch/CoWorker?style=flat-square&amp;logo=github&amp;label=stars" alt="GitHub stars"></a>
  </p>
  <p>
    <a href="#why-call-her-a-virtual-lifeform"><strong>Core idea</strong></a>
    <span> · </span>
    <a href="#what-she-does-for-a-team"><strong>Teamwork</strong></a>
    <span> · </span>
    <a href="#quick-start"><strong>Quick start</strong></a>
    <span> · </span>
    <a href="docs/README.en.md"><strong>Documentation</strong></a>
    <span> · </span>
    <a href="CONTRIBUTING.en.md"><strong>Contributing</strong></a>
  </p>
</div>

<br>

![Coworker Web identity page showing Aster's identity, current state, and profile](docs/assets/screenshots/web-identity-en.png)

<p align="center"><sub>Web identity page · Review Coworker's identity, current state, and profile.</sub></p>

Most AI tools appear when you ask a question and stop after the answer. Coworker stays present: she has her own identity and memory, uses real tools to get work done, can reflect in the background, and shows up where you already work—through APIs, WeCom, or Coworker Desktop.

She is not another chat window wrapped around a model. She is a **self-hosted, extensible agent runtime built to keep running**.

For an individual, she is a companion who stays present. For a team, she becomes a layer of **persistent context and execution**—carrying work across teammates, conversations, and days while connecting people with AI agents.

<table align="center">
  <tr>
    <td align="center"><strong>⏳ Persistent existence</strong></td>
    <td align="center"><strong>🧠 Memory continuity</strong></td>
    <td align="center"><strong>👁️ Perception and action</strong></td>
  </tr>
  <tr>
    <td align="center"><strong>🌱 Learning and growth</strong></td>
    <td align="center"><strong>🤝 Relationships and boundaries</strong></td>
    <td align="center"><strong>🧩 Self-hosted and extensible</strong></td>
  </tr>
</table>

> [!WARNING]
> Coworker is not a security sandbox. She can execute commands and read or write files with the permissions of the system user running the process.
> The current v0.x releases should only run locally or on a trusted network. Do not expose port 8000 to the public internet.
> See the [security policy](SECURITY.en.md) for details.

## One runtime, multiple ways in

Identity, memory, tasks, and tools all live in the same local-first runtime. Web, Desktop, and communication channels are complementary ways to observe Coworker, care for her, and work with her.

| Surface | Best for |
|---|---|
| **Web identity page and Care Station** | Review identity, current state, memory, Skills, models, and runtime activity, and handle day-to-day configuration. |
| **Coworker Desktop** | Put the local user, Codex, Claude Code, and Coworker in one workbench while keeping identities and conversations distinct. |
| **APIs, WeCom, and file channels** | Bring persistent context and execution into existing tools, services, and automation. |

![Coworker Desktop conversation workspace showing collaboration among a local user, Codex, Claude Code, and Coworker](docs/assets/screenshots/desktop-conversations-en.png)

<p align="center"><sub>Coworker Desktop · Switch among identities, projects, and conversations in one workbench.</sub></p>

<details>
<summary><strong>See Web usage and runtime details</strong></summary>

![Coworker Web usage page showing breakdowns by model, source, cache, and tool calls](docs/assets/screenshots/web-usage-en.png)

<p align="center"><sub>Web usage · Drill down from totals to models, sources, cache behavior, and tool calls.</sub></p>

</details>

## Why call her a “virtual lifeform”?

> **Coworker describes her relationship with people; “virtual lifeform” describes how she exists.**

This is not a claim that she is biologically alive or conscious. It is a product and architectural model: Coworker is not a stateless request handler, but a system that maintains identity, accumulates experience, perceives its environment, and acts across continuous time.

| Life-like quality | How Coworker implements it |
|:---:|---|
| **⏳ Persistent existence** | Runs in the background, receiving new events through a cycle of perception, thought, action, and sleep instead of disappearing after one request. |
| **🪪 Identity** | Maintains a name and personality under `data/identity/`, carrying the same sense of self across time, channels, and tasks. |
| **🧠 Memory continuity** | Compresses short-term context, retrieves long-term semantic memories, and restores conversations, alarms, and recent state after a restart. |
| **👁️ Perception and action** | Messages, files, and events act as inputs; tools for files, code, browsers, vision, and communication let her affect the environment. |
| **🌱 Learning and growth** | Accumulates experience through long-term memory, Skills, and memory palaces; optional Bubble and subconscious modes explore, reflect, and organize. |
| **🤝 Relationships and boundaries** | Recognizes different participants and their relationships while separate conversation threads keep teammates' short-term contexts from bleeding together. |

Coworker supports Anthropic, OpenAI, DeepSeek, Qwen, Zhipu, MiniMax, and other model services, with runtime model switching. For the full feature set and internal design, see [Core concepts and capabilities](docs/architecture/concepts.en.md).

## What she does for a team

As a virtual lifeform within a team, Coworker's value is not “one more chat window.” It is keeping important context and executable capability from disappearing inside one person's one-off conversation.

| Team moment | Her role | Team impact |
|---|:---:|---|
| Hand-offs, onboarding, or picking up an issue the next day | **Project memory** | Captures confirmed context, decisions, and experience as long-term memory, so the next collaboration starts with shared history instead of another retelling. |
| Research, investigations, reminders, and follow-ups across time zones | **Async operator** | Uses tools, preserves intermediate results, and schedules persistent reminders so work can move forward without everyone being online together. |
| Product, engineering, and multiple AI tools working together | **Collaboration hub** | Coworker Desktop connects local teammates, Codex, and Claude Code to exchange tasks and results, while `participant_id` keeps their conversation contexts separate. |
| Repeatable workflows and domain knowledge | **Team work interface** | Encodes ways of working as Skills, organizes domain context in memory palaces, and exposes them through APIs, WeCom, or file-based channels. |

A typical collaboration flow:

`Question in WeCom` → `Recall project context` → `Use tools or collaborate with Codex / Claude Code` → `Synthesize the result` → `Retain it as team memory`

> [!NOTE]
> `participant_id` provides conversation isolation, not enterprise-grade authorization or tenancy. The current v0.x releases are best suited to local or trusted small-team environments, with human review retained for consequential actions.

## Her life cycle

The sense of life comes from a real runtime loop, not just anthropomorphic language:

```mermaid
flowchart LR
    perceive["Perceive<br/>messages · files · events"] --> think["Think<br/>context · memory · reasoning"]
    think --> act["Act<br/>tools · communication · tasks"]
    act --> sleep["Sleep<br/>wait · reflect · recover"]
    sleep --> perceive
    foundation["Identity · memory · skills"] -.-> think
    foundation -.-> act
```

> **You:** “Continue yesterday's investigation, inspect the relevant code, remember the conclusion, and remind me in two hours.”

In a single request, Coworker can recover yesterday's context, use file and code tools to investigate, save durable conclusions to memory, and set a reminder that survives restarts. These are not isolated features—they are actions inside one continuous loop.

## Quick start

The fastest local evaluation path is to run from source. You need **Python 3.13+**,
[uv](https://docs.astral.sh/uv/), and access to a model service that supports tool/function
calling (usually with an API key). PyPI and wheel packages are not currently available.

### 1. Start Coworker

```bash
git clone https://github.com/VirtualBeingsResearch/CoWorker.git
cd CoWorker
uv sync
uv run playwright install chromium
uv run coworker
```

`uv run python -m coworker` is equivalent to the last command. You do not need to create `.env`
before the first run.

<details>
<summary><strong>Prefer Docker Compose?</strong></summary>

```bash
git clone https://github.com/VirtualBeingsResearch/CoWorker.git
cd CoWorker
docker compose up --build
```

Compose builds the strict-offline runtime image with its embedding model preloaded by default,
then persists the workspace, runtime state, and model cache separately. “Offline” here means the
runtime does not fetch missing content from Hugging Face or Git remotes; your configured
conversation-model provider may still require network access.

</details>

> [!NOTE]
> Intel macOS cannot install the current PyTorch wheel. Run the service through the
> [Dev Container](docs/development/development.en.md#dev-container) or Docker.
> On Debian or Ubuntu, use `uv run playwright install --with-deps chromium` if Chromium reports
> missing system libraries.

### 2. Complete first-time setup

On the first start, the terminal prints an auto-generated administrator token and stores it in
`data/admin_config.json`. Open <http://127.0.0.1:8000/admin>, enter the token, and use the wizard to:

1. choose the runtime language and maximum output tokens;
2. select a model Provider and startup model;
3. enter the API key and Base URL when required;
4. review and save the configuration.

![Coworker first-time setup wizard](docs/assets/screenshots/admin-first-run-en.png)

<p align="center"><sub>First-time setup wizard · Configure runtime language, Provider, and startup model.</sub></p>

Coworker restarts safely after you save. A brief page disconnect is normal. Treat both the
administrator token and model API key as secrets: do not send them through chat, commit them to
Git, or place them in shared documents.

### 3. Send the first message

After the page reconnects, open <http://127.0.0.1:8000/>. Use the “Chat with
Coworker” entry in the lower-right corner of the identity page. On first use,
enter your display name and select “Start chatting,” then send “Hello, who are
you?” Receiving a reply confirms that the frontend, message channel, and
current model are working.

<details>
<summary>Verify through the API in a headless environment or while
troubleshooting</summary>

```bash
curl -X POST http://127.0.0.1:8000/messages \
  -H "Content-Type: application/json" \
  -d '{"sender_id": "alice", "content": "Hello, who are you?"}'
```

</details>

From there, use the [Web management console](docs/guides/README.en.md) to refine
the setup, install [Coworker Desktop](docs/channels/desktop.en.md) to collaborate
with Codex or Claude Code, or connect your own tools through
[API and Channels](docs/channels/api-and-channels.en.md).

> [!TIP]
> For the complete journey through runtime choices, setup verification, client selection, and
> recovery, continue with the [First Run guide](docs/getting-started/README.en.md). See the
> [Configuration Reference](docs/operations/configuration.en.md) for Docker images, environment
> variables, and persistent volumes.

## Sync upstream source

Coworker can edit and commit the repository source directly, so your checkout may contain local
commits maintained by you or by her. Sync upstream regularly to keep the branches from drifting too
far apart. You can do this manually or ask Coworker to inspect and perform the sync; we recommend
the latter because she can first check the working tree, branch, and remotes, review incoming
changes, resolve straightforward conflicts, and run the relevant checks. Any operation that would
discard or overwrite local work should require your confirmation.

The following commands assume the upstream remote is named `upstream`. Add it once if it is not
configured yet:

```bash
git remote add upstream <upstream-repository-url>
```

Run these commands in the local branch you want to update:

```bash
git status --short
git fetch upstream
git merge upstream/main
```

Replace `main` if the upstream repository uses a different default branch. If a direct clone's
`origin` already points upstream, use `origin/main` instead; no additional remote is needed.

For automatic syncing, name both the desired frequency and the local branch to maintain, then ask
Coworker to set a repeating reminder. This prevents the task from using whichever branch happens to
be checked out when it runs. For example:

> Every week, inspect the current repository and safely merge `upstream/main` into `<local-branch>`. Preserve local commits, resolve only straightforward conflicts, run the relevant checks, and report the result. Ask me before discarding or overwriting any local work.

This workflow updates only the local branch. To update your own remote repository too, ask Coworker
to confirm the target remote and branch before running `git push`.

## Data and trust boundaries

Runtime data, memory, logs, and secrets stay on the local machine by default; Coworker does not
encrypt secrets stored in its configuration files. During a task, relevant prompts, context, tool
results, or attachments may be sent to the model provider you configured. Search, browser, and
communication tools also contact their corresponding third-party services. Command and file tools
run with the permissions of the operating-system user running Coworker; this is not a sandbox.

See [Data and trust boundaries](docs/architecture/data-boundaries.en.md) for storage locations, outbound
data, cleanup scope, and deployment boundaries.

## Explore further

| Document | Contents |
|---|---|
| [Documentation index](docs/README.en.md) | All usage, design, and collaboration documentation |
| [First Run](docs/getting-started/README.en.md) | Install the runtime, initialize a model, verify the instance, and select a client |
| [Web Management Console](docs/guides/README.en.md) | Status, memory, tasks, models, identity, extensions, and diagnostics |
| [Virtual-Life Philosophy and Life Architecture](docs/architecture/lifeform-philosophy.en.md) | Philosophy, life mechanisms, experimental facilities, and architecture criteria |
| [Configuration and models](docs/operations/configuration.en.md) | Environment variables, providers, models, and multi-instance configuration |
| [Data and trust boundaries](docs/architecture/data-boundaries.en.md) | Local storage, external services, permissions, and cleanup |
| [API and communication channels](docs/channels/api-and-channels.en.md) | REST, SSE, WebSocket, and file messages |
| [Coworker Desktop](docs/channels/desktop.en.md) | Installation, first connection, conversations, permissions, tray behavior, and updates |
| [Troubleshooting](docs/operations/troubleshooting.en.md) | Diagnostic order for the service, models, memory, Desktop, Relay, and containers |
| [Self-hosted Relay](docs/operations/relay.en.md) | End-to-end encrypted Desktop access from a private network; deployment, pairing, backup, and operations |
| [Core concepts and capabilities](docs/architecture/concepts.en.md) | Tools, directories, memory tree, restart recovery, and memory palaces |
| [Development guide](docs/development/development.en.md) | Local checks and Explore Lab |

## Development and contributing

See [CONTRIBUTING.en.md](CONTRIBUTING.en.md) for the contribution workflow, environment setup, and pre-PR checks.
Report security issues privately according to [SECURITY.en.md](SECURITY.en.md).

```bash
uv sync --dev
uv run pytest
```

## License

<p align="center">
  Coworker is available under the <a href="LICENSE">MIT License</a>.
  <br><br>
  <a href="#readme-top"><strong>Back to top ↑</strong></a>
</p>
