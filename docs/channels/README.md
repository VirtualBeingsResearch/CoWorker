# 通信与客户端

中文 · [English](README.en.md)

[← 返回文档索引](../README.md)

本目录描述 Coworker 如何接收外部消息、向通信对象回复，以及各客户端如何接入。

## 使用文档

- [API 与通信入口](api-and-channels.md)：REST、SSE、WebSocket、文件消息和 Bubble 直接转交。
- [Coworker Desktop](desktop.md)：连接本机用户、Codex 与 Claude Code 的桌面工作台。

## 设计文档

- [企微消息时序、可靠性与并发控制设计](wecom-message-ordering-and-concurrency.md)：梳理当前企微链路，定义按会话有序、跨会话受控并发、持久队列、幂等和精确回复关联的目标方案。
