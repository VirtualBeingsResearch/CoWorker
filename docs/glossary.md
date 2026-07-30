# 术语表

中文 · [English](glossary.en.md)

[← 返回文档索引](README.md)

| 术语 | 含义 |
|---|---|
| Coworker / 搭档 | 持续运行的 Agent 实例及其身份，不只是某个客户端 |
| 主线（main line） | 持有持续对话上下文、接收普通消息的主要 Agent 循环 |
| Participant | 一个通信对象的稳定身份；用于路由和短期对话隔离，不等同账号权限 |
| Conversation | Participant 下可选的进一步会话标识 |
| Channel | 企业微信、微信、Stream 等独立入站/出站传输 |
| Stream | 为 WebSocket、SSE 和 Desktop profile 提供连接、注册与 outbox 的共享 Runtime |
| Bubble / 泡泡 | 独立上下文和工具作用域中的并行任务，可与主线通信或绑定参与者 |
| 潜意识 | 由 MODE 调度的后台 Bubble，用于总结、审计、探索、园艺或元反思 |
| Skill | `SKILL.md` 定义的可复用工作方法；流程知识 |
| Palace / 记忆宫殿 | `PALACE.md` 定义的领域组合层，把薄卡片、Skill 和带标签记忆装配进 Bubble |
| Identity | 名字、人格、现居地和自述等持续身份材料 |
| 短期记忆 | 当前主线可直接看到的消息、工具调用和压缩锚点 |
| 记忆树 | 按时间尺度压缩短期历史的多分辨率结构 |
| 长期记忆 | mem0 管理、可语义检索和带标签维护的持久事实或经验 |
| Pinned context | 压缩后仍会重新注入的少量关键信息或文件内容 |
| Provider | Anthropic、OpenAI、DeepSeek 等模型 API 方言的实现 |
| Provider 实例 | 具有唯一注册名、密钥、Base URL 和默认模型的一份 Provider 配置 |
| fallback | 主模型失败时按顺序尝试的 Provider/模型链 |
| Relay | 为远程 Desktop 和 Coworker 转发端到端加密字节流的自托管单节点入口 |
| Bridge | Desktop 中连接 Local、Codex、Claude Code 与 Coworker 的 Rust 运行层 |
| Explore Lab | 从敏感实例快照创建可分叉、单步、回放和比较实验分支的开发工具 |
| 应急备份 | 连续 Agent 错误时保存的短期上下文快照，不是整实例灾难备份 |
| `restart_self` | 校验当前代码环境、保存短期快照并请求平台启动器安全重启的主线工具 |

更完整的关系见[运行时架构与消息流](architecture/runtime-flow.md)和
[核心概念与能力](architecture/concepts.md)。

[← 返回项目首页](../README.md)
