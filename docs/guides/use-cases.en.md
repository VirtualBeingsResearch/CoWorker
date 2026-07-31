# Common Use Cases

[中文](use-cases.md) · English

[← Back to Web Management Console](README.en.md)

These scenarios compose existing capabilities. They do not grant Coworker additional authority.
Keep human review for consequential operations.

## Personal persistent companion

Goal: continue project context, tasks, and reminders across days.

1. Set name and collaboration style in Identity Profile.
2. Continue through a stable participant identity.
3. Put confirmed facts in long-term memory and pin only a small set of always-visible context.
4. Carry next actions through tasks and alarms.
5. Review stale tasks, memory, and usage weekly.

Do not pin an entire project corpus. Put procedures in Skills and facts in memory.

## Team project memory

Goal: let members share durable background while isolating short-term conversations.

- Use a separate `participant_id` for every member.
- Compose project orientation, critical Skills, and memory tags in a Palace.
- Retain important decisions only after human confirmation.
- Transfer results through a Channel or Desktop; do not mistake conversation isolation for authorization.

## Desktop multi-agent collaboration

Goal: coordinate Local, Codex, Claude Code, and Coworker in one workbench.

1. Connect the intended Coworker and inspect actor health.
2. Start work in the correct project and conversation.
3. Give Codex or Claude a bounded task.
4. Explicitly send the result to Coworker for synthesis, retention, or further work.
5. Inspect tool activity and destination participant before delivery.

## Coworker self-upgrade

Goal: let Coworker review and integrate upstream changes while preserving context.

- Ask her to inspect working tree, remotes, version, and backup first.
- Preserve local commits and require confirmation for conflicts or overwrites.
- Run checks relevant to the update.
- Call `restart_self` separately only after checks pass.
- Verify version, model, message path, and data after restoration.

See [Upgrading and Migration](../operations/upgrading.en.md) for the full boundary.

## Automation and custom Channels

Goal: connect an existing service to persistent context.

- Use `POST /messages` and SSE/WS for simple integrations.
- Keep participant and conversation identifiers stable.
- Implement `BaseChannel` or `StreamProfile` for independent transport semantics.
- Define retry, attachment, authentication, offline-outbox, and recovery contracts.
- Do not depend on `/api/admin/*` as a stable public API.

## Domain Palace

Goal: load enough domain background for specialist work without contaminating the main line.

- Keep the Palace card to mental model, attachment condition, and pointers.
- Put mandatory procedures in critical Skills.
- Load related Skills only when needed.
- Recall and write back facts through `memory_tags`.
- Review gardener output and stale memory periodically.

[← Back to project home](../../README.en.md)
