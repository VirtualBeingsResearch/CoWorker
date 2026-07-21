# Coworker 文档

中文 · [English](README.en.md)

[← 返回项目首页](../README.md)

这里汇集运行、配置、通信入口、产品界面、内部架构和开发文档。文档按功能域组织；每个目录都有自己的索引，便于后续继续扩展而不让 `docs/` 根目录失序。

## 从这里开始

| 你想完成的事 | 从这里进入 |
|---|---|
| 第一次启动 Coworker | [项目首页：让她跑起来](../README.md#让她跑起来) |
| 配置模型与 Provider | [配置与模型](operations/configuration.md) |
| 通过 HTTP、WebSocket 或文件接入 | [API 与通信入口](channels/api-and-channels.md) |
| 连接本机用户、Codex 与 Claude Code | [Coworker Desktop](channels/desktop.md) |
| 了解数据保存在哪里、什么可能外发 | [数据与信任边界](architecture/data-boundaries.md) |
| 理解身份、记忆、工具与生命循环 | [核心概念与能力](architecture/concepts.md) |
| 评审企微消息时序与并发方案 | [企微消息时序、可靠性与并发控制设计](channels/wecom-message-ordering-and-concurrency.md) |

## 功能域

### [架构与核心概念](architecture/README.md)

产品的运行模型、记忆机制、数据保存位置和信任边界。

- [核心概念与能力](architecture/concepts.md)
- [数据与信任边界](architecture/data-boundaries.md)

### [通信与客户端](channels/README.md)

REST、SSE、WebSocket、文件、企业微信和 Coworker Desktop 等外部入口。

- [API 与通信入口](channels/api-and-channels.md)
- [Coworker Desktop](channels/desktop.md)
- [企微消息时序、可靠性与并发控制设计](channels/wecom-message-ordering-and-concurrency.md)

### [配置与运维](operations/README.md)

运行配置、模型 Provider、多实例配置和生产运行注意事项。

- [配置与模型](operations/configuration.md)

### [开发与协作](development/README.md)

本地开发、验证、贡献和安全协作流程。

- [开发指南](development/development.md)
- [贡献指南](../CONTRIBUTING.zh-CN.md)
- [安全策略](../SECURITY.zh-CN.md)
- [变更记录](../CHANGELOG.md)

## 目录约定

- 中文页面使用 `<name>.md`，英文页面使用 `<name>.en.md`。
- 功能域入口固定使用 `README.md` / `README.en.md`。
- 面向使用者的说明放在对应功能域；跨组件方案和演进设计放在最相关的功能域，并在标题中明确“设计”或“提案”。
- 图片等共享静态资源继续放在 [`assets/`](assets/) 下。
