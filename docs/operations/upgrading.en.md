# Upgrading and Migration

[中文](upgrading.md) · English

[← Back to Configuration and Operations](README.en.md)

This page is for instances that already contain identity, memory, tasks, or client connections.
Do not treat an upgrade as a code-only operation. Record the current version, create a backup,
and check for data-format or protocol changes first.

## Before upgrading

1. Read the [CHANGELOG](../../CHANGELOG.md), especially migration, compatibility, and known-limit sections.
2. Confirm that no task is failing continuously and that no memory backfill or critical tool call is running.
3. Record the Coworker, Desktop, and Relay versions and how each component is deployed.
4. Back up `data/`, `.coworker/`, configuration, and external components using
   [Backup and Restore](backup-and-restore.en.md).
5. Keep the currently working commit or image tag. Do not rely on a floating tag as the only rollback point.

> [!WARNING]
> Do not force-sync, reset, or overwrite a working tree with unreviewed changes. Coworker may also
> have created branches or commits in the checkout.

## Connect the workspace to your repository

Coworker always works in a complete Git repository, whether the service runs from source, through
Compose, or directly from Docker. A direct Docker run persists `/app` and its remote configuration
in the workspace volume, so you do not need to enter the container and configure it first. To
manage subsequent changes in your own repository, use it as `origin` and keep the official
Coworker repository or another source as `upstream`. If you track only one repository, `origin`
alone is sufficient.

You can give Coworker the repository URL and target branch directly:

> Inspect the current working tree, branch, and remotes. Configure `<my-repository-url>` as
> `origin`; if the existing `origin` points to the official Coworker repository, preserve it as
> `upstream`. Fetch both remotes, preserve every local commit and modification, safely integrate
> `upstream/main` into the current branch, run the relevant checks, then push the current branch to
> `origin`. Ask me before any force-push, discard, overwrite, or conflict whose resolution is not
> clear.

For a manual workflow, inspect the workspace and existing remotes before adapting them to the
actual repositories:

```bash
git status --short
git branch --show-current
git remote -v
git remote add upstream <upstream-repository-url>
git fetch upstream
git merge upstream/main
```

Replace `main` when the upstream uses another default branch. Do not run `git remote add` when a
remote with that name already exists. Repository URLs must not contain tokens, passwords, or
private keys. Public repositories need no extra credentials for fetching. Configure dedicated,
least-privilege Git credentials in the container or runtime account before accessing a private
repository or pushing, and never send credentials through chat.

For recurring synchronization, name the frequency, local branch, upstream branch, and whether to
push. This prevents a scheduled task from using whichever branch happens to be checked out later.

## Let Coworker upgrade herself

For a source checkout, the recommended path is to ask Coworker to inspect and perform the upgrade.
She can use file, code, and command tools to inspect the working tree and remotes, review upstream
changes, resolve conflicts whose intent is clear, run relevant checks, and call `restart_self`
separately after the code update is ready.

For example:

> Inspect the current repository and runtime, back up data affected by the upgrade, then safely
> integrate upstream changes into the current branch. Preserve local commits and modifications,
> review migrations and dependency changes, and run the relevant checks. Ask me before discarding
> or overwriting work or resolving an ambiguous conflict. When everything passes, call
> `restart_self` by itself, then report the restored version and validation results.

`restart_self` protects the transition as follows:

1. runs `python -m coworker --check` in the current Python environment, with a 30-second timeout;
2. returns an error and leaves the current process running when validation fails or times out;
3. saves short-term memory with the pending tool call after validation succeeds;
4. requests the main loop to exit, then lets the launcher replace the process in place; on Windows,
   the parent supervisor starts a new worker;
5. restores short-term memory and alarms, replacing the pending call with a real restart result.

The main line must call the tool by itself; a Bubble cannot trigger it. The check proves only that
the new code can load configuration and register Providers. It does not prove every integration
test, data migration, or external service works, so the upgrade task must still back up data,
review the diff, and run relevant tests first.

If Coworker is wrapped by a process manager that does not support process replacement, or the
upgrade changes the container image, system dependencies, launch command, or Python environment,
the host or orchestrator must perform that outer upgrade. `restart_self` can restart only the
current runtime environment.

## Manually upgrade a source checkout

Stop Coworker, then inspect the current branch and remotes:

```bash
git status --short
git branch --show-current
git remote -v
```

Fetch and review the intended upstream through your normal Git workflow. When the lock file changes:

```bash
uv sync --frozen
uv run playwright install chromium
uv run coworker --check
```

`--check` validates the startup environment without entering the persistent Agent loop. After it
passes, start with `uv run coworker`, then inspect `/status` and management diagnostics.

## Upgrade a direct Docker run

