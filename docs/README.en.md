# Coworker Documentation

[中文](README.md) · English

[← Back to project home](../README.en.md)

This documentation covers operation, configuration, communication surfaces, product clients, internal architecture, and development. Pages are grouped by functional domain; each directory has its own index so future additions do not turn the `docs/` root into an unstructured list.

## Start here

| What you want to do | Start with |
|---|---|
| Run Coworker for the first time | [Project home: Bring her online](../README.en.md#bring-her-online) |
| Configure models and providers | [Configuration and models](operations/configuration.en.md) |
| Integrate through HTTP, WebSocket, or files | [API and communication channels](channels/api-and-channels.en.md) |
| Connect local users, Codex, and Claude Code | [Coworker Desktop](channels/desktop.en.md) |
| Understand local storage and outbound data | [Data and trust boundaries](architecture/data-boundaries.en.md) |
| Learn how identity, memory, tools, and the runtime fit together | [Core concepts and capabilities](architecture/concepts.en.md) |
| Review the WeCom ordering and concurrency proposal | [WeCom message ordering, reliability, and concurrency design](channels/wecom-message-ordering-and-concurrency.en.md) |

## Functional domains

### [Architecture and core concepts](architecture/README.en.md)

Runtime model, memory mechanisms, storage locations, and trust boundaries.

- [Core concepts and capabilities](architecture/concepts.en.md)
- [Data and trust boundaries](architecture/data-boundaries.en.md)

### [Channels and clients](channels/README.en.md)

External surfaces including REST, SSE, WebSocket, files, WeCom, and Coworker Desktop.

- [API and communication channels](channels/api-and-channels.en.md)
- [Coworker Desktop](channels/desktop.en.md)
- [WeCom message ordering, reliability, and concurrency design](channels/wecom-message-ordering-and-concurrency.en.md)

### [Configuration and operations](operations/README.en.md)

Runtime configuration, model providers, multi-instance setup, and operational guidance.

- [Configuration and models](operations/configuration.en.md)

### [Development and collaboration](development/README.en.md)

Local development, validation, contribution, and security workflows.

- [Development guide](development/development.en.md)
- [Contributing guide](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
- [Changelog](../CHANGELOG.md)

## Directory conventions

- Chinese pages use `<name>.md`; English pages use `<name>.en.md`.
- Domain indexes use `README.md` and `README.en.md`.
- User-facing guidance belongs to its functional domain. Cross-component proposals belong to the most relevant domain and identify themselves as a design or proposal in the title.
- Shared static assets remain under [`assets/`](assets/).
