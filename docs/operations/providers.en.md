# Provider Configuration Guide

[中文](providers.md) · English

[← Back to Configuration and Operations](README.en.md)

Coworker dynamically reads completion-capable Providers from the installed Any-LLM runtime.
`anthropic`, `openai`, `deepseek`, `qwen` (Any-LLM's `dashscope`), `zhipu` (`zai`), `minimax`,
and `opencode-go` retain Coworker-specific adapters; other available Providers use a conservative
generic adapter.
Adding an Any-LLM Provider therefore does not require another Coworker frontend and backend enum.

To keep the default installation lightweight, Coworker pins only Any-LLM's `anthropic` and
`openai` extras instead of the large `all` extra. OpenAI-compatible Providers are generally
available immediately. A Provider that needs its own SDK appears only when that dependency can be
imported. Install a targeted extra from the Any-LLM documentation, such as
`any-llm-sdk[ollama]`, and restart to refresh the catalog. The first call may incur cost; setup does
not run an online capability probe. Coworker still preserves the necessary message, attachment,
tool, thinking, and token-usage differences for its specialized adapters instead of treating
“OpenAI-compatible” as behaviorally identical.

## Choose a model

For a main model, confirm at least:

- tool/function calling works reliably in the selected Provider dialect;
- context covers expected short-term memory and tool results;
- output limit, thinking mode, latency, and price are acceptable;
- service terms allow the conversation, memory, and attachment data required by tasks.

The recommended catalog lists models statically declared to support tools. For a model outside the
catalog, declare whether it supports tools, images, and video during first-time setup or on its
Provider connection. Coworker does not run an online capability probe.

Model orchestration can hot-adjust the primary reasoning effort. Providers with native effort
controls normalize the selection to levels supported by the active model (for example, GPT-5.6
maps unsupported `minimal` to `low`), while toggle-only thinking APIs safely collapse enabled
levels to “on”.

## Configure

Prefer first-time setup or the management console. Use `.env` for unattended deployment:

```env
LLM__DEFAULT_PROVIDER=deepseek
LLM__DEFAULT_MODEL=deepseek-chat
LLM__DEEPSEEK_API_KEY=...
LLM__DEEPSEEK_BASE_URL=
```

An empty Base URL uses the Provider default. Providers using Ollama, llama.cpp, LM Studio, or a
cloud environment credential chain may leave the form API key empty when their authentication
rules permit it. For an OpenAI-compatible gateway, prefer its specific catalog Provider type. Use
`openai` with a Base URL when no specific type exists. “Compatible” does not guarantee identical
tools, thinking, video, or error behavior.

“Sync model list” in first-time setup and Provider connections requests only the connection's model
list metadata; it does not start a chat, completion, or other model inference. Returned IDs are
merged into the selector, but an administrator must still declare `tools`, `vision`, and `video`
capabilities for models outside the catalog. A model list is not treated as capability evidence.
Some compatible gateways do not implement model listing, so a model ID can still be entered
manually when discovery fails.

The generic adapter does not infer a specific model's tool, image, or video capabilities from
Provider-level metadata. Administrators must declare capabilities for newly entered models.
Reasoning effort is passed only when Any-LLM marks the Provider as supporting reasoning. Project,
region, or environment credentials required by a specialized SDK still follow that SDK and
Any-LLM's environment-variable conventions.

### OpenCode Go

For `opencode-go`, an empty Base URL resolves to `https://opencode.ai/zen/go/v1`. The management
console syncs models visible to the subscription through `/models`; this is a metadata-only
request. Configure an API model ID such as `kimi-k3`, without the `opencode-go/` namespace prefix
used in OpenCode configuration files. The adapter also strips that prefix when one is entered.

OpenCode Go shares one Base URL across its catalog, but its current official models are served
through OpenAI-compatible `/chat/completions`, OpenAI `/responses`, or Anthropic `/messages`.
The specialized adapter routes known models according to the official catalog and retains dynamic
thinking effort. A future model that Coworker does not recognize falls back conservatively to Chat
Completions. Confirm that model's documented endpoint and update the adapter when necessary rather
than probing capabilities with a potentially billable inference call.

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
dialect. A file entry overrides a flat environment Provider with the same name.

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
- **Thinking parameter fails**: disable thinking for that role or select a known supporting model.
- **High latency/cost**: in Runtime analytics, split main, summary, vision, bubble, subconscious,
  and mem0, then consider pricing coverage and the Provider bill before changing role assignments.

See [Configuration and Models](configuration.en.md) for every variable and
[Data and Trust Boundaries](../architecture/data-boundaries.en.md) for outbound data.

[← Back to project home](../../README.en.md)
