# Glossary

[中文](glossary.md) · English

[← Back to documentation index](README.en.md)

| Term | Meaning |
|---|---|
| Coworker | The persistent Agent instance and identity, not only a client |
| Main line | Primary Agent loop that owns persistent conversation context and ordinary inbound messages |
| Participant | Stable communication identity used for routing and short-term isolation, not account authorization |
| Conversation | Optional additional conversation identifier below a participant |
| Channel | Independent inbound/outbound transport such as WeCom, Weixin, or Stream |
| Stream | Shared runtime for WebSocket, SSE, and Desktop profile registration, connection, and outbox |
| Bubble | Parallel task with independent context and tool scope; may communicate with main or bind a participant |
| Subconscious | MODE-scheduled background Bubble for summary, audit, exploration, gardening, or meta-reflection |
| Skill | Reusable procedural knowledge defined in `SKILL.md` |
| Palace | Domain composition layer in `PALACE.md`, assembling a thin card, Skills, and tagged memory in a Bubble |
| Identity | Persistent name, personality, current location, and profile material |
| Short-term memory | Messages, tool calls, and compressed anchors directly visible to the main line |
| Memory tree | Multiresolution structure that compresses short-term history across time scales |
| Long-term memory | mem0-managed persistent facts or experience with semantic search and tags |
| Pinned context | Small critical text or file content reinjected after compression |
| Provider | Implementation of a model API dialect such as Anthropic, OpenAI, or DeepSeek |
| Provider instance | One uniquely named Provider configuration with key, Base URL, and default model |
| fallback | Ordered Provider/model chain used after main-model failure |
| Relay | Self-hosted single-node entry point forwarding end-to-end-encrypted bytes between remote Desktop and Coworker |
| Bridge | Rust runtime in Desktop connecting Local, Codex, Claude Code, and Coworker |
| Emergency backup | Short-term-context snapshot after repeated Agent errors, not a full disaster backup |
| `restart_self` | Main-line tool that validates the current code, snapshots context, and requests a safe launcher restart |

See [Runtime Architecture and Message Flow](architecture/runtime-flow.en.md) and
[Core Concepts and Capabilities](architecture/concepts.en.md) for the relationships.

[← Back to project home](../README.en.md)
