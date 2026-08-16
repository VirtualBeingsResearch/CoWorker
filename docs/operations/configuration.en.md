# Configuration and Models

[中文](configuration.md) · English

[← Back to Configuration and Operations](README.en.md)

## Basic configuration

Coworker currently supports running from a source checkout only. Common settings can be
supplied through the first-run setup wizard, `.env`, or environment variables. Variable names use
double underscores to separate groups; start with the repository's `.env.example` for unattended
configuration.

Configuration precedence is `data/admin_config.json`, then `.env`, then operating-system
environment variables. `data/model_runtime_config.json` overrides only the summary, fallbacks, and
vision settings changed at runtime. When a container or service manager injects environment
variables, make sure the working directory does not contain conflicting `.env` values. The
administration page writes only fields that differ from inherited configuration to
`admin_config.json`; saving removes overrides restored to their `.env` or product-default value.
An explicit empty list remains an override when it differs from the inherited value. Unchanged
defaults therefore evolve with the product instead of being frozen merely by opening or saving a
whole settings group. Startup also normalizes an existing override file with an atomic write:
inherited values from old snapshots are removed while custom values, secrets, and explicit
overrides remain intact. Runtime Settings lists the admin overrides in the current section and
allows individual fields to be restored to inherited configuration. Unsaved drafts and secret
inputs remain isolated by section, and saving submits only the current section.

Until first-run setup is complete, Coworker starts only the management HTTP service. It does not start the Agent loop, inbound message polling, or external channels such as WeCom. Every command-line start prints the currently effective administrator token, and browser requests outside `/admin` or ordinary APIs are redirected to `/admin`; the management assets, login verification, and bootstrap endpoints remain available. The wizard displays the server's current timezone read-only, detects the browser timezone, and recommends the corresponding `TZ` environment variable, but quick setup does not automatically change the server or container timezone. It can also set the runtime language and maximum output tokens, and accepts either a recommended model or a manually entered model ID. Saving performs a clean restart into normal operation without restoring setup-time short-term state or emitting a normal restart notice.

### Runtime language and system timezone

| Variable | Default | Description |
|---|---|---|
| `I18N__LOCALE` | `zh-CN` | Instance-wide model/runtime language; accepts `zh-CN`, `en`, and common aliases such as `zh`, `zh_CN`, and `en-US`, then takes effect after restart |
| `TZ` | Follows the operating system; Docker Compose defaults to `Asia/Shanghai` | Process or container IANA timezone such as `Asia/Shanghai`; Coworker reads this system environment and never changes it at runtime |

The runtime locale is independent of the Web/Desktop interface language and can also be changed
under Runtime language in the administration page. It controls Coworker-owned system prompts,
tool schemas and result wrappers, summarization and memory framework text, Bubbles, subconscious
modes, vision requests, API errors and response notes, cataloged operational warnings and notices,
and participant-facing
system notices. Replies to a participant default to
the language of that participant's current message; an explicit language request wins; autonomous
output without a current user message falls back to the runtime locale. Switching locale does not
translate user content, historical data, third-party text, or existing Identity, Skill, Palace,
task, or memory data, so mixed-language history is expected for compatibility. Restart injects a
language-transition system notice when it detects a locale change.

The system timezone controls current time in the system prompt and `get_context`, message-time
prefixes, how alarms interpret timestamps without an explicit offset, and date boundaries in task
views. Coworker has no separate timezone override and the administration page never changes it; set
`TZ` through the operating system, container, or service startup environment, then restart the process.
First-run setup displays the process's current timezone read-only as a reference and detects the
browser's IANA timezone through `Intl.DateTimeFormat` only to display a corresponding `TZ`
recommendation. It never writes either value into configuration. A reverse proxy does not affect
detection because it runs in the administrator's browser, not on the proxy or server.

### LLM

