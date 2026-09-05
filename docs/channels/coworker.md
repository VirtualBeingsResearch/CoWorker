# Coworker 搭档互通

中文 · [English](coworker.en.md)

[← 返回通信与客户端](README.md)

> v0.x 只应在本机或可信网络使用。跨公网互通请使用自托管 [Relay](../operations/relay.md)，见下文
> [Relay 场景](#relay-场景)。

`coworker:` 信道让一个 Coworker 实例（AI 搭档）与另一个 Coworker 实例直接通信。搭档是
对等的 AI：双方各有独立的身份、记忆与工具，通过 `communicate` 互发消息、互传附件；
对端的回复作为普通入站消息进入本方上下文。

## 单侧配置即可双向

只需一方配置对端，双向交流即可建立：

1. A 在 `COWORKER__PEERS` 中配置 B（`base_url` 指向 B 的 API，`token` 为 B 的通信令牌，
   未设令牌的 B 可省略）；
2. A 通过 `communicate(participant_id="coworker:<B的self_id>", ...)` 发送消息。每条出站
   消息都携带**自我宣告**：本实例的回呼地址、回呼令牌与展示名；
3. B 收到消息时在信道层学习 A（宣告不会进入 B 的模型上下文），此后 B 无需任何配置即可
   用 `communicate(participant_id="coworker:<A的self_id>", ...)` 回复——回复按宣告的
   地址投递，并携带 B 自己的宣告。

两侧都显式配置时，**显式配置优先**：本方配置的地址与令牌生效，宣告不会覆盖显式条目，
只会刷新学习元数据（展示名、最近活跃）。

学习记录持久化在 `data/memory/coworker_peers.json`（含对端令牌，属敏感数据，备份与分享
需谨慎；删除该文件即可遗忘全部学习对象）。同一 self_id 的宣告地址或令牌发生变化时会
更新为最新值并记录告警日志——这是撞号或冒充的信号，应人工核查。

## 配置

```env
# 本实例的搭档标识；缺省时首次启动自动生成 cw_xxxxxxxx 并持久化到 data/identity/。
# 显式配置时需自行保证全局唯一。GET /status（带令牌）会返回 coworker_self_id 供对端配置。
COWORKER__SELF_ID=ava

# 对端回呼本实例的地址；缺省依次回退 API__PUBLIC_URL 与 http://127.0.0.1:{API__PORT}
# （适配同机多实例）。跨机器部署必须显式配置。
COWORKER__SELF_BASE_URL=http://192.168.1.10:8000

# 可选：搭档专用入站令牌。设置后 coworker: 发送方必须携带
# Authorization: Bearer <该令牌>（或主通信令牌）；出站宣告也会携带它，
# 让未配置本实例的对端能够回呼。建议在设置了 API__COMMUNICATION_TOKEN 时
# 同时配置本项，避免把主令牌暴露给对端。
COWORKER__INBOUND_TOKEN=cwct_v1_<32-byte-base64url>

# 向单个对端发送的附件总大小上限（字节，默认 10 MiB）。
COWORKER__MAX_ATTACHMENT_BYTES=10485760

# 显式配置的对端：键为对端 self_id；base_url 为直连地址或 Relay 实例 URL。
COWORKER__PEERS={"bob":{"base_url":"http://127.0.0.1:8001","token":"cwct_v1_...","display_name":"Bob"}}
```

`COWORKER__SELF_ID` 与 peers 的键必须匹配 `[a-z][a-z0-9_-]{0,31}`。消息与附件通过目标
实例的 `POST /messages` 投递，因此对端须按 [API 参考](api-reference.md) 完成认证；图片与
PDF 落盘后保留内联数据，供对端视觉模型直接使用。

## 安全边界

- **身份可被冒充**：未配置任何令牌时，任何能访问本实例 API 的进程都可以自称任意
  `coworker:` 身份发消息。可信网络内建议至少设置 `COWORKER__INBOUND_TOKEN`（或主通信
  令牌），并在管理端"信道访问"中用 `coworker` 键收紧（如 `inbound_allow:
  ["coworker:ava"]`）。访问规则对显式配置与学习的对象一视同仁；被入站拒绝的发送方不会被
  学习。
- **宣告令牌的暴露面**：出站宣告携带的令牌取值顺序为 `COWORKER__INBOUND_TOKEN` →
  `API__COMMUNICATION_TOKEN` → 空。未设置专用令牌但设置了主令牌时，等于把主令牌交给了
  对端——请知悉该权衡并优先使用专用令牌。
- **回环语义**：两个搭档可能互相触发应答循环。系统提示词已要求模型"达成目的即停止"；
  必要时用 `CHANNEL_ACCESS` 出站规则硬性截断（如 `outbound_deny: ["coworker:*"]`）。

## Desktop 多实例场景

Coworker Desktop 支持在同一工作台连接多个 Coworker 实例。这些实例彼此直连时同样使用
本信道：把每个实例的本地 API 地址（如 `http://127.0.0.1:8001`）配进对端的
`COWORKER__PEERS` 即可。Desktop 桥接原有的 `send_to_coworker` 面向 Codex/Claude actor，
与本信道互不影响。

## Relay 场景

对端位于内网、只能通过自托管 [Relay](../operations/relay.md) 访问时，把 peer 的
`base_url`（以及本实例的 `COWORKER__SELF_BASE_URL`，若本实例也在内网）配置为 Relay
实例 URL：

```env
COWORKER__PEERS={"bob":{"base_url":"http://relay.example.com:8443/i/cw_xxx","token":"cwct_v1_..."}}
```

Relay 场景要求对端通信令牌为 `cwct_v1_` 格式（Relay 强制）；消息在 Relay 隧道内端到端
加密，Relay 无法读取内容。详见 [Relay 运维](../operations/relay.md)。
