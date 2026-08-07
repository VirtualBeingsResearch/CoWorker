# Backup and Restore

[中文](backup-and-restore.md) · English

[← Back to Configuration and Operations](README.en.md)

Coworker has three different backup mechanisms. First decide whether you need to recover a
short-term conversation, runtime files under `data/`, or a complete instance including
configuration and external components.

| Mechanism | Scope | Best for |
|---|---|---|
| Management emergency backup | One short-term-context snapshot | Summary or full-context recovery after repeated Agent errors |
| `scripts/cleanup.py` | Files below `data/`, excluding `data/_backups/` | Local runtime snapshots, reset, and restore |
| Full disaster-recovery backup | Workspace, `data/`, `.coworker/`, configuration, clients, and Relay data | Upgrades, device migration, disk failure, and full recovery |

Neither an emergency backup nor a cleanup snapshot replaces a full instance backup.

## Full backup inventory

Stop Coworker, then account for the locations used by the deployment:

- the Git workspace, including unpushed branches, commits, and uncommitted work;
- `data/`: identity, memory, tasks, alarms, logs, inbox/outbox, and runtime state;
- `.coworker/`: Skills, Palaces, subconscious modes, and their assets;
- `.env`, `providers.json`, and configuration injected by external services;
- the Desktop operating-system data directory containing settings, credentials, indexes, and logs;
- Docker `coworker-workspace`, `coworker-state`, and `coworker-models` volumes or bind mounts;
- the Relay bbolt database, signing key, `.env`, and deployment files.

Backups may contain model keys, administrator tokens, Relay private keys, conversations, and file
content. Encrypt them and restrict access. Never commit them to Git or attach them to issues.

## Back up and restore `data/`

Inspect the scope:

```bash
uv run python scripts/cleanup.py status
```

Create a timestamped snapshot:

```bash
uv run python scripts/cleanup.py backup
```

Snapshots are written to `data/_backups/<timestamp>/`. They do not include `.env`,
`providers.json`, `.coworker/`, Desktop data, or Docker volumes.

Stop Coworker and select the intended snapshot before restoring:

```bash
uv run python scripts/cleanup.py restore
uv run python scripts/cleanup.py restore --from 20260428_123456
```

Restore copies snapshot files over files with the same names. It does not remove extra current
files that are absent from the snapshot. To reproduce an exact point in time, preserve the current
state and validate the restore in an isolated directory first.

> [!WARNING]
> `delete` and `backup-delete` remove runtime files below `data/`. Do not run them while the
> service is writing, and do not use `--yes` in automation before independently verifying scope.

## Recover short-term context

Emergency backups in Runtime Center are short-term-memory snapshots created after repeated Agent errors:

- **Summary restore** calls the summary model and injects the result as new input without replacing current context.
- **Full restore** replaces the main short-term context and trims incomplete tool-call chains.

Prefer summary restore. Before full restore, record the current message count, backup filename, and
time. Neither mode restores long-term memory, Skills, or configuration.

## Docker and Relay

Stop Coworker with `docker compose stop`, then resolve its actual mounts with
`docker compose config`, `docker volume ls`, and `docker volume inspect <name>`. A complete backup
must cover both the host checkout used as the workspace (or the legacy `coworker-workspace`
volume) and the state volume. The model cache is rebuildable, although retaining it shortens
recovery. Do not back up only the container writable layer.

Create a consistent Relay database snapshot with its own command:

```bash
coworker-relay health
coworker-relay backup --output relay-backup.db
```

The database snapshot does not include Relay `.env`, signing keys, or reverse-proxy configuration.
Protect those separately. See [Self-hosted Relay](relay.en.md) for Relay recovery.

## Recovery drill

At least before major upgrades:

1. restore in an isolated directory or temporary host, not over the only working copy;
2. use the same or an explicitly compatible Coworker version;
3. verify `/status`, identity, memory, tasks, and one test message;
4. avoid reusing a live instance identity when testing Desktop or Relay;
5. record recovery time, missing items, and improvements.

[← Back to project home](../../README.en.md)