| Variable | Default | Description |
|---|---|---|
| `LLM__DEFAULT_PROVIDER` | `deepseek` | Default LLM provider |
| `LLM__DEFAULT_MODEL` | `deepseek-v4-pro` | Default model |
| `LLM__MAX_TOKENS` | `8192` | Maximum output tokens for one LLM response |
| `LLM__THINKING_EFFORT` | Empty (provider default) | Main thinking effort: `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`, mapped by each provider to its native levels |
| `LLM__SUMMARY_PROVIDER` | Empty | Provider dedicated to summarization/compression; when empty, use the current main provider |
| `LLM__SUMMARY_MODEL` | Empty | Model dedicated to summarization/compression; setting only this field reuses the current provider, while leaving it empty with `SUMMARY_PROVIDER` configured uses that provider's `default_model` |
| `LLM__SUMMARY_THINKING` | `false` | Whether summarization/compression calls enable thinking; disabled by default to reduce latency and cost |
| `LLM__SUMMARY_THINKING_EFFORT` | Empty (provider default) | Summary thinking effort, same levels as `LLM__THINKING_EFFORT` |
| `LLM__FALLBACKS` | `[]` | Ordered fallback chain used after a main-model failure; a JSON array of `providerName` or `providerName/modelId` entries |
| `LLM__MODEL_PRICES` | `[]` | Model-price JSON array, exact-matched by Provider registry name and model ID; hot-applied and used to reprice historical estimates at current prices |
| `LLM__ANTHROPIC_API_KEY` | Empty | Anthropic API key |
| `LLM__ANTHROPIC_BASE_URL` | Empty | Custom Anthropic base URL |
| `LLM__OPENAI_API_KEY` | Empty | OpenAI API key |
| `LLM__OPENAI_BASE_URL` | Empty | Custom OpenAI base URL |
| `LLM__DEEPSEEK_API_KEY` | Empty | DeepSeek API key |
| `LLM__DEEPSEEK_BASE_URL` | Empty (uses `https://api.deepseek.com` when unset) | Custom DeepSeek base URL |
| `LLM__QWEN_API_KEY` | Empty | Qwen / DashScope API key |
| `LLM__QWEN_BASE_URL` | Empty (uses the DashScope-compatible endpoint when unset) | Custom Qwen base URL |
| `LLM__ZHIPU_API_KEY` | Empty | Zhipu API key |
| `LLM__ZHIPU_BASE_URL` | Empty (uses the Zhipu OpenAI-compatible endpoint when unset) | Custom Zhipu base URL |
| `LLM__MINIMAX_API_KEY` | Empty | MiniMax API key |
| `LLM__MINIMAX_BASE_URL` | Empty (uses the MiniMax OpenAI-compatible endpoint when unset) | Custom MiniMax base URL |
| `LLM__OPENCODE_GO_API_KEY` | Empty (falls back to official `OPENCODE_API_KEY`) | OpenCode Go subscription API key |
| `LLM__OPENCODE_GO_BASE_URL` | Empty (uses `https://opencode.ai/zen/go/v1` when unset) | Custom OpenCode Go base URL |
| `LLM__PROVIDERS_FILE` | `providers.json` | Named provider list file (see “Multiple provider instances” below); ignored if the file does not exist |
| `LLM__RUNTIME_CONFIG_FILE` | `data/model_runtime_config.json` | Runtime overrides written after online changes to thinking / summary / fallbacks / vision; these override matching model settings from `.env` at startup |
| `LLM__VISION_PROVIDER` | Empty | Provider used by the visual analysis tool; when empty, `visual_analyze` asks you to configure one first |
| `LLM__VISION_MODEL` | Empty | Model used by the visual analysis tool; video analysis also requires the provider to declare native video support |
| `LLM__VISION_THINKING` | `true` | Whether visual analysis calls enable thinking; set it to `false` to use a supported provider's non-thinking mode and reduce latency and cost |
| `LLM__VISION_THINKING_EFFORT` | Empty (provider default) | Vision analysis thinking effort, same levels as `LLM__THINKING_EFFORT` |

