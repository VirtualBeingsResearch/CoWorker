# Environment source templates

Templates for creating new environment sensing sources. Copy a template
directory to `.coworker/environment/<your-name>/` and customize.

## Available templates

- **python-inline/** — Inline Python source (recommended). Runs in-process,
  gets `ctx` injected directly. Simplest and most powerful.
- **shell/** — Shell subprocess source. Demonstrates the JSON-RPC protocol
  for non-Python languages. Any language that reads stdin / writes stdout
  can use this mode.

## Quick start

```bash
# 1. Copy the template
cp -r templates/environment/python-inline .coworker/environment/my-source

# 2. Edit SOURCE.md (name, schedule, params) and source.py (poll logic)

# 3. Reload to activate
# (The agent does this via manage_environment action=reload, or the
# runtime auto-discovers on the next tick.)
```

See [docs/architecture/environment-sensing.md](../../docs/architecture/environment-sensing.md)
for the full guide.
