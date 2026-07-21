# Core Concepts and Capabilities

[中文](concepts.md) · English

[← Back to Architecture and Core Concepts](README.en.md)

## Current capabilities

- **Multiple LLM providers**: Supports Anthropic, OpenAI, DeepSeek, Qwen, Zhipu, and MiniMax, with hot model switching through the API or tools. `providers.json` can define multiple named instances of the same provider type—for example, several Zhipu keys—each with its own default model.
- **Layered memory**: Short-term context is compressed automatically. **mem0** manages long-term memory on top of ChromaDB and a local SentenceTransformer, with automatic deduplication and semantic merging. When a new message arrives, the system retrieves relevant memories and injects them under the `[自动回忆]` (automatic recall) marker; memories already recalled or written are not injected twice in the same session, even after a restart. Short-term memory is also restored automatically after a restart.
- **Participant isolation**: Each `participant_id` has an independent conversation thread, preventing context from different users from bleeding together.
- **Three interaction channels**: File inbox/outbox, REST API, and real-time SSE/WebSocket communication.
- **Tool system**: File access, code execution, web search, browser automation, memory operations, skill loading, task board operations, model switching, and more.
- **Visual analysis**: After configuring `LLM__VISION_PROVIDER/MODEL`, a text-only model such as DeepSeek can call `visual_analyze` and delegate image or video understanding to a vision model. Video is sent as native Base64 input and requires a vision model that declares video support. If encoded input reaches 10 MiB, Coworker first tries to compress it with FFmpeg.
- **Bubble thinking** (optional): With `AGENT__BUBBLE_THINKING=true`, the model can proactively fork independent subtasks from the current context and run them concurrently, then merge their conclusions automatically. The main line and bubbles can communicate in both directions. When a bubble is bound to a `participant_id` (and optionally a `conversation_id`), subsequent matching, unambiguous communication is handed directly to that active bubble; it can reply only to its bound participant. Use `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_PARTICIPANT_MATCHES` to enable visible handoff for selected communication IDs with full-ID globs. Live WebSocket/SSE sessions also enable it by transport by default; use `AGENT__BUBBLE_HANDOFF_TRANSPARENCY_STREAM_TRANSPORTS` to narrow or disable that behavior. The default participant globs match only WeCom and the Desktop `local` actor, not Claude or Codex actors. The transfer, direct replies, and completion are labeled as coming from the Bubble. After reaching the cycle limit, a Bubble can continue from its retained context with `bubble_spawn(bubble_id=...)` during the configured grace period.
- **Subconscious thinking** (optional): With `AGENT__SUBCONSCIOUS_THINKING=true`, the system triggers several types of background reflection: **self-audit**, **experience summarization** (extracting durable experience into long-term memory immediately before short-term compression), free association, skill-library review, palace gardening, and more. Trigger cadence and behavior live in `.coworker/subconscious/*/MODE.md`; environment variables retain only the global switch, the pre-compression summary switch, and a general `max_cycles` fallback. The process stays silent and does not interrupt the main workflow.
- **Skill system**: Loads `SKILL.md`-style natural-language operating guides from `.coworker/skills`.
- **Memory palaces (Memory Block Tree)**: Loads domain packages from `.coworker/palaces` through `PALACE.md`. Each palace is a domain composition layer: a thin orientation card plus pointers to skills (procedures) and long-term memories (facts). Only a compact registry—name plus attachment condition—stays in the system prompt. The full palace is injected on demand into the task's bubble: critical skills are loaded in full, related skill names are listed for optional loading, and long-term memories are recalled by `memory_tags`. When a bubble finishes successfully, its conclusions are written back to long-term memory under the palace tags, allowing the palace to keep growing through use.
- **Identity system**: Loads a name and personality from `data/identity`. On first startup, Coworker may begin in an unnamed newborn state.

## Runtime i18n and prompt localization

Coworker renders its own model-visible framework text using an instance-wide locale. The first
release supports `zh-CN` and `en`, defaulting to `zh-CN`. The locale lives in an async-safe context;
prompt caches are partitioned by locale, and background model tasks inherit and retain the locale
present when they are created. It also drives weekday names, message-source labels, IP geolocation
request language, and the browser's default locale, while time zones and ISO date formats remain
unchanged.

