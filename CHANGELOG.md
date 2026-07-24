# Changelog

## Unreleased

- feat(desktop): improve the collapsed sidebar and short log ledger, make tray residency optional, reorder connection profiles, add message copy/quote actions and incoming-message notifications, clarify offline history, derive missing Codex titles, and discover Codex App or npm-installed binaries across platforms

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