A direct `docker run` creates mounts for `/app`, `/var/lib/coworker`, and `/opt/huggingface`.
Before upgrading, follow [Backup and Restore](backup-and-restore.en.md#run-the-docker-image-directly)
to record their names and back up the workspace and state. Then preserve the old container and let
the replacement reuse its mounts:

```bash
docker stop coworker
docker rename coworker coworker-before-upgrade
docker pull ghcr.io/virtualbeingsresearch/coworker:offline
docker run --name coworker \
  --volumes-from coworker-before-upgrade \
  -p 127.0.0.1:8000:8000 \
  -e API__HOST=0.0.0.0 \
  ghcr.io/virtualbeingsresearch/coworker:offline
```

When the managed workspace is on the image's default branch, clean, and eligible for a
fast-forward, the new image advances it from the embedded Git bundle. Local modifications,
commits, other branches, and divergent history remain in place. Do not remove
`coworker-before-upgrade` or the backup until the replacement is verified. Both containers share
the same state volume, however, so confirm data-format compatibility before returning to the old
image and restore the pre-upgrade backup when required.

### Migrate from direct Docker to Compose

First use the backup procedure to create `workspace.tgz` and `state.tgz`. Extract the workspace to
a new host directory, retain the old container, then create the Compose container and restore its
state volume:

```bash
mkdir coworker-compose
tar -xzf /absolute/path/to/coworker-backup/workspace.tgz -C coworker-compose
docker stop coworker
docker rename coworker coworker-direct-backup
cd coworker-compose
docker compose pull
docker compose create --no-build
docker run --rm \
  --volumes-from coworker \
  --mount type=bind,src=/absolute/path/to/coworker-backup,dst=/backup,readonly \
  --entrypoint sh \
  ghcr.io/virtualbeingsresearch/coworker:offline \
  -ec 'tar -C /var/lib/coworker -xzf /backup/state.tgz'
docker compose up --no-build -d
docker compose ps
```

`workspace.tgz` contains Git history, `.coworker/`, and workspace configuration. `state.tgz`
restores into Compose's separate state volume. Keep `coworker-direct-backup` and the encrypted
backup until identity, memory, tasks, and messages are verified. If a custom model cache cannot be
recreated from the image, also back up `/opt/huggingface` separately.

## Upgrade Docker Compose

Compose uses the current checkout as its workspace and the published image as its execution
environment by default. After stopping writes and creating a backup, pin `COWORKER_IMAGE` to the
version tag or digest you intend to validate, then run:

```bash
docker compose stop
docker compose pull
docker compose up --no-build -d
docker compose ps
```

If the checkout contains `pyproject.toml`, `uv.lock`, or image-level system dependency changes not
included in the selected published image, run `docker compose build` and `docker compose up -d`
instead of reusing the old execution environment.
`coworker-state` and `coworker-models` are separate volumes. Replacing a container does not
migrate, back up, or delete them.

### Migrate an existing checkout data directory

The Compose entrypoint replaces `/app/data` with a state-volume link only when that path is absent
or empty. If you previously ran `uv run coworker` in the same checkout, `data/` may be non-empty.
Startup then refuses to overwrite it instead of silently losing data.

Stop the source process, move the original directory to a protected location outside the
checkout, then restore its contents into the state volume created by Compose:

```bash
mv data ../coworker-data-before-compose
docker compose pull
docker compose create --no-build
docker run --rm \
  --volumes-from coworker \
  --mount type=bind,src="$PWD/../coworker-data-before-compose",dst=/backup,readonly \
  --entrypoint sh \
  ghcr.io/virtualbeingsresearch/coworker:offline \
  -ec 'cp -a /backup/. /var/lib/coworker/'
docker compose up --no-build -d
```

The migrated directory contains administrator tokens, model keys, conversations, and attachments.
Keep it in a location readable only by the runtime account. Do not remove
`../coworker-data-before-compose` until the new container is fully verified.

Older Compose versions used the `coworker-workspace` named volume by default. On the first upgrade
to a version that defaults to the current checkout, that volume is not deleted, but the new bind
mount hides it. Before startup, follow [Backup and Restore](backup-and-restore.en.md) to resolve the
actual volume name and back up its branches, commits, and modifications. To keep using the old
workspace temporarily, set
`COWORKER_WORKSPACE_SOURCE=coworker-workspace` in `.env`. Remove the override and switch to the
current checkout only after its contents have been migrated safely.

## Data and memory migration

- Do not assume an older release can read data written by a newer release.
- After a memory-tree upgrade, new compression events extend the tree by default. To backfill
  historical logs, use the management console or `POST /backfill_tree`. Backfill makes model calls.
- Changing the embedding model changes the vector space of long-term memory. Do not switch an
  existing store unless release notes provide an explicit rebuild path.
- Identity, Skills, Palaces, subconscious modes, tasks, and history are not translated when the
  runtime locale changes.

See [Core Concepts and Capabilities](../architecture/concepts.en.md) for memory internals and
notes about specific legacy formats.

## Component compatibility

- Go Relay, Python Coworker, and Rust Desktop use Relay protocol version `1`. Upgrade them together
  when protocol or key derivation changes; see [Relay v1 Protocol](relay-protocol.en.md).
- Desktop negotiates a protocol version with Coworker. An old client may continue to work locally
  while being unable to connect to an incompatible Coworker or Relay.
- A failed Desktop update does not remove the installed version. Missing signatures or platform
  assets must leave the old version in place.

## Validate after upgrading

- `/status` reports the expected runtime, Provider, and model;
- management diagnostics has no continuously growing failures;
- a test message completes through the intended reply path;
- long-term memory, tasks, alarms, and recent interactions remain readable;
- local Desktop and remote Relay connections work when applicable;
- the upgrade time, target version, backup, and results are recorded.

## Rollback

Code rollback and data restore are separate operations. Switch only the code or image when the old
release is known to support the current data. If the new release wrote an incompatible format,
stop the service, preserve the failed state, and restore the complete pre-upgrade backup. Do not
experiment by deleting `data/` or Docker volumes.

[← Back to project home](../../README.en.md)
