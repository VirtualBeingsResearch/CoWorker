# 自托管中继（Relay）

中文 · [English](relay.en.md)

Coworker 中继让内网中的 Coworker 主动建立出站连接，并让新版 Desktop 通过同一个公网
入口访问状态、注册、消息、SSE 和桌面更新：

```text
http://relay.example.com:8443/i/{instance_id}
```

外层可以是普通 HTTP/WebSocket，因为 Desktop 与 Coworker 会在中继转发的字节流内建立
固定公钥的 TLS 1.3 连接。中继只能看到来源 IP、实例、连接时间、流量大小和时序，不能
读取或伪造 Token、路径、Header、消息、附件、SSE 或更新制品。中继仍能丢弃、延迟和
限速流量，因此可用性仍取决于中继。

旧版 Desktop 不支持此协议，继续使用 Coworker 直连地址。中继没有旧式 HTTP 代理门面；
访问 `/i/{instance_id}/status` 等普通路径会返回 `404`。

## 初始化与部署

`apps/coworker-relay/` 提供单一的 `coworker-relay` 服务与管理工具。Release 同时提供
Relay 镜像和各平台二进制。首次运行 `init` 时，向导会询问是否使用容器。

选择容器部署（默认）后运行：

```bash
mkdir coworker-relay-deploy
cd coworker-relay-deploy
coworker-relay init
docker compose up -d
```

选择原生部署后，生成的配置会使用宿主机数据路径和仅监听回环地址的管理端口：

```bash
mkdir coworker-relay-deploy
cd coworker-relay-deploy
coworker-relay init --deployment native \
  --public-url http://203.0.113.10:8443
coworker-relay serve
```

向导会显示公网地址样例并默认使用 `http://<host>:8443`。无需域名、证书、ACME 或公网
80 端口；也可以使用 `--deployment container|native` 非交互初始化：

```bash
coworker-relay init \
  --public-url http://203.0.113.10:8443 \
  --deployment native
```

`coworker-relay --help` 和每个子命令的 `--help` 都可直接查看。生成的 `.env` 权限为
`0600`，包含随机管理员 Token；服务与 CLI 默认读取当前目录的 `.env`，也可用
`--config` 或 `RELAY_CONFIG` 指定其他文件。现有文件不会被静默覆盖。
`init` 默认把部署文件写入当前目录；也可用 `--dir` 指定其他目录。

原生模式把数据库和 Relay 签名密钥保存在部署目录的 `data/` 下。Compose 模式则默认将
公网 `8443` 映射到容器，并仅把管理端口映射到宿主机回环地址：

```text
0.0.0.0:8443 -> relay:8443
127.0.0.1:8444 -> relay:8444
```

管理端口不得暴露到公网。管理员应通过 SSH 登录 Relay 主机后运行 CLI。最低配置为：

```text
RELAY_PUBLIC_URL=http://relay.example.com:8443
RELAY_LISTEN=:8443
RELAY_ADMIN_LISTEN=:8444
RELAY_DATABASE=/var/lib/coworker-relay/relay.db
RELAY_SIGNING_KEY=/var/lib/coworker-relay/relay-signing.key
RELAY_ADMIN_TOKEN=<至少 24 个字符的随机值>
```

可以在前方部署 HTTPS/WSS 反向代理；此时 `RELAY_PUBLIC_URL` 使用公开的 HTTPS origin，
代理必须支持 WebSocket。内层端到端加密始终存在。只有
`RELAY_TRUSTED_PROXY_CIDRS` 中的代理可提供来源 IP。

## 配对与 Desktop

在部署目录中创建十分钟有效、仅使用一次的配对码：

```bash
coworker-relay instance create --name home-coworker
```

在 Coworker 管理后台的“远程访问”页面填写 Relay 地址和配对码。配对使用挑战-HMAC，
配对码不以明文发送；Coworker 会生成实例密钥并固定 Relay 签名公钥。完成后把页面显示的
Base URL 和现有 communication token 填入新版 Desktop：

```text
Base URL: http://relay.example.com:8443/i/cw_xxx
Bearer Token: cwct_v1_...
```

如果十分钟内没有完成配对，Relay 会在下一次每分钟运行的垃圾回收中自动删除这个未配对
实例。已经完成配对的实例不会因配对码记录过期而删除。

Desktop 不需要 transport、证书或公钥字段。它会从精确的实例路径识别 Relay，显示
“Relay / 端到端加密”，并且在连接失败、身份不匹配或协议不兼容时不会回退到明文 HTTP。

Relay 要求 communication token 使用 `cwct_v1_<32-byte-base64url>` 格式。没有 Token 时，
启用 Relay 会自动生成并写入受保护配置；已有弱格式 Token 时必须显式轮换，避免静默破坏
现有直连 Desktop。轮换后所有 Desktop 都需要更新 Token。

“测试连接”会验证真实的配对或控制连接；它不请求公开 `/status`。

## 管理、封禁与备份

```bash
coworker-relay health
coworker-relay version
coworker-relay instance list
coworker-relay instance revoke cw_xxx
coworker-relay bans list --instance cw_xxx
coworker-relay bans remove --instance cw_xxx --ip 203.0.113.8 --reason "误封"
coworker-relay metrics
coworker-relay gc
coworker-relay backup --output relay-backup.db
```

Relay 对入口挑战签名失败按 `instance_id + 来源 IP` 计数；十分钟内五次失败会封禁一小时，
重启后仍有效。解禁必须记录原因。验签前还有连接频率、帧大小和全局验签并发限制。Relay
只转发有上限的二进制块，不理解内部请求或路由。

升级前备份数据库、`.env` 和 Relay 签名密钥。检测到非 E2EE Relay v1 的数据库 schema
时，进程会停止并要求先备份，再删除旧数据并重新初始化，不会猜测迁移。

Relay v1 是单节点服务，不能让多个副本共享 bbolt 数据卷，也不能把同一实例随机分配到
多个副本。SIGTERM 会停止新连接、关闭隧道并进行有界退出。

## 数据与安全边界

- 公网只开放 `GET /healthz`、配对 WebSocket、Coworker 控制 WebSocket 和实例 Desktop
  WebSocket。
- Relay 持久化实例公钥、认证 epoch、配对状态、来源 IP 封禁、审计和聚合流量统计；不缓存
  更新或业务内容。
- 原始 Token、Authorization、请求路径、Header、正文、消息、附件和更新内容不得进入
  Relay 日志、数据库、指标、错误响应或崩溃信息。
- Coworker 解密后只允许 Desktop 通信与只读更新路由；管理、模型、日志、备份、发布和
  任意 HTTP/TCP 代理路径不会开放。
- 原始 Bearer 位于密文请求中，并继续由 Coworker 现有接口认证。
- 更新检查和制品下载同样经过端到端加密，安装包仍由 Tauri updater 验证发布签名。
- v1 不提供 P2P、WebRTC、WireGuard、离线消息、多设备独立授权或多节点高可用。

协议字节、密钥域和兼容规则见 [Relay v1 协议](relay-protocol.md)。
