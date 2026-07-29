# Coworker Documentation

[中文](README.md) · English

[← Back to project home](../README.en.md)

This documentation covers operation, configuration, communication surfaces, product clients, internal architecture, and development. Pages are grouped by functional domain; each directory has its own index so future additions do not turn the `docs/` root into an unstructured list.

## Start here

| What you want to do | Start with |
|---|---|
| Run Coworker for the first time | [First Run](getting-started/README.en.md) |
| Use the Web management console | [Web Management Console](guides/README.en.md) |
| Configure models and providers | [Configuration and models](operations/configuration.en.md) |
| Integrate through HTTP, WebSocket, or files | [API and communication channels](channels/api-and-channels.en.md) |
| Connect local users, Codex, and Claude Code | [Coworker Desktop](channels/desktop.en.md) |
| Diagnose startup, model, or connection failures | [Troubleshooting](operations/troubleshooting.en.md) |
| Understand local storage and outbound data | [Data and trust boundaries](architecture/data-boundaries.en.md) |
| Learn how identity, memory, tools, and the runtime fit together | [Core concepts and capabilities](architecture/concepts.en.md) |

## Functional domains

### [First Run](getting-started/README.en.md)

Choose a run method, complete management setup, verify the instance, and select Desktop, API, or a communication surface.

### [Web Management Console](guides/README.en.md)

Use Life Overview, memory, runtime, models, identity, capability content, remote access, and diagnostics.

### [Architecture and core concepts](architecture/README.en.md)

Runtime model, memory mechanisms, storage locations, and trust boundaries.

- [Core concepts and capabilities](architecture/concepts.en.md)
- [Data and trust boundaries](architecture/data-boundaries.en.md)

### [Channels and clients](channels/README.en.md)

External surfaces including REST, SSE, WebSocket, files, WeCom, and Coworker Desktop.

- [API and communication channels](channels/api-and-channels.en.md)
- [Coworker Desktop](channels/desktop.en.md)
- [Weixin Claw](channels/weixin-claw.en.md)

### [Configuration and operations](operations/README.en.md)

Runtime configuration, model providers, multi-instance setup, and operational guidance.

- [Configuration and models](operations/configuration.en.md)
- [Troubleshooting](operations/troubleshooting.en.md)
- [Self-hosted Relay](operations/relay.en.md)
- [Relay v1 protocol](operations/relay-protocol.en.md)

### [Development and collaboration](development/README.en.md)

Local development, validation, contribution, and security workflows.

- [Development guide](development/development.en.md)
- [Desktop development and release](development/desktop.en.md)
- [Contributing guide](../CONTRIBUTING.en.md)
- [Security policy](../SECURITY.en.md)
- [Changelog](../CHANGELOG.md)

## Directory conventions

- Chinese pages use `<name>.md`; English pages use `<name>.en.md`.
- Domain indexes use `README.md` and `README.en.md`.
- User-facing guidance belongs to its functional domain. Cross-component proposals belong to the most relevant domain and identify themselves as a design or proposal in the title.
- Shared static assets remain under [`assets/`](assets/).
