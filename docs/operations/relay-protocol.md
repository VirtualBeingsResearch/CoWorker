# 中继（Relay）v1 协议与兼容边界

中文 · [English](relay-protocol.en.md)

[← 返回 Relay 运维](relay.md)

Go Relay、Python Coworker 和 Rust Desktop 都使用协议版本 `1`。版本、身份或签名不匹配时
连接失败，不会回退到明文 HTTP。

## 公网端点与配对

```text
GET /healthz
WS  /_relay/v1/pair
WS  /_relay/v1/coworker
WS  /i/{instance_id}/_relay/v1/connect
```

配对码由 32 字节随机值组成，十分钟有效且只能原子消费一次。Coworker 对 Relay nonce、
配对 ID 和实例公钥的固定字节编码计算 HMAC；Relay 返回其静态签名公钥、
`instance_id` 和签名绑定。之后控制连接使用实例 Ed25519 签名、随机 nonce、connection ID
和单调认证 epoch 防止重放、乱序和跨实例使用。

## Token 派生

通信 Token 必须为 `cwct_v1_` 加 32 字节 base64url（无填充）。HKDF-SHA256 使用
`coworker-relay-e2ee-v1` 域、实例 ID 绑定的 salt 和不同 purpose，分别派生：

- `relay-entry-auth`：Relay 入口挑战签名；
- `inner-tls-server`：Coworker 内层 TLS 服务身份；
- `inner-client-proof`：Desktop 内层客户端证明。

Coworker 只同步入口公钥和认证 epoch。Relay 得不到 Token 或两个内层私钥。入口挑战绑定
版本、实例、epoch、连接 ID、随机 nonce 和过期时间。

## 字节中继与内层 TLS

入口认证成功后，Relay 为 Desktop 分配 16 字节 session ID，并在 Coworker 控制通道发送
签名的 session-open。Desktop WebSocket 上的每个二进制消息都是不透明的 TLS 字节；
Coworker 控制通道的数据消息只在前方增加 session ID。Relay 限制外层帧大小、数量、队列
和连接时间，但看不到内部 stream 数或路由。

Desktop 与 Coworker 在字节流内建立 TLS 1.3。Desktop 只接受 Token 派生的 Coworker
Ed25519 证书公钥；握手后 Coworker 再发送一次性挑战，Desktop 用独立的
`inner-client-proof` 密钥签名。完成两步后才接受虚拟请求，因此 Relay 不能利用入口认证
结果冒充 Desktop。签名覆盖固定字段顺序的原始字节，不依赖 JSON 重序列化。

## 虚拟 HTTP

TLS 内层使用固定 10 字节帧头：

```text
version:u8 | type:u8 | stream_id:u32be | payload_length:u32be
```

帧支持客户端证明、请求开始/正文/结束、响应开始/正文/结束、取消、错误和 ping。Header
使用有序的 `[名称, 值]` 数组，重复值保持原顺序；正文与 SSE 都可分块流式传输。流 ID
支持并发，接收方必须实施有界队列、背压、帧长和 Header 限制。

Coworker 解密后统一执行 Relay 暴露策略，仅允许状态、Desktop 注册管理、消息、SSE 和
已发布桌面更新。原始 Bearer 仍由现有 ASGI 认证验证。

## 来源上下文

Relay 在 session-open 中签名来源 IP、公开 origin、实例和会话。Coworker 根据这个可信
上下文和解密后的原始 target 追加 `X-Coworker-Relay-*`、`Forwarded`、Original URL/Target
和 Request ID。客户端已有的同名 Header 保留在前；可信边界写入
`scope.state.coworker_relay`。认证、授权和来源判断只能读取可信上下文，不能相信客户端
提交的同名 Header。

## 兼容承诺

- v1 可以增加接收方明确忽略的控制字段，但不能改变签名输入、密钥 purpose 或帧含义。
- 新帧类型、认证语义或密钥派生变化需要新协议版本或明确能力协商。
- Relay 数据库 schema 明确标记版本；非 E2EE v1 schema 会停止启动并要求重新初始化。
- Relay 与 Coworker 应协同升级；只支持此协议的新版 Desktop。
