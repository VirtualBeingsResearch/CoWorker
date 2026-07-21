# Standard development workflow

## Repository roles

- The canonical upstream repository is `git@github.com:VirtualBeingsResearch/CoWorker.git`.
- The local `origin` remote is the current developer's personal fork. Do not assume a fixed fork owner.
- The local repository must also have an `upstream` remote pointing to the canonical repository. If it is missing, add it with:

  ```bash
  git remote add upstream git@github.com:VirtualBeingsResearch/CoWorker.git
  ```

- Before changing remotes, pushing, or opening a pull request, verify the resolved repository and branch targets. Never force-push `main` or discard divergent work to synchronize it unless the user explicitly approves that destructive action.

## Synchronize before starting work

Before starting every new feature or substantial fix, update the primary `main` worktree from upstream and mirror the result to the fork:

```bash
git switch main
git fetch upstream
git merge --ff-only upstream/main
git push origin main
```

- Start new work only from this synchronized `main`.
- Keep `main` free of feature commits. If the fast-forward merge fails, stop and inspect the divergence instead of creating a synchronization merge commit or forcing the branch.
- Do not overwrite, stash, commit, or otherwise absorb unrelated changes already present in the primary worktree.

The fork may alternatively be synchronized with `gh repo sync <fork-owner>/CoWorker --source VirtualBeingsResearch/CoWorker --branch main`, followed by a fast-forward-only local pull. Do not use `--force` automatically.

## Branches and worktrees

- Every large or non-trivial feature must be developed on a dedicated branch in an independent Git worktree. A feature is non-trivial when it spans multiple components, is expected to take several commits, or benefits from isolation from other ongoing work.
- Use a focused branch name such as `feat/<slug>`, `fix/<slug>`, or `chore/<slug>`.
- A typical worktree creation command is:

  ```bash
  git worktree add ../CoWorker-<slug> -b feat/<slug> main
  ```

- Keep one logical change per branch and pull request. Do not mix unrelated cleanup or user-owned changes into the feature commit.

## Implementation, validation, and automatic pull-request delivery

- Completing a requested feature includes implementing it, running the relevant checks from `CONTRIBUTING.md`, committing the scoped changes, pushing the feature branch to `origin`, and creating the upstream pull request.
- These commit, push, and pull-request steps are authorized by default for completed feature work. Perform them automatically without waiting for a separate confirmation unless the user explicitly asks not to, authentication is unavailable, validation has materially failed, the remote/branch target is ambiguous, or the operation risks overwriting or publishing unrelated work.
- Pull-request creation is the automatic delivery boundary. Creating a PR never grants permission to merge it, enable auto-merge, or enqueue it in a merge queue.
- Use clear, conventional commit messages. Prefer small coherent commits when they improve reviewability, but do not split a tightly coupled change mechanically.
- Before delivery, review the final diff and confirm that required tests, documentation, examples, paired localized docs, and `CHANGELOG.md` updates have been handled according to `CONTRIBUTING.md`.
- If some relevant check cannot be run, do not conceal it; document the exact unrun or failing check in the pull request.

## Push and pull-request workflow

Push the worktree's feature branch to the developer's fork, not to upstream:

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

- After creating or updating a pull request, inspect its checks and review readiness when practical. Useful commands include:

  ```bash
  gh pr view <number> --repo VirtualBeingsResearch/CoWorker \
    --json isDraft,mergeable,reviewDecision,statusCheckRollup
  gh pr checks <number> --repo VirtualBeingsResearch/CoWorker --watch
  ```

- Always leave the pull request open for human review after automatic delivery, even when every check passes and the authenticated account has merge permission.
- Do not call `gh pr merge`, enable auto-merge, enqueue the pull request in a merge queue, invoke an equivalent GraphQL/API merge operation, or use an administrative policy bypass as part of the automatic workflow.
- A general request to implement, complete, ship, or deliver work authorizes commit, push, and pull-request creation, but does not authorize merging. Merge only when the user explicitly asks to merge that specific pull request in the current conversation.
- Report the PR URL, validation status, and any remaining review or CI requirements. If checks are pending, they may be monitored, but passing checks do not change the manual-merge requirement.

Do not merge the feature branch into the local or fork `main` before opening the pull request. The pull request branch is the integration boundary. After the pull request is merged upstream, synchronize `main` from `upstream/main`, push the synchronized `main` to `origin`, and only then remove the feature worktree and delete the feature branch after verifying that it contains no uncommitted work.
