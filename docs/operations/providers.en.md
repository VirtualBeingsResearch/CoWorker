# Provider Configuration Guide

[中文](providers.md) · English

[← Back to Configuration and Operations](README.en.md)

Coworker includes `anthropic`, `openai`, `deepseek`, `qwen`, `zhipu`, `minimax`, and
`opencode-go` Providers, plus a generic `openai_compatible` Provider whose capabilities you
declare yourself. The first call may incur cost; the setup wizard does not run an online
capability probe.

## Choose a model

For a main model, confirm at least:

- tool/function calling works reliably in the selected Provider dialect;
- context covers expected short-term memory and tool results;
- output limit, thinking mode, latency, and price are acceptable;
- service terms allow the conversation, memory, and attachment data required by tasks.

The recommended catalog lists models statically declared to support tools. For a model outside the
catalog, declare whether it supports tools, images, and video during first-time setup or on its
Provider connection. Coworker does not run an online capability probe.

## Fetch models dynamically

In the admin Model Orchestration page, **Refresh model catalog** pulls live model IDs from each
registered Provider's models endpoint. During first-time setup you can enter an API key and Base
URL and press **Fetch model catalog** to preview before saving. Fetched IDs are merged with the
built-in catalog:

- `openai`, `deepseek`, `qwen`, `zhipu`, `minimax`, `opencode-go`, and
  `openai_compatible` use the OpenAI-compatible `GET /models`;
- `anthropic` uses `GET /v1/models`.

Remote lists return model IDs only, not tool/vision capability metadata. A dynamically discovered
model that is not in the built-in catalog still needs declared `tools`/`vision`/`video`
capabilities on its Provider connection. A failed fetch keeps the built-in catalog and shows the
error in the page; existing connections are unaffected.

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
identical tools, thinking, video, or error behavior. Use `openai_compatible` for a completely
unknown OpenAI-compatible gateway: it has no built-in catalog, so declare `default_model` and
`model_capabilities` in `providers.json`.

## Thinking effort

Main, summary, and vision each have an independent thinking switch and effort:

```env
LLM__THINKING_EFFORT=high
LLM__SUMMARY_THINKING=false
LLM__SUMMARY_THINKING_EFFORT=low
LLM__VISION_THINKING=true
LLM__VISION_THINKING_EFFORT=medium
```

Canonical levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`; an empty
value keeps the Provider's historical default request shape. `thinking=false` is equivalent to
`none` (models that always think ignore the disable request). Each Provider maps the canonical
levels to its native scale:

- `openai`: passed through to the Responses API `reasoning.effort`; unset keeps the historical
  `high` default;
- `anthropic`: `output_config.effort` on newer models, adaptive/disabled only on older ones;
- `deepseek`: `low`/`high`/`max`, with `medium`/`xhigh` mapped to `high` per official docs;
- `qwen`: `enable_thinking` plus `reasoning_effort`, with `high`/`max` mapped to `xhigh`;
- `zhipu`: `reasoning_effort` on GLM-5.2+, on/off only for other models;
- `minimax`: adaptive/disabled only;
- `opencode-go`: `reasoning_effort` passthrough for the supported DeepSeek/Kimi models;
- `openai_compatible`: sends standard `reasoning_effort` only when an effort is configured.

## OpenCode Go

`opencode-go` targets the OpenAI-compatible endpoint of the OpenCode Go subscription
(`https://opencode.ai/zen/go/v1`) with `LLM__OPENCODE_GO_API_KEY` (falling back to the
official `OPENCODE_API_KEY` environment variable) or a `providers.json` key.
The built-in catalog covers the OpenAI-compatible DeepSeek V4, Kimi K2.5+, GLM-5, MiMo, and HY
models. OpenCode Go serves MiniMax/Qwen through its Anthropic-compatible endpoint; configure
those with `type: anthropic` and `base_url: https://opencode.ai/zen/go`.

## Multiple instances of one type

Copy `providers.json.example` to an untracked `providers.json`:

```json
[
  {
    "name": "zhipu-team-a",
    "type": "zhipu",
    "api_key": "...",
    "base_url": "",
    "default_model": "glm-5.1",
    "model_capabilities": [
      { "model": "custom-omni-model", "tools": true, "vision": true, "video": false }
    ]
  }
]
```

`name` is the unique registry name used by `switch_model` and fallback. `type` selects the API
dialect. A file entry overrides a flat environment Provider with the same name. A generic
OpenAI-compatible gateway looks like:

```json
{
  "name": "self-hosted-vllm",
  "type": "openai_compatible",
  "api_key": "EMPTY",
  "base_url": "http://127.0.0.1:8000/v1",
  "default_model": "Qwen3-32B",
  "model_capabilities": [
    { "model": "Qwen3-32B", "tools": true, "vision": false, "video": false }
  ]
}
```

`model_capabilities` declares `tools`, `vision`, and `video` for an exact model ID. A declaration
overrides the protocol's built-in model detection; unlisted models continue to use the built-in
catalog. A model with `video: true` must also set `vision: true`. Primary and fallback models require
`tools`, while a vision specialist requires `vision`.

## Define model prices

Runtime Settings → Models & Providers contains an independent pricing table. The same data can be
provided as JSON through `LLM__MODEL_PRICES`. Prices exact-match the Provider registry name and
model ID. The connection does not need to be managed in the console, so pricing can cover `.env`
or `providers.json` connections and retained historical Provider/model pairs.

Enter input, output, and optional cached-input prices per million tokens. A blank cached-input
price uses the regular input price. Currencies accumulate separately without exchange-rate
conversion. Editing a price immediately recalculates management estimates from existing token
usage; it neither changes `usage_stats.json` nor preserves the price that applied when a call ran.

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
- **Thinking parameter fails**: disable thinking for that role, or set a `thinking_effort` the
  model supports; unsupported levels are rejected with a 400 by the Provider.
- **High latency/cost**: in Runtime analytics, split main, summary, vision, bubble, subconscious,
  and mem0, then consider pricing coverage and the Provider bill before changing role assignments.

See [Configuration and Models](configuration.en.md) for every variable and
[Data and Trust Boundaries](../architecture/data-boundaries.en.md) for outbound data.

[← Back to project home](../../README.en.md)
