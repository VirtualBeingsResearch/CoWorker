# Explore Lab Usage and Development

[中文](explore-lab.md) · English

[← Back to Development and Collaboration](README.en.md)

Explore Lab imports a sensitive snapshot from a running Coworker and creates isolated branches
that can pause, step, fork, and replay. It compares prompts, configuration, and behavior; it is not
a production message surface.

## Start

```bash
npm ci --prefix apps/explore-lab/frontend
npm --prefix apps/explore-lab/frontend run build
uv run --project apps/explore-lab/backend python -m explore_lab
```

Open <http://127.0.0.1:8100/>. The backend serves
`apps/explore-lab/frontend/dist` by default; `--ui-dir` selects another build directory.

## Import an experiment

Import uses the administrator token to request `/api/export_config` from a Coworker instance. The
bundle contains effective configuration, `data/`, `.coworker/`, and `providers.json`, potentially
including every key, message, and attachment. Use it only locally or on an isolated trusted
network, and never commit experiment workdirs.

Import creates a baseline root branch. Branch runtimes use simulated participants:
`communicate` records outbound messages but never delivers them to a real Channel.

## Experimental workflow

1. Send input to baseline and use step/step N to control cycles.
2. Fork a stable state and set labels, notes, and configuration or prompt overrides.
3. Run branches and inspect transcript, Bubbles, subconscious work, and state.
4. Record a verdict on each branch.
5. Compare output, cycle count, prompt, `thinking.md`, and configuration differences.

Back-step restores that branch's own snapshot and never modifies the source Coworker.

## Scenarios and replay

A Scenario is a sequence of messages with `participant_id` and optional delay. Replay forks N child
branches from the selected state, sends the same events, and resumes each branch. This helps expose
non-determinism and configuration differences. Record sample size, version, model, and configuration.

## Branch lifecycle

- Lab restart attempts to restore branch metadata and runnable state.
- A sleeping branch can wake when opened.
- A branch with children cannot be deleted.
- Batch operations can step, pause, or resume multiple branches.
- Orchestrator shutdown pauses and terminates its branch runners.

## Security and cleanup

- Use the administrator token only for import; never include it in screenshots or notes.
- Treat exports and branch workdirs as credential files.
- Do not expose Explore Lab publicly; development CORS and branch control are not production authorization.
- Preserve reproduction metadata, versions, diffs, and conclusions before cleanup.
- Convert a confirmed defect into a minimal source test after reproducing it in a branch.

[← Back to project home](../../README.en.md)
