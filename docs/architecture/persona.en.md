# Persona: The optional people sub-mechanism

English · [中文](persona.md)

[← Back to architecture and core concepts](README.en.md)

> Person is an **optional, lightweight sub-mechanism**: it gives the model a "person" abstraction for recognizing the same human across multiple channel addresses, maintaining a persona card per person, and merging duplicate people. It embeds into existing mechanisms and can be turned off entirely with `MEMORY__PERSONA_ENABLED=false` — disabled behavior is identical to today.

In the lifeform philosophy, "relationship" is one of the life concepts that constitute the persistent Coworker (see [virtual-life philosophy](lifeform-philosophy.en.md)). Person carries that relationship: `person_id` is the stable anchor; cross-channel address bindings and the agent-maintained card are organized around it and survive restarts through `data/persons.json` and the card files.

## What it does and does not do

**Does**:

- **Cross-channel merging**: bind multiple `participant_id` values (optionally with `conversation_id`) to one Person;
- **Agent-maintained cards**: one markdown card per Person, rewritten wholesale by the main model during conversations through the `persona` tool (so knowledge can be corrected or forgotten), injected before the person's first message of the session;
- **Merge duplicate people**: the model or a caretaker can merge two Person entities into one.

**Does not**:

- No multi-tenant/account system (`person_id` is the future seam for one, but no accounts are built in);
- No participant-kind classification — whether an address is a person is judged by the model from the `[CHANNELS]` channel semantics;
- No system-prompt guideline rewrites, no roster injection (the people list is queried on demand through the `persona` tool);
- No mem0 changes (long-term memory stays in its single bucket; per-person knowledge is carried by the card plus `relationship` memories tagged with participant ids);
- No background tasks, no automatic person creation (people are only created explicitly via `bind`).

## How it works

### ① Remembering who is who — PersonStore and card files

- `data/memory/persons.json`: `Person` (`person_id`, `display_name`, `aliases[]`).
- Each address is `{participant_id, conversation_id?, channel, notes[]}`. `conversation_id` is recorded only when the channel needs it to locate a specific conversation or human (e.g. a Weixin session); addresses uniquely routable by `participant_id` alone (e.g. `wecom:single:*`) omit it. `notes` accumulate per address — the same channel can carry several notes, appended by the agent across `bind` calls.
- `data/memory/cards/{person_id}.md`: the agent-maintained card, rewritten wholesale rather than appended.

### ② Maintaining knowledge — the `persona` tool

- `persona(action="bind", participant_id, conversation_id?, person_id?/name?, note?)`: bind an address to a known person (by `person_id` or name) or create a new one; `note` is appended to the address's notes;
- `persona(action="card", person_id, content?)`: empty `content` reads the card; non-empty `content` rewrites it wholesale. When and how a card is created is the model's own judgment;
- `persona(action="merge", keep_person_id, drop_person_id)`: union addresses into the kept person and delete the other entity; `relationship` memories stay in the single bucket, unmoved.

### ③ Bringing knowledge into context — first-message injection

When the main loop processes inbound messages it looks up a binding by `participant_id` (optionally with `conversation_id`): if found and the card has not appeared in this session (dedup via `source="persona_card:{person_id}"`), the card is injected **before the person's first message of the session**. Unbound, group and system messages get no card and are handled as usual. The injected card carries `person_id` so the model can reference it in later `bind`/`card` calls.

### ④ Channel-provided semantics — the `[CHANNELS]` prompt

Each channel describes address semantics in its existing `agent_instructions()` (the `[CHANNELS]` section): e.g. wecom's `wecom:single:*`=a person, `wecom:group:*`=a group; weixin's `weixin:{bot}`=a 1:1 connection, `conversation_id`=the session, `weixin:control`=control messages. The model uses this to judge who is bindable and what `conversation_id` means.

### Soft boundary

By default memory/card lookup targets only the current conversation person (the model copies `participant_id` from the message header); cross-person reads require explicit parameters. In shared deployments, recent-activity auto-recall is filtered by the resolved participant so other people's recent events are not fed to the current conversation.

## Configuration and data

```env
MEMORY__PERSONA_ENABLED=true        # disabled behavior is identical to today
MEMORY__PERSONA_STORE_PATH=data/memory/persons.json
MEMORY__PERSONA_CARDS_DIR=data/memory/cards
```

The admin API exposes `GET/POST /api/admin/persons`, `GET/PATCH/DELETE /api/admin/persons/{id}`, `POST /api/admin/persons/{id}/merge`, `GET/PUT /api/admin/persons/{id}/card` (admin token required; DELETE also removes the card file; merging moves the dropped person's card into the kept person).

## Boundaries and notes

- **Groups / senders inside groups**: `wecom:group:*` has no binding, so no person context; individual senders inside groups are not bound in v1 (a group is a communication target, not a Person).
- **Cards are model-maintained**: they may lag or contain fabricated content — treat them as untrusted input; when a card contradicts `relationship` memory, the card is the current understanding and memory is the recallable evidence; the agent reconciles them.
- **Mistaken binding of a non-person address**: no hard validation; constrained by `[CHANNELS]` knowledge and tool descriptions, fixable from the admin panel.
- **Bubble inheritance**: a card already injected on the main line flows into forked bubble context; fresh-start bubbles need an explicit load (future capability).
- **States are not conflated**: card (current understanding) ≠ `relationship` memory (recallable facts) ≠ logs (records); stored separately with distinct semantics.

[← Back to project home](../../README.md)
