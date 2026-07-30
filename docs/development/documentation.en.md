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

- Use isolated synthetic data only.
- Remove or mask tokens, API keys, users, conversations, paths, and runtime records.
- Capture Chinese and English interfaces separately.
- Store files under `docs/assets/screenshots/` with stable descriptive names.
- Update screenshots in the same PR when UI behavior changes.

Mermaid is appropriate for end-to-end flow, responsibility boundaries, and sequences; do not use
it instead of a three-line explanation.

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
```

Also review manually:

- every relative link and image exists;
- paired pages align in headings, steps, commands, and warnings;
- new pages appear in indexes;
- README anchors work in GitHub rendering;
- generated `src/coworker/web/` did not change accidentally;
- screenshots contain no sensitive data.

Documentation-only changes rarely need the full code suite. When a page claims API, CLI,
configuration, or platform behavior, run the smallest relevant check or verify it directly
against implementation and tests, and disclose unrun checks in the PR.

[← Back to project home](../../README.en.md)
