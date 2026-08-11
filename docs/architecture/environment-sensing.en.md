# Environment Sensing

[中文](environment-sensing.md) · English

[← Back to architecture index](README.en.md)

## Overview

Environment sensing gives the agent **perception organs** — passively receiving
change signals from the external world, actively querying its own runtime
environment, and defining new perception sources through scripts.

Environment sources are **receive-only Channels**: they push external signals
into the agent inbox, but the agent cannot "reply" to them (`send()` returns a
not-supported error). This reuses the mature Channel infrastructure (access
control, traffic recording, routing, system prompt injection, lifecycle).

## Three information flows

### 1. Passive push (source polling)

Sources execute `poll()` on their declared trigger schedule and push signals
through the standard `publish_inbound()` path:

```
.coworker/environment/<name>/source.py  poll(ctx) → ctx.emit_signal(...)
    ↓ EnvironmentRuntime scheduling
EnvironmentChannel.publish_inbound(IncomingEvent)
    ↓ access control + traffic recording
InboxWatcher.push(wake=True)
    ↓ Agent wakes up, sees [ENVIRONMENT SIGNAL · <source>] message
```

### 2. Active query (tools)

The agent can call at any time:
- `get_system_status` — real-time CPU/memory/disk/process snapshot
- `get_runtime_context` — container detection/cloud/host info
- `manage_environment` — list/enable/disable/reload/trigger sources

### 3. System prompt injection

Active source list is injected into the `[CHANNELS]` prompt section, so the
agent always knows what it can perceive.

## Creating a source

Create `SOURCE.md` + `source.py` under `.coworker/environment/<name>/`:

```
.coworker/environment/my-source/
├── SOURCE.md     # frontmatter metadata
└── source.py     # async def poll(ctx): ...
```

### SOURCE.md frontmatter

```yaml
---
name: my-source           # source name (also participant_id suffix)
description: My custom source
mode: inline              # inline (in-process) or subprocess (isolated)
language: python
script: source.py
enabled: true
protected: false          # protected sources cannot be deleted (only disabled)

# Scheduling triggers (combinable — any match triggers a poll)
schedule_trigger: periodic  # periodic | cold_floor | manual
every_seconds: 300          # every 300 seconds
# every_n_cycles: 10        # every 10 agent cycles
# every_n_tool_calls: 50    # every 50 tool calls
# cold_floor_seconds: 60    # once, 60s after startup
# cron: "0 * * * *"          # cron expression
# min_interval_seconds: 60   # minimum interval guard

timeout_seconds: 60
params:
  url: https://example.com/api
---
```

### source.py (inline mode)

```python
async def poll(ctx):
    """Called by the framework on each trigger."""
    # ctx.config — params dict from SOURCE.md
    # ctx.http — shared httpx.AsyncClient
    # ctx.logger — loguru logger bound to this source

    resp = await ctx.http.get(ctx.config["url"])
    for item in resp.json()["items"]:
        # emit_signal auto-deduplicates (same fingerprint → skipped)
        ctx.emit_signal(
            title=item["title"],
            content=item["body"],
            fingerprint=f"item:{item['id']}",  # stable dedup key
            url=item.get("url"),
        )

    # Save cursor for next poll
    ctx.set_cursor(resp.headers.get("etag"))
```

### ctx API

| Method | Description |
|---|---|
| `ctx.config` | params dict (read-only) |
| `ctx.http` | shared httpx.AsyncClient |
| `ctx.logger` | loguru logger (source-tagged) |
| `ctx.emit_signal(title, content, fingerprint, url?, severity?)` | emit a signal; returns whether accepted (after dedup) |
| `ctx.get_cursor()` | retrieve the last saved cursor |
| `ctx.set_cursor(cursor)` | save a cursor |
| `ctx.is_known(fingerprint)` | check if a fingerprint was already emitted |

## Execution modes

### inline (recommended, default)

Python scripts run in-process; `ctx` is injected directly into the namespace.
Simplest and most efficient — `async def poll(ctx): ctx.emit_signal(...)` works.

### subprocess

Scripts run in a child process, communicating via stdin/stdout JSON-RPC. Any
language that can read/write stdin/stdout works. Use for isolation or non-Python.

## Scheduling model

Each source declares its own triggers (modeled on subconscious modes):

| Field | Semantics |
|---|---|
| `every_seconds` | every N seconds |
| `interval_seconds` | alias for every_seconds |
| `every_n_cycles` | every N agent cycles |
| `every_n_tool_calls` | every N tool calls |
| `cold_floor_seconds` | once, N seconds after startup |
| `cron` | standard 5-field cron expression |
| `min_interval_seconds` | minimum interval guard |
| `schedule_trigger: manual` | never auto-triggered (only via manage_environment) |

Multiple triggers can be combined — **any match fires the poll**.

## Agent self-editing

The agent can:
- Create new `SOURCE.md` + `source.py` via `write_file`
- `manage_environment(action="reload")` to rescan and discover new sources
- `manage_environment(action="enable/disable")` to toggle sources
- `manage_environment(action="run_now")` to trigger an immediate poll
- `get_system_status` to query its own resource state

This formalizes the agent's ability to extend its own perception.

## Configuration

```bash
ENVIRONMENT__ENABLED=true
ENVIRONMENT__SOURCES_DIR=.coworker/environment
ENVIRONMENT__STATE_PATH=data/environment/state.json
ENVIRONMENT__DEFAULT_TIMEOUT_SECONDS=60
ENVIRONMENT__MAX_CONCURRENT_POLLS=5
```

## Built-in sources

- **github-issues** — track a GitHub repo's issues and comments
- **tech-rss** — subscribe to an RSS/Atom feed

These ship with the project under `.coworker/environment/`. Edit their `params`
to customize.
