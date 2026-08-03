# Changelog

## Unreleased

## 0.3.6 - Unreleased

- docs(release): finalize v0.3.5 changelog (#112)
- docs: require reading project contribution guidance (#113)
- feat(dev): add Linux dev container (#115)
- fix(alarm): prevent duplicate restored triggers (#117)
- chore(lint): enable high-signal correctness checks (#118)
- fix(ui): refine desktop interface details (#119)
- feat(scripts): add Coworker notification helper (#120)
- fix(container): unify runtime and agent workspace (#122)
- fix(agent): keep passive startup asleep (#123)
- feat(relay): add end-to-end encrypted path relay (#116)
- chore(ci): upgrade Node.js to 24 (#126)
- Bump the "all-dependencies" group with 6 updates across multiple ecosystems (#127)
- docs: complete user and operations guides (#125)
- fix(subconscious): persist Bubble model metadata (#128)
- fix(memory): unify automatic compression budget (#129)
- fix(logging): raise interaction log rotation threshold (#130)
- feat(task): pin active-task overview and simplify reminders (#131)
- fix(admin): default memory tail panel to latest messages (#133)
- fix(vision): return analysis results inside bubbles (#135)
- fix(admin): live-refresh the current memory message tail (#134)

## 0.3.5 - 2026-07-26

- fix(desktop): apply onboarding update URL default (#107)
- fix(release): make drafts revisable before publish (#108)

## 0.3.4 - Unreleased

- docs(release): finalize v0.3.3 changelog (#76)
- feat(desktop): add launch and bridge autostart settings (#77)
- fix(desktop): restore Unix command discovery (#78)
- feat(desktop): add version statistics (#79)
- build(deps): bump the vite group across 3 directories with 1 update (#85)
- build(deps): bump clap from 4.6.2 to 4.6.4 (#96)
- build(deps): bump lucide-react in /apps/coworker-desktop/desktop (#97)
- build(deps): bump tauri-plugin-single-instance from 2.4.2 to 2.4.3 (#94)
- build(deps): bump @tauri-apps/plugin-dialog (#95)
- build(deps-dev): bump @testing-library/jest-dom (#93)
- build(deps): bump mcp2cli from 3.0.3 to 3.3.1 (#91)
- build(deps): bump anthropic from 0.117.0 to 0.120.0 (#88)
- build(deps): bump tauri-plugin-dialog from 2.7.1 to 2.7.2 (#87)
- build(deps): bump python-multipart from 0.0.29 to 0.0.32 (#86)
- build(deps): bump regex from 1.13.0 to 1.13.1 (#84)
- build(deps): bump actions/cache from 5 to 6 (#82)
- build(deps): bump astral-sh/setup-uv from 8.3.2 to 9.0.0 (#81)
- build(deps): bump thiserror from 2.0.18 to 2.0.19 (#90)
- build(deps): bump lucide-react from 1.25.0 to 1.27.0 in /web (#98)
- build(deps): bump openai from 2.35.1 to 2.48.0 (#83)
- fix(admin): align destructive action confirmation (#99)
- build(deps): bump the react group across 3 directories with 4 updates (#92)
- chore(deps): group Dependabot updates (#100)
- fix(frontend): support TypeScript 7 module resolution (#102)
- fix(desktop): bootstrap native Codex coworker replies (#104)
- fix(desktop): keep Codex warnings local (#103)
- feat(channels): add extensible Weixin module (#80)

## 0.3.3 - 2026-07-25

- feat(container): initialize persistent Git workspaces from an embedded repository bundle, support configurable repository sources, and expose container health without application code changes
- feat(desktop): improve the collapsed sidebar and short log ledger, let users choose and remember the close behavior on first close, reorder connection profiles, preserve each conversation's scroll position across actor switches, add message copy/quote actions, local Markdown and attachment image previews, contextual incoming/completion notifications with the bundled app icon, and 25-item Codex/Claude history paging, clarify offline history, derive missing Codex and local-chat titles from their first meaningful message without repeatedly scanning full histories, safely resume native Codex App/CLI sessions from CoWorker, discover Codex App bundled binaries on Windows, macOS, and Linux, including npm Node resolution, recover actor registration and streams while the Coworker channel runtime starts or restarts, and preserve server error details such as an unconfigured communication token
- fix(runtime): keep one lightweight Windows supervisor, hard-exit the fully torn-down worker on restart, and relaunch a single replacement worker so Python threads and application memory do not accumulate across restarts
- fix(memory): resolve mem0 through the same named Brain provider credentials and effective base URL, adapt all OpenAI-compatible providers by API dialect without a second model allowlist, and isolate mem0 from unrelated OpenRouter environment settings
- fix(memory): keep Mem0 configuration provider names within its validated API dialects while replacing their implementations with Coworker endpoint and TLS adapters
- fix(desktop): use complete GitHub release-asset digests to detect identical or conflicting imported releases before downloading binaries, while retaining download verification for legacy APIs without digests
- fix(channels): persist participant last-sent and last-received timestamps in a shared Channel activity store so connection context survives application restarts
- fix(channels): reject unknown built-in participant IDs without sending or auto-correcting them, suggest complete known IDs only within four edits, and retain offline registered Stream participants as valid outbox targets
- feat(wecom): keep the WeCom Channel runtime registered while disabled or incomplete, and hot-apply admin configuration by enabling, disabling, or reconnecting its WebSocket without restarting Coworker
- fix(admin): continue filtered lifetime-history searches across empty bounded scan windows until a match or the beginning of retained history is reached
- fix(admin): index completed Bubble and subconscious transcripts so management lists avoid repeatedly parsing full logs, while recovering legacy or missed index records
- fix(wecom): stop emitting unsupported member-mention markers or advertising `mentioned_list`, omit unnecessary `conversation_id` values from direct messages, keep group sends without `conversation_id` proactive, and preserve native stream replies and plain Markdown delivery
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
