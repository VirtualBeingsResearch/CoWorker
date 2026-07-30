# Documentation Maintenance

[中文](documentation.md) · English

[← Back to Development and Collaboration](README.en.md)

This page complements the contributing guide with rules for keeping user documentation aligned
with a fast-moving product.

## Information architecture

- First-success journeys belong in `getting-started/`.
- Web operations and scenario tutorials belong in `guides/`.
- APIs, Channels, and clients belong in `channels/`.
- Configuration, deployment, upgrade, backup, and troubleshooting belong in `operations/`.
- Runtime model and trust boundaries belong in `architecture/`.
- Build, test, and release maintenance belongs in `development/`.

Do not create a top-level directory for one page. Keep the root README to product positioning and
the shortest successful path, linking details to an authoritative page.

## Bilingual pages

`docs/<path>.md` is Chinese and `docs/<path>.en.md` is English. Open and update both together:

- keep commands, paths, configuration keys, API fields, and product terms aligned;
- do not translate third-party text or protocol values into different semantics;
- link to the same-language companion;
- expose every new page from its domain index and the root documentation index.

## Screenshots and diagrams

Use one or two screenshots for a task's key interface. Prefer text, Mermaid, or structured examples
for reference tables and protocols.

- Use isolated synthetic data created from scratch; do not anonymize real data and reuse it as a
  demo.
- Clear tokens, API keys, real users, conversations, paths, and runtime records before capture.
- Capture Chinese and English interfaces separately.
- Store files under `docs/assets/screenshots/` with stable descriptive names.
- Update screenshots in the same PR when UI behavior changes.

Mermaid is appropriate for end-to-end flow, responsibility boundaries, and sequences; do not use
it instead of a three-line explanation.

### When to take a screenshot

A screenshot should help readers confirm either “I am in the right place” or “the operation
succeeded.” Prefer screenshots for:

- key entry points where first-time setup, connection, or authorization can be confusing;
- the most important operation state or successful result in a task;
- Desktop or Web layouts that are difficult to identify from text alone.

Do not capture every button or put parameters that readers must search, copy, or maintain into an
image. Use text when the interface changes frequently, one or two sentences explain the step, or a
screenshot would expose too much unrelated UI.

### Prepare an isolated synthetic scenario

Never capture a personal or production instance. Use a disposable workspace, browser profile, and
runtime data directory, then build the minimum scenario from a blank state. Do not load a personal
`.env`, `providers.json`, `data/`, browser sync account, or real Desktop history. The entire
disposable environment should be safe to discard after capture.

Synthetic content should be coherent, readable, and clearly invalid. For example:

| Content | Recommended example |
|---|---|
| Person and Coworker | `Alice`, `Atlas` |
| Project | `demo-project` |
| Local path | `/workspace/demo-project` |
| Provider / model | `demo-provider` / `demo-model` |
| Token / API key | Leave blank; use `demo-token-not-valid` only when the format must be visible |
| Base URL | `https://example.invalid/v1` |
| Conversation | Two or three short fictional messages related to the current guide |
| Usage, tasks, and logs | A small fixed set of fictional records, never copied from real records |

`.invalid` is a reserved invalid domain and is suitable for UI demonstrations. Do not use
realistic secret prefixes, corporate intranet domains, personal usernames, home-directory paths,
repository remotes, IP addresses, device names, or avatars. Synthetic content must also be
appropriate for a public repository.

If reaching the target state requires a Provider, use a dedicated demo account or a local mock and
make sure it cannot access real data. Do not temporarily disable production authentication for a
screenshot or repeatedly submit a fake key to a real Provider.

### Capture steps

1. State the user outcome the screenshot must prove and keep only the UI relevant to that outcome.
2. Populate the disposable environment with data like the examples above. Disable notifications,
   autofill, browser extension overlays, and unrelated windows.
3. Fix the window size, zoom, and theme. A Chinese/English pair must use the same viewport,
   interface state, and data semantics. Current Web documentation screenshots commonly use
   `1600 × 1000`; keep adjacent screenshots consistent.
4. Capture the `-zh` file from the Chinese UI, then switch the product to English and capture the
   `-en` file in the same state. Do not replace Chinese UI text with English in an image editor.
5. Crop browser tabs, the address bar, desktop, Dock, menu bar, and unrelated empty space, while
   retaining the navigation and title needed to identify the page.
6. Inspect every visible field at full size before adding the file to documentation.

Save interface screenshots as actual PNG files; reserve JPEG for photographic material. Do not
merely change the extension. Bilingual companions share a base name, for example:

```text
docs/assets/screenshots/admin-first-run-zh.png
docs/assets/screenshots/admin-first-run-en.png
```

Describe a stable “surface + state” in the filename. Do not include a version, date, person name,
or generated ID. If arrows or numbers are necessary, keep annotations minimal and visually
distinct from product controls, and retain an unannotated original for future updates.

### Add the image and review it

Place the image after the first step that needs it. Alt text should name the interface and state,
not say “screenshot.” Add a note below the image that declares isolated synthetic data:

```markdown
![Coworker first-time setup wizard](../assets/screenshots/admin-first-run-en.png)

<p align="center"><sub>First-time setup wizard · The screenshot uses isolated synthetic configuration and contains no real credentials.</sub></p>
```

The Chinese companion must use its `-zh` file, Chinese alt text, and Chinese note. Before
submission, review the image again from the perspective of someone other than its creator. If any
value might be sensitive, rebuild the scenario and recapture instead of relying only on blur or
redaction.

## Writing and safety

- Begin with the reader outcome, then prerequisites and steps.
- State success feedback, recovery, and destructive consequences.
- Verify commands against current scripts and manifests.
- Never make data deletion, volume deletion, or force-overwrite the first troubleshooting step.
- Use placeholders for credentials, logs, and export bundles.
- Distinguish facts, version-specific behavior, and future proposals.

## Pre-commit checks

```bash
git diff --check
rg -n 'TODO|TBD|<token>|real secret' docs
file docs/assets/screenshots/*
```

Also review manually:

- every relative link and image exists;
- paired pages align in headings, steps, commands, and warnings;
- new pages appear in indexes;
- README anchors work in GitHub rendering;
- generated `src/coworker/web/` did not change accidentally;
- screenshot encoding matches the extension, and bilingual images use matching viewports and
  states;
- screenshots contain no sensitive data, and synthetic content cannot be mistaken for a real
  account or secret.

Documentation-only changes rarely need the full code suite. When a page claims API, CLI,
configuration, or platform behavior, run the smallest relevant check or verify it directly
against implementation and tests, and disclose unrun checks in the PR.

[← Back to project home](../../README.en.md)