Built-in text lives under `src/coworker/i18n/catalogs/<locale>/` in domain-specific TOML catalogs,
using semantic keys and `{{variable}}` placeholders. Startup and tests strictly require identical
keys, non-empty values, and placeholder sets across locales. An unknown locale or incomplete
built-in catalog fails closed, so a missing key never leaks to a model. Tool names, parameter names,
enum values, IDs, tags, actual data, and original third-party exception text remain unchanged.

User-maintained Markdown assets may provide localized companion files. Lookup order is exact locale,
base language, then the original file:

- `SKILL.en.md` overrides only `description` and the body;
- `PALACE.en.md` overrides only `when_to_attach` and the body;
- `MODE.en.md` overrides only `goal`, `purpose`, `retire_after`, and the body;
- `thinking.en.md` and identity prose such as `personality.en.md` override the matching prose.

Names, stable IDs, tags, scheduling fields, and other operational metadata always come from the
original file. A missing companion falls back silently; malformed content or mismatched placeholders
emit a load warning and fall back without invalidating the original asset. Existing snapshots,
memories, tasks, alarms, logs, and user assets are neither migrated nor translated. For mem0,
Coworker localizes only its own wrappers and custom instructions that require preserving source
language; prompts owned internally by mem0, model providers, or other third-party libraries are not
copied or monkey-patched.

## Main tools

The following tools are registered by default at startup:

- File tools: `read_file`, `write_file`, `list_directory`, `find_files`, `grep_files`
- Web tools: `search_web`, `fetch_url`
- Browser tools: `browser_open`, `browser_screenshot`, `browser_action`, `browser_get_content`, `browser_close`, `browser_list_sessions`
- Code tools: `execute_code`, `get_code_result`, `kill_code_job` (`execute_code` waits at most two seconds by default; `block=true` applies only in bubble context and is ignored on the main line. `get_code_result` returns only the current state and does not wait; call `sleep` before retrying when a wait is needed.)
- Memory tools: `query_memory` (unified search: `query` searches both the recent-activity index and long-term memory; `start`/`end` recall or filter a time window; `query` can be combined with `start`/`end`), `manage_memory`, `clear_short_term_memory` (manually compress all primary memory without deleting it), `manage_pinned_context`
- System tools: `sleep`, `switch_model`, `get_context`, `restart_self`
- Alarm tools: `set_alarm`, `list_alarms`, `cancel_alarm`
- Communication tools: `communicate`, `list_ws_connections`
- Skills and tasks: `get_skill`, `task_board`

`visual_analyze` is registered by default and is visible to both text and vision main models. It becomes available after `LLM__VISION_PROVIDER` and `LLM__VISION_MODEL` are configured:

- `visual_analyze`: Performs visual analysis and reasoning over images or videos from local paths or HTTP(S) URLs. Video requires native video support from the vision model, and the Base64 data URL must remain below 10 MiB; Coworker tries FFmpeg compression when it exceeds the limit.

The following tools are visible only to models with vision capability:

- `view_image`: Loads an image from a local path or URL for direct inspection and supports `full_resolution`.
- `browser_view`: Captures and inspects the current browser page and supports `full_resolution`.

## Directory structure

```text
coworker/
├── .coworker/skills/        # Skill knowledge base
├── .coworker/palaces/       # Memory palaces (domain orientation cards)
├── examples/                # API and WebSocket examples
├── scripts/                 # Utility scripts
├── src/coworker/
│   ├── agent/               # Main loop, inbox watcher, interaction logs
│   ├── api/                 # FastAPI REST API + WebSocket
│   ├── brain/               # Multi-provider LLM abstraction
│   ├── core/                # Configuration, types, exceptions
│   ├── identity/            # Identity loading
│   ├── memory/              # Short-term memory, long-term memory, vector embeddings
│   ├── palaces/             # Memory-palace scanning and loading
│   ├── prompts/             # System prompt construction
│   ├── skills/              # Skill-file scanning and loading
│   └── tools/               # Tool implementations and registration
├── tests/                   # Unit tests
└── data/                    # Runtime data, created or written after startup
    ├── inbox/               # Incoming file messages
    ├── outbox/              # Outgoing file messages
    ├── memory/              # mem0/ChromaDB persistence + short_term_snapshot.json
    ├── identity/            # name.txt, personality.md, and related files
    ├── logs/                # Logs and interactions*.jsonl shards
    └── task_board.md        # File used by the task-board tool
```

## Memory system

