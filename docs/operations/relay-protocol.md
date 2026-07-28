# Relay v1 协议与兼容边界

中文 · [English](relay-protocol.en.md)

[← 返回 Relay 运维](relay.md)

本文是 Go Relay 与 Python 内置 Relay Client 之间的 v1 契约。两端都必须发送协议版本
`1`；版本不一致时 Relay 返回 `426 protocol_incompatible`，不会尝试降级。

## 连接与帧

Coworker 使用实例长期凭据建立
`wss://<relay>/_relay/v1/connect`。隧道采用 UTF-8 JSON 文本帧，帧类型包括：

- `ping`、`pong`：连接存活检测。
- `verifier`、`verifier_ack`：原子同步 communication token 的 Argon2id verifier。
- `request`：一个经过 Relay 前置认证的 HTTP 请求。
- `response_start`、`response_body`、`response_error`：流式响应。
- `cancel`：公网请求断开、超时或背压溢出时取消 Coworker 内部请求。

`request_id` 在一个隧道连接内关联请求和响应。Relay不会在断线后自动重放普通业务
请求，也不提供离线 outbox；这避免在无法确认处理结果时重复发送消息。Desktop和
Coworker现有协议负责其自身已有的 ACK 和去重语义。

## Header 与来源上下文

请求帧中的 `headers` 是 `[名称, 值]` 对组成的数组。`relay_header_start` 指向 Relay
追加 Header 的第一个元素，因此 Coworker可以区分客户端值和 Relay可信追加值。

Go 的 `net/http` 会保留同名 Header 的多个值，但不会暴露 HTTP 报文中不同名称 Header
的原始全局顺序。Relay因此按名称确定性排列客户端 Header，并保留每个名称内部的值顺序；
随后追加 Relay Header。任何认证、授权或来源 IP 判断都只能使用已认证隧道上下文和追加
区域，不能信任客户端提交的同名 Header。

Python Client会拒绝 Header数量、总大小、名称、编码或 `relay_header_start` 边界不合法
的请求帧，并校验 v1 要求的 Relay追加 Header集合、实例和 Request ID；无效帧不会进入
Coworker ASGI应用。

## 流、限制和背压

- v1 将完整请求正文读入内存后，以 Base64 放入单个 `request` 帧；上限为 32 MiB。
- 隧道文本帧上限为 48 MiB，用于容纳 Base64和协议开销。
- 响应正文通过多个 `response_body` 帧发送，适用于 SSE和安装包下载。
- 每个响应流有有界缓冲区。消费者过慢时 Relay取消该流，不允许无界占用内存。
- 客户端断开时 Relay发送 `cancel`，但取消是尽力而为，不能作为事务回滚机制。

真正的流式请求上传、断线续传请求和通用文件隧道不属于 v1。

## 更新缓存

v1 的更新路由仍由 Coworker现有只读更新端点产生响应。Relay只缓存已通过路由白名单
且状态为 `200` 的安装包响应，并在缓存命中时重新执行认证。客户端不能向 Relay提交
任意上游 URL；Relay自身也不充当通用上游下载器。

缓存键由实例和资产路径组成，无语义的查询参数不会生成重复副本；同一资产的并发填充锁会
在最后一个使用者退出后回收。内容在写入、Relay重启后的首次读取、文件发生变化以及周期
复核时使用 SHA-256校验，普通命中不重复读取整个安装包。缓存支持 ETag和 Range读取。
Desktop继续独立验证 Tauri更新签名，Relay缓存校验不能替代发布签名。

## 兼容承诺

- 协议 `1` 内可以增加接收方会忽略的响应 JSON 字段，但不能改变已有字段含义。
- 新帧类型、请求分块上传或认证语义变化需要新的协议版本或能力协商。
- Relay和 Coworker版本应一起升级；`coworker-relay health` 会显示 Relay构建版本和协议版本。
- v1 是单节点协议。实例在任一时刻只能有一个活动隧道，新连接会替换旧连接。
