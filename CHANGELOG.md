# Changelog

## Unreleased

- fix(runtime): keep one lightweight Windows supervisor, hard-exit the fully torn-down worker on restart, and relaunch a single replacement worker so Python threads and application memory do not accumulate across restarts
- fix(memory): resolve mem0 through the same named Brain provider credentials and effective base URL, adapt all OpenAI-compatible providers by API dialect without a second model allowlist, and isolate mem0 from unrelated OpenRouter environment settings
- fix(memory): keep Mem0 configuration provider names within its validated API dialects while replacing their implementations with Coworker endpoint and TLS adapters
- fix(desktop): use complete GitHub release-asset digests to detect identical or conflicting imported releases before downloading binaries, while retaining download verification for legacy APIs without digests
- fix(channels): persist participant last-sent and last-received timestamps in a shared Channel activity store so connection context survives application restarts
- feat(wecom): keep the WeCom Channel runtime registered while disabled or incomplete, and hot-apply admin configuration by enabling, disabling, or reconnecting its WebSocket without restarting Coworker
- fix(admin): give all management form fields one top-aligned layout contract so labels, controls, hints, and switches stay aligned without Provider- or Desktop-specific sizing patches
- fix(admin): present Passive sleep as event-driven waiting in runtime status and the public runtime ledger, and explain that `sleep(0)` waits indefinitely instead of using the active self-wake interval
- feat(admin): add an authenticated, uncached, read-only view of the exact system prompt currently used by the agent
- fix(desktop): fall back to the administrator token for Desktop communication and expose a redacted dedicated-token setting
- refactor(identity): keep identity focused on name, current location, and personality; retire duplicated goal and life-story fields in favor of tasks, long-term memory, and `profile.md`
- refactor(channels): replace `ChannelHost` with `ChannelRegistry` and a single `ChannelSystem` composition root, make `BaseChannel` the only Channel extension abstraction with a `from_sender` shortcut and declarative outbound capabilities, preserve message delivery while explicitly reporting omitted unsupported fields to AI callers, support WeCom `extra.mentioned_list` with Channel-owned unsupported-field diagnostics, move Stream sessions, registrations, attachments, outbox delivery, and lifecycle into `StreamRuntime`, route Desktop as an internal `StreamProfile`, compact Desktop pinned context without changing snapshot transport, lazily announce transparent Bubble handoff on the first real exchange, pair completion notices only with announced sessions, and aggregate invalid bound Bubble communication arguments while echoing the fixed target, match WeCom replies to their exact inbound frame through `conversation_id`, aggregate all Channel, profile, Runtime startup, and tool registration diagnostics, make tool batches atomic, inject channels directly into API routes, and remove obsolete communication-tool proxies and legacy bridge compatibility paths without changing current participant IDs or wire contracts

## 0.3.2 - 2026-07-23

- feat(desktop-updates): synchronize partial GitHub Releases using asset digests, preserve domain-based requests, and render imported release notes safely
- fix(channels): show the latest send and receive times for every listed channel in localized `list_connections` output instead of transient active/offline labels
- refactor(channels): centralize normalized inbound event delivery through `ChannelHost` and remove WeCom Runner's direct `InboxWatcher` dependency
- refactor(channels): route raw HTTP/WebSocket envelopes into their owning channels, which now normalize payloads, persist attachments, record receive activity, and publish inbound events
- feat(first-run): add admin-only clean bootstrap setup with runtime language/token/passive-mode options, confirmed custom tool-capable models, setup redirects, and effective-token display while setup is incomplete
- refactor(channels): introduce a unified `Channel`/`ChannelHost` abstraction, promote the generic WS/SSE transport to `channels/stream/` (consolidating the dual connection registry), replace `CommunicateTool.register_sender` with channel-owned routing, split `WeComRunner` into runner/sender/contacts, and split `DesktopRegistry` (detail store extracted, dead `intercept` removed). `list_ws_connections` is renamed to `list_connections` and now aggregates connections across all channels (WS/SSE streams, WeCom groups/users, Desktop actors); Explore Lab also exposes its virtual participants through the same tool and names its editable control-API field `virtual_connections`. `IncomingEvent.source` is now a plain `str`. Production wire contracts (URLs, register/SSE/WS/message shapes, participant_id assignment) are preserved; the Explore Lab control API intentionally drops its former connection-field name without an alias.
- ci: add a reviewed version-preparation workflow, preserve generic Unreleased notes during version bumps, and include previously filtered internal commit subjects
- ci: add a one-step manual release entry that creates a canonical tag and starts desktop and container publishing
- fix(admin): show model-switch errors in the management console
- fix(first-run): avoid queuing profile generation before a model is configured, clarify the setup URL, and default Compose to the published offline image
- docs: reorganize documentation by functional domain
- feat(i18n): add instance-wide `zh-CN`/`en` runtime localization for prompts, complete tool schemas, memory, Bubbles, subconscious modes, vision, notifications, Coworker-owned API messages, cataloged operational notices, and localized user-asset companions; locale changes are announced after restart

## 0.3.1 - 2026-07-20

- Bump actions/setup-node from 6 to 7
- Bump actions/upload-artifact from 4 to 7
- fix: ci failure
- fix: ci broken
- fix(ci): bundle check skip dependabot pr
- build(deps): bump the vite group across 3 directories with 2 updates
- build(deps): bump actions/checkout from 6 to 7
- chore: update deps and resolve rust warn
- feat(container): add preloaded embedding images
- feat(agent): add passive rest mode
- feat(web): add localized chat dashboard
- feat: support non-thinking visual analysis
- feat: rotate interaction logs
- fix(test): default passive mode cause stuck
- feat(admin): add lifetime interaction log viewer
- fix(agent): back up and reset context after recovery errors
- feat(bubble): resume timed-out bubbles
- fix: make AgentConfig default visible to mypy
- feat: add transparent Bubble conversation handoff
- fix: preserve stream transport literal types
- fix: harden Bubble participant communication
- fix(admin): render structured user messages
- fix(wecom): deduplicate message prefixes
- refactor: shorten model-facing IDs
- docs: improve the bilingual project documentation and add product screenshots
- chore(desktop): move the updater public key to build-time configuration
- ci: add merge queue coverage, automatic web bundle updates, and mypy caching
- build(deps): refresh Python, Rust, web, desktop, and Explore Lab dependencies
- fix(container): restore Python 3.13 compatibility and make Playwright provisioning independent of source packaging
- fix(deps): pin spaCy 3.8.13 to restore Python 3.14 installations and container builds
