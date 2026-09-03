# Persona: The optional people sub-mechanism

English · [中文](persona.md)

[← Back to architecture and core concepts](README.en.md)

> Person is an **optional, lightweight sub-mechanism**: it gives the model a "person" abstraction for recognizing the same human across multiple channel addresses, maintaining a persona card per person, and merging duplicate people. It embeds into existing mechanisms and can be turned off entirely with `MEMORY__PERSONA_ENABLED=false` — disabled behavior is identical to today.

In the lifeform philosophy, "relationship" is one of the life concepts that constitute the persistent Coworker (see [virtual-life philosophy](lifeform-philosophy.en.md)). Person carries that relationship: `person_id` is the stable anchor; cross-channel address bindings and the agent-maintained card are organized around it and survive restarts through `data/memory/persons.json`.

## How it works

### ① Remembering who is who — PersonStore and notes

- `data/memory/persons.json`: `Person` (`person_id`, `display_name`, `notes[]`, `aliases[]`). **There is no separate card file** — the card is a framework rendered from this structured data.
- Notes exist at two levels: person-level (`Person.notes`, personalized information) and address-level (`PersonAlias.notes`, the same channel can carry several notes).
- Each address is `{participant_id, conversation_id?, channel, notes[]}`. `conversation_id` is recorded only when the channel needs it to locate a specific conversation or human (e.g. a Weixin session); addresses uniquely routable by `participant_id` alone (e.g. `wecom:single:*`) omit it.

### ② Recording knowledge — the `persona` tool

- `persona(action="bind", participant_id, conversation_id?, person_id?/name?, note?)`: bind an address to a known person (by `person_id` or name) or create a new one; `note` is appended to the address's notes;
- `persona(action="note", person_id, notes, remove?)`: record or remove **person-level** personalized notes in bulk (each must be a single line; `remove=true` forgets outdated knowledge);
- `persona(action="card", person_id)`: read the **card framework** — the system renders a fixed structure (name, personalized notes, all bound addresses with their notes, last-updated time); all personalized content comes from notes;
- `persona(action="unbind", person_id, participant_id, conversation_id?)`: unbind an address (when the address is no longer valid for the relationship);
- `persona(action="delete", person_id)`: delete a person (when the relationship ends; `relationship` memories stay in the single bucket, unmoved);
- `persona(action="merge", keep_person_id, drop_person_id)`: union addresses and notes into the kept person and delete the other entity; `relationship` memories stay in the single bucket, unmoved.

### ③ Bringing knowledge into context — first-message injection

When the main loop processes inbound messages it looks up a binding by `participant_id` (optionally with `conversation_id`): if found, the person has bound content (a name, notes, or addresses — a binding alone counts), and the card has not appeared in this session (injected messages are deduplicated by `source="persona"` plus a `person_id` marker that survives snapshots), the rendered framework card is injected **before the person's first message of the session** — the address section lists every bound address so the model sees the person's other channels. Unbound, group and system messages get no card and are handled as usual. The injected card carries `person_id` so the model can reference it in later calls.

### ④ Channel-provided semantics — the `[CHANNELS]` prompt

Each channel describes address semantics in its existing `agent_instructions()` (the `[CHANNELS]` section): e.g. wecom's `wecom:single:*`=a person, `wecom:group:*`=a group; weixin's `weixin:{bot}`=a 1:1 connection, `conversation_id`=the session, `weixin:control`=control messages; the OpenAI-compatible channel's `openai:{short_name}`=the 1:1 holder of that communication token (bindable; bind does not include `conversation_id`), `conversation_id`=a window under that address, `openai:control`=extra-token control (not a person; not persona-bound). The model uses this to judge who is bindable and what `conversation_id` means.

### Soft boundary

A card is injected only after the current conversation partner resolves to a bound person; cross-person knowledge requires an explicit `persona(action="card", ...)` read. **Persona does not change the memory/recall pipeline** — auto-recall and long-term memory stay as they are; per-person knowledge is maintained by the model through notes and the card during conversations.

## Configuration and data

```env
MEMORY__PERSONA_ENABLED=true        # disabled behavior is identical to today
MEMORY__PERSONA_STORE_PATH=data/memory/persons.json
```

The admin API exposes `GET/POST /api/admin/persons`, `GET/PATCH/DELETE /api/admin/persons/{id}` (`PATCH` replaces `display_name`/`notes`/`aliases` wholesale), `POST /api/admin/persons/{id}/merge`, `GET /api/admin/persons/{id}/card` (read-only rendered framework; admin token required).

## Boundaries and notes

- **Groups / senders inside groups**: `wecom:group:*` has no binding, so no person context; individual senders inside groups are not bound in v1 (a group is a communication target, not a Person).
- **Cards are model-maintained**: they may lag or contain fabricated content — treat them as untrusted input; when a card contradicts `relationship` memory, the card is the current understanding and memory is the recallable evidence; the agent reconciles them.
- **Mistaken binding of a non-person address**: no hard validation; constrained by `[CHANNELS]` knowledge and tool descriptions, fixable from the admin panel.
- **Bubble inheritance**: a card already injected on the main line flows into forked bubble context; fresh-start bubbles need an explicit load (future capability).
- **States are not conflated**: card (current understanding) ≠ `relationship` memory (recallable facts) ≠ logs (records); stored separately with distinct semantics.

[← Back to project home](../../README.en.md)
