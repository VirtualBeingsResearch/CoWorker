# Standard development workflow

## Required project guidance

Before planning or modifying code or documentation:

1. Read `CONTRIBUTING.md` completely. It is the canonical source for contribution scope, validation, localization, commit, and pull-request requirements.
2. Read `docs/development/README.md` completely and follow every link relevant to the task. For implementation, environment setup, or test selection, also read `docs/development/development.md` completely.
3. For security-sensitive changes or vulnerability-reporting work, read `SECURITY.md` completely before proceeding.
4. When changing localized documentation, open both the Chinese and English companion files before editing and update them together.

Do not rely only on summaries in this file; the applicable referenced documents are part of the instructions for the task.

## Repository roles

- The canonical upstream repository is `git@github.com:VirtualBeingsResearch/CoWorker.git`.
- The local `origin` remote is the current developer's personal fork. Do not assume a fixed fork owner.
- The local repository must also have an `upstream` remote pointing to the canonical repository. If it is missing, add it with:

  ```bash
  git remote add upstream git@github.com:VirtualBeingsResearch/CoWorker.git
  ```

- Before changing remotes, pushing, or opening a pull request, verify the resolved repository and branch targets. Never force-push `main` or discard divergent work to synchronize it unless the user explicitly approves that destructive action.

## Synchronize before starting work

Before starting every new feature or substantial fix from `main`, update the local `main` branch from upstream and mirror the result to the fork:

```bash
git switch main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
```

- Start new work only from this synchronized `main`.
- Keep `main` free of feature commits. If the fast-forward merge fails, stop and inspect the divergence instead of creating a synchronization merge commit or forcing the branch.
- Do not overwrite, stash, commit, or otherwise absorb unrelated local changes.
- When the current branch already belongs to the assigned work, keep that branch and do not switch it to `main` merely to begin or continue the task.

The fork may alternatively be synchronized with `gh repo sync <fork-owner>/CoWorker --source VirtualBeingsResearch/CoWorker --branch main`, followed by a fast-forward-only local pull. Do not use `--force` automatically.

## Branches

- Use a focused branch name such as `feat/<slug>`, `fix/<slug>`, or `chore/<slug>`.
- Keep one logical change per branch and pull request. Do not mix unrelated cleanup or user-owned changes into the feature commit.

## Localization and i18n

- Treat user-visible Python runtime text as localized content. Do not add new
  hard-coded natural-language strings to tools, API responses, channel output,
  runtime notices, or model-facing prompts when the text can be owned by
  Coworker.
- Put runtime translations in the domain catalog that owns the message under
  `src/coworker/i18n/catalogs/<locale>/`. Update both `en` and `zh-CN` catalogs
  in the same change. Keep semantic keys and the complete set of
  `{{placeholder}}` names identical across locales.
- Use `tr("catalog.key", ...)` at the call site. Keep protocol names,
  participant IDs, enum values, timestamps, file paths, and user or
  third-party text unchanged; translate only Coworker-owned labels and
  surrounding prose.
- For localized documentation under `docs/`, update the paired `.md` and
  `.en.md` pages together. Keep commands, configuration names, API fields,
  and product terms consistent between languages.
- Root project guides follow the same convention: `CONTRIBUTING.md` and
  `SECURITY.md` are Chinese-first, with `.en.md` English companions. Update
  each pair together.
- Add or update tests for both locale outputs when behavior is user-visible.
  At minimum run `uv run --frozen pytest tests/unit/test_i18n.py` and the
  affected feature tests; `test_i18n.py` enforces catalog key and placeholder
  parity.

## Implementation, validation, and completion reporting

- Completing a requested feature includes implementing it, running the relevant checks from `CONTRIBUTING.md`, committing the scoped changes, and reporting the completed work and validation results to the user.
- Committing scoped changes is authorized by default for completed feature work unless the user explicitly asks not to, validation has materially failed, the current branch is ambiguous, or the commit would include unrelated work.
- Do not push the feature branch to `origin` or create a pull request unless the user explicitly requests that operation in the current conversation. Local completion and a user-facing report are the default delivery boundary.
- Use clear, conventional commit messages. Prefer small coherent commits when they improve reviewability, but do not split a tightly coupled change mechanically.
- Before reporting completion, review the final diff and confirm that required tests, documentation, examples, and paired localized docs have been handled according to `CONTRIBUTING.md`.
- Do not edit `CHANGELOG.md` in feature, fix, documentation, dependency, or routine maintenance pull requests. Keep the hand-written `Unreleased` section, but update it only in dedicated release-preparation or changelog-maintenance work so concurrent pull requests do not contend on the same file.
- Use clear conventional commit and pull-request titles so the release maintainer can assemble the hand-written `Unreleased` notes accurately.
- If some relevant check cannot be run, do not conceal it; report the exact unrun or failing check to the user and, if a pull request is later requested, document it there as well.

## Optional push and pull-request workflow

Use this workflow only when the user explicitly requests pushing the feature branch or creating a pull request in the current conversation.

Push the feature branch to the developer's fork, not to upstream:

```bash
git push -u origin <feature-branch>
```

Then create a pull request from `<fork-owner>:<feature-branch>` to `VirtualBeingsResearch/CoWorker:main`:

```bash
gh pr create \
  --repo VirtualBeingsResearch/CoWorker \
  --base main \
  --head <fork-owner>:<feature-branch> \
  --title "<conventional PR title>" \
  --body-file <pr-body-file>
```

- Resolve `<fork-owner>` from the actual `origin` remote; never hard-code a personal account in shared instructions.
- Use a conventional, concise PR title such as `feat(scope): description` or `fix(scope): description`.
- The PR body must summarize the outcome and implementation, list validation performed, identify risks or compatibility/security implications, disclose checks not run, and link related issues when applicable.
- Keep the pull request reviewable and limited to one logical change. Create it as ready for review when the feature is complete; use a draft only when work is intentionally incomplete or externally blocked.
- After creation, return the PR URL and inspect CI with `gh pr checks --repo VirtualBeingsResearch/CoWorker --watch` when practical.

## Manual merge only

- If the user explicitly requests creating or updating a pull request, inspect its checks and review readiness when practical. Useful commands include:

  ```bash
  gh pr view <number> --repo VirtualBeingsResearch/CoWorker \
    --json isDraft,mergeable,reviewDecision,statusCheckRollup
  gh pr checks <number> --repo VirtualBeingsResearch/CoWorker --watch
  ```

- Always leave a created pull request open for human review, even when every check passes and the authenticated account has merge permission.
- Do not call `gh pr merge`, enable auto-merge, enqueue the pull request in a merge queue, invoke an equivalent GraphQL/API merge operation, or use an administrative policy bypass unless the user explicitly requests merging that specific pull request.
- A general request to implement, complete, ship, or deliver work authorizes a scoped local commit, but does not authorize pushing, pull-request creation, or merging. Perform each remote operation only when the user explicitly requests it in the current conversation. Merge only when the user explicitly asks to merge that specific pull request.
- Report the PR URL, validation status, and any remaining review or CI requirements. If checks are pending, they may be monitored, but passing checks do not change the manual-merge requirement.

Do not merge the feature branch into the local or fork `main` before opening the pull request. The pull request branch is the integration boundary. After the pull request is merged upstream, synchronize `main` from `upstream/main`, push the synchronized `main` to `origin`, and only then delete the feature branch after verifying that it contains no uncommitted work.
