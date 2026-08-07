# 配置与运维

中文 · [English](README.en.md)

[← 返回文档索引](../README.md)

本目录收纳部署和运行 Coworker 时需要维护的配置与运维说明。

- [配置与模型](configuration.md)：环境变量、模型选择、多实例配置和运行时模型切换。
- [Provider 配置指南](providers.md)：Provider 方言、模型分工、多实例和常见调用错误。
- [长期运行与部署](deployment.md)：Docker、源码进程管理、网络、安全和上线检查。
- [升级与迁移](upgrading.md)：仓库远端、搭档自升级、Docker/人工升级、数据迁移与回滚。
- [备份与恢复](backup-and-restore.md)：应急上下文、`data/` 快照和整实例灾难恢复。
- [可观测性与日常运维](observability.md)：健康、诊断、日志、用量、成本和巡检节奏。
- [故障排查](troubleshooting.md)：服务、管理端、模型、记忆、Desktop、Relay 和容器的统一检查顺序。
- [自托管中继（Relay）](relay.md)：路径型公网入口、端到端加密、配对、封禁与运维。
- [Relay v1 协议](relay-protocol.md)：密钥派生、内层 TLS、虚拟 HTTP 与兼容边界。
- 涉及本地数据、外部服务和清理范围时，另见[数据与信任边界](../architecture/data-boundaries.md)。
