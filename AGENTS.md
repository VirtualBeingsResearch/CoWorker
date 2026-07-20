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

## Implementation, validation, and automatic delivery

- Completing a requested feature includes implementing it, running the relevant checks from `CONTRIBUTING.md`, committing the scoped changes, pushing the feature branch to `origin`, and creating the upstream pull request.
- These commit, push, and pull-request steps are authorized by default for completed feature work. Perform them automatically without waiting for a separate confirmation unless the user explicitly asks not to, authentication is unavailable, validation has materially failed, the remote/branch target is ambiguous, or the operation risks overwriting or publishing unrelated work.
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

## Merge eligible pull requests

- After creating or updating a pull request, inspect both the authenticated account's repository permissions and the pull request's merge readiness. Useful checks include:

  ```bash
  gh api repos/VirtualBeingsResearch/CoWorker --jq '.permissions'
  gh pr view <number> --repo VirtualBeingsResearch/CoWorker \
    --json isDraft,mergeable,reviewDecision,statusCheckRollup
  gh pr checks <number> --repo VirtualBeingsResearch/CoWorker --watch
  ```

- If the authenticated account has merge permission, prefer completing the workflow by merging the pull request directly without waiting for a separate confirmation. This default authorization applies only to pull requests created for the requested, reviewed, and validated work; respect an explicit user request to leave a pull request open.
- Merge only when the pull request is ready: it is not a draft, GitHub reports it as mergeable, required reviews are satisfied, there are no unresolved blocking review conversations, and all required checks have passed. If the repository has no checks configured, verify that this is intentional before merging.
- If checks are pending, wait for them. If the repository supports auto-merge, it may be enabled with the repository's preferred merge method; otherwise monitor the checks and merge after they pass.
- Respect the repository's configured merge strategy. When multiple methods are allowed and no project-specific convention exists, prefer squash merge for a single logical change:

  ```bash
  gh pr merge <number> --repo VirtualBeingsResearch/CoWorker --squash
  ```

- Never use `--admin` or another policy-bypass mechanism automatically. Do not merge a conflicted PR, a PR with failing checks, a PR blocked by required review, or a PR containing changes outside the requested scope.
- If merge permission is unavailable or a repository rule blocks the merge, leave the pull request open and report the exact remaining requirement.

Do not merge the feature branch into the local or fork `main` before opening the pull request. The pull request branch is the integration boundary. After the pull request is merged upstream, synchronize `main` from `upstream/main`, push the synchronized `main` to `origin`, and only then remove the feature worktree and delete the feature branch after verifying that it contains no uncommitted work.
