# 公网访问（Cloudflare Tunnel）

中文 · [English](tunnel.en.md)

[← 返回配置与运维](README.md)

Cloudflare Tunnel 让 `cloudflared` 从本机主动建立出站连接，把
`http://127.0.0.1:<API__PORT>` 映射到一个 HTTPS 域名。不需要开放路由器端口，也不需要公网
IP。它适合**操作者自己**在手机或另一台电脑上打开值守台，或让外部客户端访问 `/v1`。

它不替代 [自托管中继（Relay）](relay.md)。Relay 是端到端加密的 Desktop 通道；Tunnel 在
Cloudflare 边缘终止 TLS，**Cloudflare 能看到明文**。需要保密时使用 Relay。

## 两种模式

| | named（配置的隧道） | quick（临时隧道） |
|---|---|---|
| 域名 | 固定，重启不变 | 每次启动随机生成 `*.trycloudflare.com` |
| 需要账号 | 是（Cloudflare Zero Trust） | 否 |
| 网页聊天 `/sse` | 可用 | **不可用**（quick tunnel 会缓冲 SSE） |
| 运行日志 `/logs/stream` | 可用 | **不可用** |
| Desktop 直连 | 可用 | 不可用 |
| 管理端轮询 | 可用 | 可用 |
| 适用 | 长期使用 | 临时体验、排障 |

## 前置条件

1. **必须显式设置 `API__COMMUNICATION_TOKEN`**。默认实例的 `/messages`、`/sse`、`/ws` 与
   `/logs/stream` 不需要认证；不设令牌就开隧道等于把这些入口公开。
2. **初始化必须完成**。首次初始化期间不能启用隧道。
3. 已安装 `cloudflared`：官方容器镜像已内置；源码或系统安装见下文。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `TUNNEL__MODE` | `off` | `off`、`quick` 或 `named` |
| `TUNNEL__TOKEN` | 空 | named 必填；Cloudflare Zero Trust 面板生成的隧道 token |
| `TUNNEL__PUBLIC_URL` | 空 | named 必填；公开 origin，例如 `https://coworker.example.com` |
| `TUNNEL__PROTOCOL` | `http2` | `http2`、`quic` 或 `auto`；受限网络保持 `http2` |
| `TUNNEL__BINARY` | 空 | 可选的 `cloudflared` 路径，默认在 `PATH` 中查找 |
| `TUNNEL__QUICK_ALLOWED_EMAILS` | `[]` | quick 模式的邮箱白名单，交给 cloudflared 做邮箱验证 |

`TUNNEL__TOKEN` 属于受保护配置：管理端只显示是否配置和尾号，不会回显完整值。

## 启用 named 隧道

1. 在 Cloudflare Zero Trust 创建 Tunnel，添加一条 Public Hostname，指向
   `http://127.0.0.1:8000`（容器内监听 `0.0.0.0` 时指向容器自身的 8000）。
2. 复制隧道 token，填入管理端「远程访问 → 公网访问」，同时填写同一个公开主机名。
3. 点「连接」。状态变为「已连接」后，用二维码在手机上打开值守台。

隧道每 60 秒探测一次公网地址；连续两次失败会重启 `cloudflared` 并重新连接。宿主崩溃后，
下次启动会回收上一次遗留的子进程（按 pid、创建时间和完整命令行精确匹配）。

## 启用 quick 隧道

在同一个卡片选择「临时」并连接。地址在每次启动时变化，旧链接立即失效。它只适合临时
体验：网页聊天、运行日志和 Desktop 都不可用，Cloudflare 也不为它提供可用性保证。

## 安装 cloudflared（源码运行）

官方容器镜像已经内置固定版本的 `cloudflared`。在源码或系统安装中，先安装再启用隧道：

```bash
# Windows
winget install --id Cloudflare.cloudflared

# macOS
brew install cloudflared

# Debian / Ubuntu：按 Cloudflare 官方仓库说明添加源后
apt install cloudflared

# 其他平台
# https://github.com/cloudflare/cloudflared/releases
```

应用不会在运行时下载或升级 `cloudflared`。找不到二进制时，管理端和 API 错误会给出对应
平台的安装提示。

## Desktop 连接

Desktop 可以用 named 隧道直连：生产模式要求 `https://` 加 Bearer 令牌，隧道天然满足，客户
端不需要任何改动。但有两个限制：

- 消息、附件和 SSE 内容在 Cloudflare 边缘是明文；需要端到端加密时使用 Relay。
- Desktop 不能携带 Cloudflare Access 的 service token 头，因此**桌面使用的 hostname 不要
  启用 Access**。需要 Access 时，把值守台和桌面拆成两个 hostname。

## 能力矩阵

| 能力 | named | quick |
|---|---|---|
| 管理端页面与轮询 | 可用 | 可用 |
| 网页聊天 `/sse` | 可用 | 不可用 |
| 运行日志 `/logs/stream` | 可用 | 不可用 |
| 聊天 WebSocket（脚本、外部客户端） | 可用 | 未验证 |
| Desktop 直连 | 可用 | 不可用 |
| `/v1` 短请求（小于 100 秒） | 可用 | 可用 |
| `/v1` 流式 | 可用 | 不可用 |
| 单次请求体小于 100 MB | 可用 | 可用 |
| 桌面更新制品上传 | 不可用（413） | 不可用（413） |
| 记忆压缩、回溯、备份恢复 | 不建议（易 524） | 不建议 |
| 首次初始化 | 不可用 | 不可用 |

## 失败对照

| 现象 | 常见原因 | 处理 |
|---|---|---|
| Cloudflare 1033 或本地 502 | `cloudflared` 连不上边缘 | 保持 `TUNNEL__PROTOCOL=http2`，检查出站网络 |
| Cloudflare 530 | 域名失去边缘注册 | 看门狗会自动重启；持续出现时检查 token 与主机名 |
| 524 | 100 秒内没有响应头 | 改用流式 `/v1`，长任务在本地执行 |
| 413 | 请求体超过 100 MB | 制品上传与恢复走本地 |
| 聊天或日志连上但收不到消息 | 使用了 quick 隧道 | 换 named 隧道 |
| 401 / 403 | 令牌缺失、错误，或 Access 策略拦截 | 检查 Bearer 与 Access 策略 |

## 安全边界

- Cloudflare 边缘可读明文，与 Relay 的端到端加密相反。
- 值守台 hostname 建议启用 **Cloudflare Access**（SSO 或邮箱验证码）：浏览器 cookie 自动
  生效，应用代码不需要改动。
- 在 Cloudflare 侧对 `/api/admin/session/verify` 与 `/api/admin/*` 配置限速；应用内不做限速。
- 审计日志记录隧道连接、断开、重连和测试事件，以及请求的真实来源 IP（`X-Forwarded-For`）。
- 不要把 `API__HOST` 改成 `0.0.0.0` 来配合隧道：`cloudflared` 连接回环地址即可。

## 验证清单

- [ ] `API__COMMUNICATION_TOKEN` 已显式配置，且与管理员令牌不同。
- [ ] named 模式重启后主机名不变，管理端与聊天都可用。
- [ ] `/v1/chat/completions` 使用 `stream=true` 时能持续收到事件。
- [ ] 强制结束 Coworker 进程后，系统里没有遗留的 `cloudflared` 进程。
- [ ] 断开公网后，看门狗能把隧道重启到新的可用状态。