Short-term memory is divided into the agent's own thought stream and participant-isolated conversation threads. When token usage reaches `MEMORY__COMPRESS_THRESHOLD`, the system compresses older messages through the **multiresolution memory tree** described below. Immediately before compression, the subconscious process also extracts durable information into mem0.

### Multiresolution memory tree (short-term memory LOD)

> This is different from the “Memory palace (Memory Block Tree)” below. Both names contain “block tree,” but this structure is a temporal-scale spine **inside short-term memory**, while a palace is a domain **composition layer**.

The design maps game-style LOD/mipmaps—full detail nearby and progressively coarser detail farther away—onto time. Instead of collapsing the oldest short-term history into a **single** summary, compression promotes it into a **tree leaf**. Binary level merging combines two nodes at the same level into one coarser node, forming a multiresolution spine: recent history remains as full-detail raw messages, while older history is represented by increasingly coarse summaries. Model context becomes `one time-labelled summary at each time scale + the raw tail`, keeping its size at O(log N) while preserving awareness of larger time scales such as “what happened last week?” or “what was the main thread today?” Whenever possible, merging re-summarizes the relevant time range from raw logs instead of summarizing summaries, limiting progressive degradation.

- **Time-window recall / unified search**: `query_memory(start=..., end=...)` first uses short-term memory-tree summaries to recall a period; if no summary exists, it falls back to raw logs and generates one. `query_memory(query=...)` searches both the recent-activity index and mem0 long-term memory and returns five combined results by default. `query_memory(query=..., start=..., end=...)` performs semantic search focused on the specified time window. Inline query output is limited to 3,000 characters. The complete frozen result is written to a temporary Markdown file with a `read_file` path and section line numbers for stable paging and expansion. A call without arguments fails; provide `query`, or provide both `start` and `end`.
- **Manual full compression**: `clear_short_term_memory` compresses every remaining live message in the current `primary` stream into the memory tree. It releases context space without deleting memory and triggers subconscious `summarize` first to extract long-term memories. If a tool is currently running, the final `tool_use` is retained to preserve message structure.
- **History backfill** (upgrade migration): Upgrades do not backfill by default; the memory tree starts growing from new compression events. To build a multiscale tree from **existing history**, Coworker reads every `interactions*.jsonl` shard, divides the history by time, summarizes each block into a leaf, and rebuilds the spine through cascading merges. `MEMORY__TREE_BACKFILL_MAX_LEAVES` caps the number of generated leaves. Two methods are available:
  - **Offline** (while the process is stopped): `uv run python -m coworker --backfill-tree` rebuilds the tree, writes the snapshot, and exits.
  - **Online** (while running): `POST /backfill_tree`, optionally with `{"max_leaves": 64}`. This operator-triggered operation has no model-token cost. It rebuilds asynchronously in the background, logs progress for each block, and exposes progress through `GET /backfill_tree` as `{running, done, total}`. On completion it logs the result and pushes a system message to the inbox; duplicate triggers return 409. A temporary-tree build followed by atomic replacement under the compression lock keeps the live tree untouched during construction and preserves nodes compressed while the backfill was running. Do not run the offline CLI while the process is active; both would contend for the snapshot file.
- **Fallback**: Set `MEMORY__TREE_ENABLED=false` to restore the legacy single compressed-summary anchor.

**Pinned messages**: The `manage_pinned_context` tool lets the model pin important text or file content. A pin exists as a real message in the conversation stream. After short-term compression removes it, the next cycle detects that it is missing and appends it to the end of `primary` as the newest input, preserving cache hits while periodically emphasizing it. File pins reread their files every time they are reinjected, so they always contain current content. Pin state is persisted in snapshots and restored after a restart.

**mem0** manages long-term memory and persists it under `MEMORY__DB_PATH` with ChromaDB. Embeddings use the local `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` model by default and load automatically on first use. mem0 merges or deduplicates semantically similar memories during writes to prevent redundant accumulation.

**Recent-activity index**: Interactions from the last `MEMORY__RECENT_ACTIVITY_DAYS` days that have already been compressed out of short-term `primary` are written to a separate `recent_activity_v1` Chroma collection. This supports recall of what was recently done, tool results, errors, and final states. Compression schedules indexing in the background; activities still retained as raw short-term messages are not indexed twice. Ordinary events are indexed one event at a time, while a tool call and its corresponding result are combined into one item so arguments, status, and output are recalled together. A semantic match acts only as an anchor: the system expands it from raw logs into the complete activity chain—received message, visible response, tool calls, and results—and injects it as an activity replay. Internal reasoning, retrieval scores, and slicing metadata are excluded. Noise such as `message_tick`, system prompts, automatic recall, and model usage is not indexed. Long tool evidence remains one logical item externally but is divided internally with the current embedder's tokenizer into overlapping windows of 120 tokens by default with 24-token overlap, so details near the end remain searchable even when the embedding model's input length is exceeded.

