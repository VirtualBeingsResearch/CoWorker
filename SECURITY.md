# 安全策略

中文 · [English](SECURITY.en.md)

## 支持的版本

安全修复会提交到默认分支并随最新版本发布。除非发布说明另有声明，否则旧版本不再维护。

## 报告漏洞

请通过 GitHub 的 **Security → Report a vulnerability** 流程私下报告漏洞，并提供受影响版本、影响、复现步骤和可能的缓解建议。

如果无法使用私有漏洞报告，只能创建一个公开 issue 请求私下联系渠道。不要在公开 issue 中包含利用细节、凭据、个人数据或可能带有密钥的日志。

公开披露前，请给维护者留出确认和修复问题的时间。我们会按报告者意愿进行致谢。

## 安全模型

Coworker 是自主 Agent，不是安全沙箱。它的工具可以用运行进程的操作系统用户权限执行命令和读写文件。因此，模型输出以及网页、消息、附件、技能和记忆中的内容都必须视为不可信输入。

[数据与信任边界](docs/architecture/data-boundaries.md) 说明了哪些数据保存在本机、哪些数据可能离开设备，以及清理脚本会移除和不会移除的内容。

对于当前 v0.x 版本：

- 使用专用的最小权限用户运行 Coworker，或将它放在隔离的容器或虚拟机中。
- 只授予它访问可丢弃或已备份工作区的权限。
- 除非部署环境已专门进行隔离，否则不要提供生产凭据。
- API 默认绑定 `127.0.0.1`；配置通信令牌后，REST 消息、状态与 Desktop 通信都要求 Bearer token；此时未携带有效令牌的 `GET /status` 只返回基础生命周期信息，未配置令牌时 `/status` 保持返回完整快照。通过反向代理暴露服务时，应在代理层终止 TLS，显式设置 `API__HOST`，将 `API__CORS_ORIGINS` 配置为可信浏览器来源，并设置强 `API__COMMUNICATION_TOKEN`。
- `API__DEVELOPMENT_MODE=true` 不再关闭 Coworker API 侧任何通信 Bearer 检查；Desktop 侧的 HTTPS 检查由 Desktop 自己的 `security.development_mode` 单独控制。本机 HTTP 调试需要 Desktop 侧显式开启，且只适用于刻意配置的纯本机环境，绝不能在共享或公开监听地址上启用。
- 不要把 8000 端口直接暴露到公网或不可信网络。管理员令牌会保护管理 API，但它并不是每个路由的完整授权边界。
- 公网 Desktop 访问应使用[自托管中继（Relay）](docs/operations/relay.md)。新版 Desktop
  与 Coworker 在 Relay 字节流内建立固定公钥的 TLS 1.3；Relay只能观察连接元数据，不能
  解密或伪造业务请求。仍应使用非 root 容器、固定版本镜像、强本地管理员 Token、备份和
  最小化的 `RELAY_TRUSTED_PROXY_CIDRS`。
- Relay v1 只支持单节点。不要让多个副本共享 bbolt数据卷；不要通过负载均衡器把同一
  实例的隧道和请求分配到不同副本。
- 不要把 `.env`、`providers.json`、运行时数据、日志、导出的配置和桌面端凭据提交到仓库或附在漏洞报告中。

我们尤其欢迎报告身份验证绕过、命令或路径遍历、密钥泄露、不安全的更新处理、Relay
跨实例访问、Header边界混淆、封禁绕过，以及逃逸已记录权限边界的问题。