Each `LLM__MODEL_PRICES` item contains `provider`, `model`, a three-letter uppercase `currency`,
`input_per_million`, `output_per_million`, and optional `cached_input_per_million`. Prices must be
finite and non-negative, and each Provider/model pair may appear only once. A missing cached-input
price falls back to the regular input price. For example:

```json
[
  {
    "provider": "openai",
    "model": "gpt-5.2",
    "currency": "USD",
    "input_per_million": 1.75,
    "output_per_million": 14,
    "cached_input_per_million": 0.175
  }
]
```

Pricing is independent of connection sources, so it can supplement read-only connections from
`.env` or `providers.json`. Saving prices in the management console neither rebuilds Providers nor
requires a restart; historical tokens are recalculated against current prices. Currencies remain
separate and are never converted. The management console suggests common three-letter ISO 4217
currency codes while still allowing other three-letter codes to be entered manually. Currency symbols
come from the browser using the code and interface locale; codes without a dedicated symbol are shown
as codes.

### Memory

| Variable | Default | Description |
|---|---|---|
| `MEMORY__DB_PATH` | `data/memory` | Long-term memory database directory |
| `MEMORY__SHORT_TERM_MAX_TOKENS` | `120000` | Triggers one short-term-memory compression pass after the latest complete model input reaches this budget; temporary overshoot is allowed |
| `MEMORY__COMPRESS_RATIO` | `0.30` | Fraction of the oldest primary-message tokens processed by each compression pass; shared by tree and legacy modes |
| `MEMORY__TREE_ENABLED` | `true` | Enable the multiresolution memory tree; disabling it restores the legacy single-anchor compression behavior |
| `MEMORY__TREE_SPINE_CAP_FRACTION` | `0.30` | Token cap for the memory-tree spine as a fraction of the total |
| `MEMORY__TREE_BACKFILL_MAX_LEAVES` | `64` | Maximum number of leaves generated by one `--backfill-tree` history backfill |
| `MEMORY__TREE_BACKFILL_CONCURRENCY` | `5` | Maximum concurrency for leaf summarization and reduction during backfill |
| `MEMORY__TREE_MERGE_REACH_DEPTH` | `2` | Number of lower detail levels read during a high-level merge; `2` means the lowest two levels |
| `MEMORY__AUTO_RECALL_ENABLED` | `true` | Whether to search long-term memory automatically when a message arrives |
| `MEMORY__AUTO_RECALL_RELEVANCE_THRESHOLD` | `0.5` | Relevance threshold for automatic recall (0–1) |
| `MEMORY__AUTO_RECALL_LIMIT` | `5` | Maximum number of memories injected by each automatic recall |
| `MEMORY__MEM0_LLM_PROVIDER` | `""` (follows main line) | Independent provider for mem0 extraction; leave empty to follow the runtime active provider, including manual switches and failure fallbacks, or set a Brain provider name or type to reuse its credentials and effective `base_url`. Hot-applied, no restart needed |
| `MEMORY__MEM0_LLM_MODEL` | `""` (follows main line) | Independent model ID for mem0 extraction. When the provider is also empty, it follows the runtime active model. With an explicit provider and an empty model, it uses that provider's `default_model` (or `LLM__DEFAULT_MODEL`). The model ID is passed through to the API dialect; changes are hot-applied with no restart needed |
| `MEMORY__MEM0_LLM_THINKING` | `false` | Thinking toggle for the mem0 extraction LLM; injects the matching parameter for known thinking models (extraction is a structured JSON task, so thinking is off by default to avoid burning tokens). Hot-applied, no restart needed |
| `MEMORY__MEM0_EMBEDDER_MODEL` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | Embedding model used by mem0; do not switch it directly when existing data is present |

### Agent