**Automatic recall**: Whenever a user message arrives, the system semantically searches long-term memory and injects results above the relevance threshold under the `[自动回忆]` marker. A stricter threshold can also search recent activity and inject a few highly relevant excerpts under `[自动回忆·历史活动回放]`. IDs of memories already recalled or written through `write_memory` are stored in `Message.recalled_memory_ids` and persisted in snapshots, preventing duplicate injection in the same session, including after a restart.

**Data migration**: Before the first upgrade to a mem0-based version, the old ChromaDB format is incompatible with mem0 and must be cleared:

```bash
rm -rf data/memory/
# Alternatively, set MEMORY__DB_PATH=data/memory_v2 in .env
```

### Restart state inheritance

At the end of each cycle, short-term memory is snapshotted automatically to `data/memory/short_term_snapshot.json`; shutdown performs one final defensive save. If the snapshot exists at the next startup, Coworker restores conversation history and removes incomplete trailing tool-call chains. It then injects a system restart notice containing the current time and the number of restored alarms.

The agent can trigger a safe restart through `restart_self`, for example after updating its own code. The restart flow first validates the code environment with `--check`, then saves a snapshot and replaces the process in place. The new process restores short-term memory, and the `restart_self` tool result is replaced with a real success message containing the number of restored messages and alarms.

Alarm state is persisted to `data/memory/alarms.json` and rescheduled after restart. Missed alarms fire immediately with their lateness included in the message. Repeating alarms fire only once to catch up, then continue on their original interval.

For a clean start, delete the snapshot files:

```bash
rm data/memory/short_term_snapshot.json
rm data/memory/alarms.json  # Also clear alarm state (optional)
```

## Memory palaces (Memory Block Tree)

Memory palaces isolate domain context for **specialized task execution** from the continuously running conversation stream so the two do not contaminate each other. A palace is not a new storage system; it is a **composition layer** that assembles a domain orientation card, critical skills, and relevant long-term memories into one attachable unit.

Palaces live at `.coworker/palaces/<name>/PALACE.md` and use a structure similar to a skill:

```markdown
---
name: product-bug
when_to_attach: A user reports a defect or unexpected behavior in the example product and it must be verified and filed as a bug
critical_skills: [bug-create]             # Load the complete body when spawning a bubble
related_skills: [issue-tracker, product-config]  # List names on the card; load on demand with get_skill
memory_tags: [product, bug]                # Recall matching long-term memories by tag
---

# Domain orientation card (thin, ≤ ~800 tokens)
# Keep only orientation: domain mental model, common mistakes, pointers, and when to ask questions.
# Put procedures in skills and facts in mem0—the card only tells the model what it is doing and where to find the rest.
```

**How it works**:

- Only the `[PALACES]` registry—name plus `when_to_attach`—stays in the system prompt, preserving a stable prompt prefix for caching.
- When the main line encounters a specialized task matching a palace, it spawns a bubble with `bubble_spawn(palaces=[...])`. A specialized execution should generally use `fresh_start=true` for clean domain context, and one bubble can attach multiple palaces.
- Bubble startup injects the complete bodies of critical skills, the palace orientation card, and long-term memories recalled through `memory_tags`.
- When the bubble finishes successfully, its conclusions are written back to mem0 under the palace's `memory_tags` by a deterministic hook. The next attachment recalls them by tag, so the palace keeps growing through task execution.
- The subconscious system includes a **palace gardener** (`garden` mode). It inspects one palace's domain memories, removes stale, contradictory, or redundant material, and consolidates missing facts. Its cadence is controlled by `use_threshold`, `every_seconds`, and `min_interval_seconds` in `garden/MODE.md`. The gardener runs serially and treats the orientation card as read-only; proposed card edits are sent to the main line.

Configure the palace directory with `AGENT__PALACES_DIR`; the default is `.coworker/palaces`.

> `.coworker/palaces/` is currently excluded from version control because model-maintained cards are still being evaluated. The repository does not ship an example palace; the sample above documents only the format.
