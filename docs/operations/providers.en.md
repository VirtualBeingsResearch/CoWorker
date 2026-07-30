# Provider Configuration Guide

[中文](providers.md) · English

[← Back to Configuration and Operations](README.en.md)

Coworker includes `anthropic`, `openai`, `deepseek`, `qwen`, `zhipu`, and `minimax` Providers.
The first call may incur cost; the setup wizard does not run an online capability probe.

## Choose a model

For a main model, confirm at least:

- tool/function calling works reliably in the selected Provider dialect;
- context covers expected short-term memory and tool results;
- output limit, thinking mode, latency, and price are acceptable;
- service terms allow the conversation, memory, and attachment data required by tasks.

The recommended catalog lists models statically declared to support tools. Administrators are
responsible for models entered outside the catalog.

## Configure

Prefer first-time setup or the management console. Use `.env` for unattended deployment:

```env
LLM__DEFAULT_PROVIDER=deepseek
LLM__DEFAULT_MODEL=deepseek-chat
LLM__DEEPSEEK_API_KEY=...
LLM__DEEPSEEK_BASE_URL=
```

An empty Base URL uses the Provider default. For an OpenAI-compatible gateway, still choose the
Provider type matching its actual request and response dialect. “Compatible” does not guarantee
identical tools, thinking, video, or error behavior.

## Multiple instances of one type

Copy `providers.json.example` to an untracked `providers.json`:

```json
[
  {
    "name": "zhipu-team-a",
    "type": "zhipu",
    "api_key": "...",
    "base_url": "",
    "default_model": "glm-5.1"
  }
]
```

`name` is the unique registry name used by `switch_model` and fallback. `type` selects the API
dialect. A file entry overrides a flat environment Provider with the same name.

## Model roles

- main: conversation, tool planning, and persistent tasks;
- summary: compression and summarization, usually without thinking to reduce cost;
- vision: image/video analysis, requiring declared Provider capability;
- mem0: long-term-memory extraction;
- fallback: ordered takeover after main-model failure.

Validate each specialist before adding fallback. Do not leave a dead Provider first in the chain.

## Common failures

- **Provider not registered**: check that an API key exists and `DEFAULT_PROVIDER` is a registry name.
- **401/403**: check key, Base URL, account scope, and proxies that alter headers.
- **404/model missing**: model IDs pass through unchanged; use the service's actual ID.
- **Tool call fails**: both model and gateway must support tool/function calling.
- **Thinking parameter fails**: disable thinking for that role or select a known supporting model.
- **High latency/cost**: split `/status.usage_stats` by main, summary, vision, bubble,
  subconscious, and mem0 before changing role assignments.

See [Configuration and Models](configuration.en.md) for every variable and
[Data and Trust Boundaries](../architecture/data-boundaries.en.md) for outbound data.

[← Back to project home](../../README.en.md)
