# Coworker 文档

中文 · [English](README.en.md)

[← 返回项目首页](../README.md)

这里汇集运行、配置、通信入口、产品界面、内部架构和开发文档。文档按功能域组织；每个目录都有自己的索引，便于后续继续扩展而不让 `docs/` 根目录失序。

## 从这里开始

| 你想完成的事 | 从这里进入 |
|---|---|
| 第一次启动 Coworker | [首次运行](getting-started/README.md) |
| 确认系统、架构和组件是否兼容 | [平台支持与组件兼容](getting-started/platform-support.md) |
| 使用 Web 管理后台 | [Web 管理后台](guides/README.md) |
| 从典型场景开始组合能力 | [典型使用场景](guides/use-cases.md) |
| 创建 Skill、Palace 或潜意识模式 | [能力内容创作](guides/capability-authoring.md) |
| 配置模型与 Provider | [配置与模型](operations/configuration.md) |
| 通过 HTTP、WebSocket 或文件接入 | [API 与通信入口](channels/api-and-channels.md) |
| 查询接口字段、认证和错误处理 | [API 参考](channels/api-reference.md) |
| 连接本机用户、Codex 与 Claude Code | [Coworker Desktop](channels/desktop.md) |
| 连接一个或多个 Telegram Bot | [Telegram](channels/telegram.md) |
| 长期部署、升级或恢复实例 | [部署](operations/deployment.md) · [升级](operations/upgrading.md) · [备份](operations/backup-and-restore.md) |
| 排查启动、模型或连接问题 | [故障排查](operations/troubleshooting.md) |
| 了解数据保存在哪里、什么可能外发 | [数据与信任边界](architecture/data-boundaries.md) |
| 理解项目为何以虚拟生命为目标 | [虚拟生命理念与生命架构](architecture/lifeform-philosophy.md) |
| 理解身份、记忆、工具与生命循环 | [核心概念与能力](architecture/concepts.md) |
| 查找 Bubble、Palace、Participant 等术语 | [术语表](glossary.md) |

## 功能域

### [首次运行](getting-started/README.md)

从选择运行方式、完成管理端初始化，到验证实例并选择 Desktop、API 或通信入口。

- [平台支持与组件兼容](getting-started/platform-support.md)

### [Web 管理后台](guides/README.md)

使用生命总览、记忆、运行、模型、身份、能力内容、远程访问与诊断功能。

- [典型使用场景](guides/use-cases.md)
- [能力内容创作](guides/capability-authoring.md)

### [架构与核心概念](architecture/README.md)

产品的运行模型、记忆机制、数据保存位置和信任边界。

- [虚拟生命理念与生命架构](architecture/lifeform-philosophy.md)
- [核心概念与能力](architecture/concepts.md)
- [运行时架构与消息流](architecture/runtime-flow.md)
- [数据与信任边界](architecture/data-boundaries.md)

### [通信与客户端](channels/README.md)

REST、SSE、WebSocket、文件、企业微信、Telegram 和 Coworker Desktop 等外部入口。

- [API 与通信入口](channels/api-and-channels.md)
- [API 参考](channels/api-reference.md)
- [Coworker Desktop](channels/desktop.md)
- [Telegram](channels/telegram.md)
- [微信 Claw](channels/weixin-claw.md)

### [配置与运维](operations/README.md)

运行配置、模型 Provider、多实例配置和生产运行注意事项。

- [配置与模型](operations/configuration.md)
- [Provider 配置指南](operations/providers.md)
- [长期运行与部署](operations/deployment.md)
- [升级与迁移](operations/upgrading.md)
- [备份与恢复](operations/backup-and-restore.md)
- [可观测性与日常运维](operations/observability.md)
- [故障排查](operations/troubleshooting.md)
- [自托管中继（Relay）](operations/relay.md)
- [Relay v1 协议](operations/relay-protocol.md)

### [开发与协作](development/README.md)

本地开发、验证、贡献和安全协作流程。

- [开发指南](development/development.md)
- [Explore Lab 使用与开发](development/explore-lab.md)
- [Desktop 开发与发布](development/desktop.md)
- [文档维护规范](development/documentation.md)
- [贡献指南](../CONTRIBUTING.md)
- [安全策略](../SECURITY.md)
- [变更记录](../CHANGELOG.md)
- [术语表](glossary.md)

## 目录约定

- 中文页面使用 `<name>.md`，英文页面使用 `<name>.en.md`。
- 功能域入口固定使用 `README.md` / `README.en.md`。
- 面向使用者的说明放在对应功能域；跨组件方案和演进设计放在最相关的功能域，并在标题中明确“设计”或“提案”。
- 图片等共享静态资源继续放在 [`assets/`](assets/) 下。
