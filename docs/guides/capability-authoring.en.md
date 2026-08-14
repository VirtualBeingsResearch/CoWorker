# Authoring Capability Content

[中文](capability-authoring.md) · English

[← Back to Web Management Console](README.en.md)

Coworker has three user-maintained capability assets. A Skill defines how to work, a Palace
defines when to compose domain capabilities, and a subconscious mode defines when to reflect in
the background. All may enter model context, so review, test, and version them like code.

| Asset | Main file | Put here | Keep elsewhere |
|---|---|---|---|
| Skill | `.coworker/skills/<slug>/SKILL.md` | Stable procedures, checklists, and tool constraints | Large sets of changing facts |
| Palace | `.coworker/palaces/<slug>/PALACE.md` | Thin domain card, Skill pointers, and memory tags | Full procedures or fact stores |
| Subconscious mode | `.coworker/subconscious/<slug>/MODE.md` | Trigger, background goal, permissions, and exit | Ordinary foreground tasks |

## Create a Skill

```markdown
---
name: incident-review
description: Review an incident and produce verifiable improvements
version: 1
---

# Incident review

1. Fix the timeline and evidence first.
2. Separate direct causes, systemic causes, and unknowns.
3. Give every improvement an owner and verification method.
```

`name` is required and globally unique. `description` determines when the Agent discovers it.
Define trigger, inputs, steps, stop conditions, recovery, and prohibited actions. Never include
tokens, personal data, or unreviewed web instructions.

## Create a Palace

```markdown
---
name: reliability
when_to_attach: Handling incidents, alerts, or recovery drills
critical_skills: [incident-review]
related_skills: [deployment-check]
memory_tags: [reliability, incident]
---

# Reliability orientation

Preserve evidence and recoverability before changing state. Recall facts from tagged memory.
```

`critical_skills` are injected in full into the Bubble. `related_skills` are listed for on-demand
loading. `memory_tags` drive recall and writeback. Keep the body thin and stable: mental model,
common traps, and pointers. Put procedures in Skills and facts in long-term memory.

## Create a subconscious mode

Mode frontmatter supports `periodic`, `garden`, `cold_floor`, and `manual` triggers, with cycle,
time, tool-call, or cold-floor cadence. The body may use `{bubble_id}`, `{goal}`, and `{max_cycles}`.

A mode can also opt into an independent pre-compression trigger. Set `pre_compress: true`, use
`every_n_compressions` for the compression-event cadence, and use
`pre_compress_min_interval_seconds` for a wall-clock floor; both cadence conditions must be met.
`pre_compress_context: slice` passes only the messages about to be compressed, while `full` passes
the complete main-line context before compression. This trigger can coexist with `periodic`, for
example using compression boundaries as the primary trigger and a tool-call threshold as a
fallback when compression is delayed.

Start from the closest existing `.coworker/subconscious/*/MODE.md`. Define at least:

- `name`, `enabled`, `trigger`, and `max_cycles`;
- `goal` and a durable `purpose`;
- a clear output path: memory, task, main-line notice, or silent completion;
- `retire_after`, describing when to pause or archive it;
- `protected: true` only for a core safety or integrity mode.

A background Bubble's `bubble_done` result is not delivered to the main line by default. Use
`bubble_send(target="main", ...)` or an allowed persistent store when the main line must see it.

## Localization

Chinese main files use `SKILL.md`, `PALACE.md`, and `MODE.md`; English companions use
`SKILL.en.md`, `PALACE.en.md`, and `MODE.en.md`. Companions translate only supported prose:

- Skill: `description` and body;
- Palace: `when_to_attach` and body;
- Mode: `goal`, `purpose`, `retire_after`, and body.

Keep `name`, tool names, tags, triggers, IDs, and stable metadata identical.

## Validate and iterate

1. Save through Capability Content and inspect YAML, duplicate-name, and companion warnings.
2. Inspect Current System Prompt and confirm only the intended thin registry is resident.
3. Use one explicit task to verify Skill discovery.
4. Verify Palace attachment, critical Skills, and tagged memories.
5. Start a subconscious mode manually or at low frequency, then inspect output, cost, and authority.
6. Review tasks, Bubble/subconscious records, and audit before increasing cadence.

Search for references before renaming or deleting an asset. Treat third-party capability content
as untrusted input and review it before making it persistent.

[← Back to project home](../../README.en.md)