| Variable | Default | Description |
|---|---|---|
| `AGENT__INBOX_DIR` | `data/inbox` | Directory for incoming file messages |
| `AGENT__OUTBOX_DIR` | `data/outbox` | Directory for outgoing file messages |
| `AGENT__IDENTITY_DIR` | `data/identity` | Identity file directory |
| `AGENT__LOGS_DIR` | `data/logs` | Log directory |
| `AGENT__SYSTEM_PROMPT_TEMPLATE` | Empty | System prompt template. Empty or whitespace-only values use the product-standard template; maximum 100,000 characters. A saved administration override requires a safe restart |
| `AGENT__INTERACTION_LOG_ROTATION_BYTES` | `52428800` | Maximum bytes in one interaction-log shard. At the limit, the active `interactions.jsonl` is archived under an increasing number and a new file is used. Set to `0` to disable rotation. |
| `AGENT__IDLE_SLEEP_SECONDS` | `30` | Idle sleep interval in seconds |
| `AGENT__INBOX_POLL_INTERVAL` | `2.0` | Inbox polling interval |
| `AGENT__TICK` | `true` | Whether autonomous ticks run when no external message is present |
| `AGENT__PASSIVE_MODE` | `false` | Enable Passive mode. On first startup and restart, the main loop remains at rest and retains startup notices silently until the next real wakeup; idle timeouts do not wake it |
| `AGENT__CODE_HARD_TIMEOUT` | `300` | Hard timeout in seconds for the code execution tool |
| `AGENT__IMAGE_MAX_DIMENSION` | `960` | Maximum image dimension in pixels before sending it to a model; larger images are scaled proportionally |
| `AGENT__MESSAGE_TIME_PREFIX` | `true` | Whether to prefix user messages sent to the model with local time |
| `AGENT__BUBBLE_THINKING` | `true` | Whether to enable parallel Bubble thinking |
| `AGENT__BUBBLE_MAX_CONCURRENT` | `5` | Maximum number of concurrent Bubble branches |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES` | `["wecom:*", "weixin:*", "tg:*", "coworker-desktop:*:local:*"]` | JSON array of case-sensitive, full-ID participant globs; an entry without wildcards is an exact match. Matching recipients receive a Bubble-ID takeover or resume notice on the first real exchange, and direct replies carry provenance; completion is sent only for an announced handoff. The defaults match WeCom, Weixin Claw, Telegram, and the Desktop `local` actor; set `[]` to disable every default participant match. |
| `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS` | `["websocket", "sse"]` | JSON transport array accepting `websocket` and `sse`; both are enabled by default, so live generic streams use transparent handoff automatically. A Desktop actor that does not match a participant glob never falls through to this rule, so `claude` and `codex` remain excluded. Set `[]` to disable transport matching. |
| `AGENT__BUBBLE_TIMEOUT_RESUME_SECONDS` | `300` | Grace period in seconds for continuing a Bubble with `bubble_spawn(bubble_id=...)` after it reaches its cycle limit; set to `0` to disable. |
| `AGENT__SUBCONSCIOUS_THINKING` | `true` | Whether to enable background subconscious thinking |
| `AGENT__SUBCONSCIOUS_SUMMARIZE_BEFORE_COMPRESS` | `true` | Whether to trigger subconscious summarization before compression |
| `AGENT__SUBCONSCIOUS_MAX_CYCLES` | `5` | Maximum cycles for one subconscious task |

`AGENT__SYSTEM_PROMPT_TEMPLATE` may reference `{{IDENTITY}}`, `{{ENVIRONMENT}}`,
`{{INSTINCTS}}`, `{{GUIDELINES}}`, `{{LANGUAGE_POLICY}}`, `{{THINKING}}`,
`{{CHANNELS}}`, `{{SKILLS}}`, and `{{PALACES}}`. Each variable contains its section
heading and rendered body. Matching `_CONTENT` variables such as `{{IDENTITY_CONTENT}}`
and `{{ENVIRONMENT_CONTENT}}` contain only the body, so headings such as `[IDENTITY]`
may be omitted or supplied by the template. A variable must occupy its own line and may
appear at most once; the full and content-only forms of one section cannot be used together.
Unknown, duplicate, or conflicting variables fail configuration validation. Use `\{{NAME}}` for a literal
placeholder. Variables may be reordered or omitted; a template with no variables fully
replaces the built-in prompt. Bracketed headings, including `[CUSTOM]`, are ordinary text
and may be renamed, split, or removed. Tool schemas are not part of the template and remain
supplied by the model-call layer. For multiline templates, use **Relationships → Identity
Profile** in the administration page; save, then perform a safe restart to apply the change.
The editor has synchronized line numbers and a blank-line count. Each variable card also
previews the full section and body rendered by the currently running instance.

### API, administration, and communication

| Variable | Default | Description |
|---|---|---|
| `API__HOST` | `127.0.0.1` | API listen address; expose it only through an explicitly configured reverse proxy/TLS layer |
| `API__PORT` | `8000` | API listen port |
| `API__PUBLIC_URL` | Empty | Browser-facing public HTTP(S) root URL behind a reverse proxy; it may contain only a scheme, host, and optional port, and first-run reconnect prefers it over the internal bind address |
| `API__CORS_ORIGINS` | `["http://localhost:8000", "http://127.0.0.1:8000"]` | JSON list of browser origins allowed to access the API; an empty list disables cross-origin requests |
| `API__COMMUNICATION_TOKEN` | Empty (administrator-token fallback) | Bearer token for production communication; when explicitly set it protects Desktop traffic, ordinary REST messages, the runtime log stream, and the full `/status` snapshot. Saving through the management console applies immediately; `.env` changes require a restart. Configure it separately to isolate permissions |
| `CHANNEL_ACCESS` | `{}` | JSON object of per-channel inbound/outbound participant access lists; each entry may contain `inbound_allow`, `inbound_deny`, `outbound_allow`, and `outbound_deny` |
| `ADMIN__TOKEN` | Generated on first startup | Bearer token for the `/admin` console and `/api/admin/*`; the generated value is saved in the administration configuration file |
| `ADMIN__CONFIG_FILE` | `data/admin_config.json` | Typed JSON override layer saved by the administration page, with higher priority than `.env`; non-hot-reload settings take effect after a restart |
| `DESKTOP_UPDATES__DIR` | `data/desktop_updates` | Storage directory for Desktop update releases and assets |
| `DESKTOP_UPDATES__ADMIN_TOKEN` | Empty | Bearer token for the Desktop update administration API |
| `DESKTOP_UPDATES__SYNC_SOURCES` | `[]` | JSON array of upstream source instances. Each item has a stable `id`, display `name`, and `type`; multiple GitHub repositories and multiple Coworker instances are supported |
| `DESKTOP_UPDATES__SYNC_ACTIVE_SOURCE` | Empty | UUID of the active source; empty disables synchronization. Only one active source runs at a time |
| `DESKTOP_UPDATES__FEED_TOKEN` | Empty | Independent token that lets other Coworker instances sync from this instance's published release feed; empty means the feed endpoint is closed, and it is not the administrator token |
| `DESKTOP_UPDATES__SYNC_INTERVAL_SECONDS` | `21600` | Upstream check interval, from 300 to 604800 seconds |
| `DESKTOP_UPDATES__SYNC_ON_START` | `true` | Run one upstream check when the service starts |
| `DESKTOP_UPDATES__SYNC_MAX_ASSET_BYTES` | `2147483648` | Maximum bytes allowed for one downloaded asset |
| `DESKTOP_UPDATES__SYNC_MAX_RUN_BYTES` | `4294967296` | Maximum total bytes allowed for one synchronization run |
| `WECOM__ENABLED` | `false` | Whether to enable the WeCom intelligent-bot WebSocket connection |
| `WECOM__BOT_ID` | Empty | WeCom bot ID |
| `WECOM__SECRET` | Empty | WeCom bot secret |
| `WECOM__WS_URL` | Empty | Optional WeCom WebSocket URL; empty uses the SDK default |
| `TELEGRAM__BOTS` | `{}` | JSON object of multiple Telegram Bots keyed by stable `instance_id`; each item accepts `enabled`, `display_name`, `bot_token`, `api_base_url`, `local_mode`, and `poll_timeout_seconds` |
| `WEIXIN__ENABLED` | `true` | Enable the personal-Weixin ClawBot channel; no network polling occurs without a connection |

When a reverse proxy serves `/admin`, `/api/*`, and static assets together, set
`API__PUBLIC_URL` to the origin the browser actually opens, such as
`https://coworker.example.com`. It does not change the internal `API__HOST` or `API__PORT` bind;
it keeps first-run and post-restart administrator navigation on the stable public address. Do not
include `/admin`, another path, query parameters, or credentials. If the frontend and API use
different origins, add the exact frontend origin to `API__CORS_ORIGINS` as well. When changing the
internal port, update the reverse-proxy upstream before Coworker becomes ready on the new port.

`CHANNEL_ACCESS` keys are channel names, and all four rule lists contain case-sensitive, full-ID
participant globs. Deny takes precedence; a non-empty allow list admits only matches; an omitted
channel or four empty lists allows everything. The administration console hot-applies this setting.
See [Channel access lists](../channels/api-and-channels.en.md#channel-access-lists) for built-in keys,
enforcement timing, and examples.

When the main loop is resting, the Overview page in the administration console shows **Continue**.
The action itself sends only an internal wake signal and creates no new inbox message. The model
continues from its existing context, while any startup notice or other event already queued
silently is still processed in normal order. The corresponding administrator API is
`POST /api/admin/resume`; its `resumed` field reports whether the request actually woke a resting
main loop.

Saving WeCom settings in the admin console immediately enables, disables, or rebuilds the WebSocket connection without restarting Coworker. A reconnect clears reply frames that belong only to the old connection while preserving discovered contacts and recent activity. If WeCom reports that a newer connection has taken over, the runtime waits for the next configuration change instead of competing with that connection.

Telegram supports multiple Bots at once, and the administration console hot-adds, removes,
enables, disables, or rebuilds individual instances. Each instance defaults to
`https://api.telegram.org`, while `api_base_url` may select a proxy or self-hosted Bot API Server;
enable `local_mode` only when the self-hosted server runs with `--local` and shares file paths with
Coworker. Participant IDs use
`tg:<instance_id>:<chat_id>`, and tokens remain masked in the administration API. See
[Telegram](../channels/telegram.en.md) for complete configuration, Privacy Mode, attachment limits,
and troubleshooting.

The Weixin Claw module registers its transport, management interface, and hot-settings provider
together. A confirmed scan stores the connection in
`MEMORY__DB_PATH/weixin_connections.json` and immediately starts one
`weixin:<bot_instance_id>` participant; connections are not `admin_config.json` settings. One Bot
instance can bind only one Weixin account. Whoever views the QR code is not automatically bound to that connection, and the agent still organizes contact relationships. An unfinished pairing session is restored after leaving and returning to the administration page. See [Weixin Claw](../channels/weixin-claw.en.md).

### Container Git workspace

| Variable | Default | Description |
|---|---|---|
| `COWORKER_BUNDLE_REPOSITORY_URL` | Official Coworker repository | Compatible repository converted to a Git bundle while building the image |
| `COWORKER_BUNDLE_REPOSITORY_REF` | Repository `HEAD` | Branch, tag, or commit recorded as the bundled checkout |
| `COWORKER_WORKSPACE_PATH` | `/app` | In-container Git workspace shared by the running source and the Agent |
| `COWORKER_WORKSPACE_SOURCE` | `.` | Compose mount source for `/app`; defaults to the current checkout, or set it to `coworker-workspace` to reuse the image-managed named volume |
| `COWORKER_STATE_PATH` | `/var/lib/coworker` | Persistent runtime data; `/app/data` in the workspace points here |
| `COWORKER_REPOSITORY_URL` | Empty | Repository cloned on first startup by a non-strict-offline image |
| `COWORKER_REPOSITORY_REF` | Bundled commit or remote default branch | Branch, tag, or commit checked out during initialization |
| `COWORKER_REPOSITORY_BUNDLE` | Bundle embedded in the image | Path to an explicitly mounted custom bundle |

By default, Compose mounts the current local Git checkout directly at `/app`, repository
initialization never overwrites it, and the entrypoint links `/app/data` to the separate
`coworker-state` volume. If the checkout already has a non-empty `data/`, the entrypoint refuses to
overwrite it; follow [Upgrading and Migration](upgrading.en.md#migrate-an-existing-checkout-data-directory)
to import it into the state volume first. With `COWORKER_WORKSPACE_SOURCE=coworker-workspace`, Docker copies the
image's `/app` tree into the named volume when it is first created and the entrypoint attaches Git
metadata from the bundle.
After an image update, the entrypoint automatically fast-forwards only a clean, non-divergent
managed workspace that remains on the image's default branch; local changes, commits, other
branches, and divergent history remain untouched. Other repository settings apply only before the
workspace is initialized. The `offline` image prevents the startup initializer from accessing the
network through `COWORKER_REPOSITORY_URL`, but it is not a network sandbox and does not block
user-authorized Agent requests that use Git, search, a browser, or integrations. Generate
private-repository bundles in a controlled build environment; do not put credentials in URLs or
image build arguments.

## Supported models

The built-in provider types are `anthropic`, `openai`, `deepseek`, `qwen`, `zhipu`, `minimax`,
and `opencode-go`. `openai_compatible` is a generic OpenAI-compatible type with no built-in
catalog; declare its models through `model_capabilities`. The recommended catalog contains models
that the corresponding provider statically marks as tool-capable. The exact list changes with the
source; use the first-run wizard and the provider implementations under
[`src/coworker/brain/`](../../src/coworker/brain/) as the source of truth.
First-run setup can accept a model outside the catalog after the administrator declares whether it
supports tools, images, and video on this connection. No potentially billable online probe is
performed, and a primary model must be declared tool-capable. These declarations remain editable
under Runtime settings → Models & Providers.

Only providers with a corresponding API key are registered. `LLM__DEFAULT_PROVIDER` must refer to
a registered provider instance name.

### Multiple provider instances (`providers.json`)

The flat fields above, such as `LLM__ZHIPU_API_KEY`, allow only one instance of each provider type. To configure **multiple instances of the same type**—for example, separate Zhipu keys for different users—list them by `name` in the JSON file referenced by `LLM__PROVIDERS_FILE`. This separates the provider “type” (API dialect and model catalog) from its registration name (the registry key referenced by `default_provider` and `switch_model`):

```json
[
  { "name": "zhipu-userA", "type": "zhipu", "api_key": "...", "default_model": "glm-5.1", "model_capabilities": [{ "model": "custom-omni-model", "tools": true, "vision": true, "video": false }] },
  { "name": "zhipu-userB", "type": "zhipu", "api_key": "...", "base_url": "...", "default_model": "glm-4.7" }
]
```

Fields: `name` (required, unique registration name), `type` (required; a built-in provider type or
the generic `openai_compatible`), `api_key`, optional `base_url`, optional `default_model` (used when
`switch_model` selects the instance without specifying a model), and optional `model_capabilities`.
Each capability entry contains an exact `model` ID plus `tools`, `vision`, and `video` booleans.
Explicit declarations override the Provider type's built-in detection; unlisted models keep using
the built-in catalog. Video capability also requires vision capability. `openai_compatible` has no
built-in catalog, so configure `default_model` and declare at least its `tools` capability.

- Flat fields still work and are merged automatically as the default instance where `name == type`; an entry with the same `name` in the file overrides it.
- A missing file is ignored, so existing configurations continue to work unchanged.
- See `providers.json.example` in the repository root for a complete example.
