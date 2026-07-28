# 自托管 Relay

中文 · [English](relay.en.md)

Coworker Relay 让内网中的 Coworker 主动建立加密出站连接，并通过统一路径为 Desktop 提供状态、注册、消息、SSE 和桌面更新服务：

```text
https://relay.example.com/i/{instance_id}
```

Relay 会终止公网 HTTPS，并能在请求处理期间看到 Header 和正文，但不会持久化消息正文、附件或 SSE 事件。

## 部署

`apps/coworker-relay/` 下的 `coworker-relay` 提供 Relay 服务和管理命令。
先从 Coworker Release 下载适合当前系统的 Relay
压缩包，将其中的可执行文件放入 `PATH`，然后运行：

```bash
coworker-relay init
cd coworker-relay-deploy
docker compose up -d
```

`coworker-relay init` 在终端中启动交互式向导；自动化部署可继续使用
`coworker-relay init --public-url https://relay.example.com:8443`。运行
`coworker-relay --help`、`coworker-relay init --help` 或任意子命令的 `--help` 可查看完整用法，
帮助命令不会读取配置或访问 Relay。

每次 Coworker发行会同时提供 GHCR 的 `linux/amd64`、`linux/arm64` Relay镜像，以及
Linux、macOS和 Windows 的 `coworker-relay` 压缩包、SHA-256和构建来源证明。
建议生产部署固定版本标签。源码开发时仍可在 `apps/coworker-relay/` 执行
`docker build -t coworker-relay .` 和 `go build ./cmd/...`。

`coworker-relay init` 会生成权限为 `0600` 的部署 `.env`、`compose.yaml` 和
`.gitignore`，持久化数据使用独立 Docker 卷，并自动生成随机管理员 Token。Token 只写入
`.env`，不会打印到终端。外部端口默认为 8443；已有生成文件时会拒绝覆盖，交互式向导会
请求确认，非交互模式只有显式传入 `--force` 才会替换。公网域名和公网 IP 默认使用
ACME 自动申请、安装和续期证书；私网 IP、私有 CA 或已有证书使用
`--tls-cert <path> --tls-key <path>`。`coworker-relay` 的服务和管理命令都会自动读取当前
目录的 `.env`，也可通过 `RELAY_CONFIG` 指定其他文件；进程中显式设置的环境变量优先。
容器默认执行 `coworker-relay serve`。私有 CA 可通过
`RELAY_CA_CERT` 指向 PEM 信任包；它只扩展正常的证书信任，CLI 不提供跳过证书
校验的选项。

生成的容器以非 root 用户运行。ACME 模式下，Compose 会把宿主机公网 TCP 80 端口映射到
容器 8080 端口，并将 ACME账户、私钥、证书和续期状态保存在 Relay 数据卷中。公网 IP
证书使用 `shortlived` profile；Relay 会在证书生命周期约三分之二处开始续期，失败时继续
使用尚未过期的旧证书并每小时重试。首次签发失败或旧证书已过期时，HTTPS 保持未就绪并
以带抖动的指数退避重试；Relay 不会以不安全的明文或自签名模式启动。

最低配置：

```text
RELAY_PUBLIC_URL=https://relay.example.com
RELAY_ADMIN_TOKEN=<至少 24 个字符的随机管理令牌>
RELAY_DATABASE=/var/lib/coworker-relay/relay.db
RELAY_LISTEN=:8443
```

容器 secret可用 `RELAY_ADMIN_TOKEN_FILE=/run/secrets/relay-admin-token` 代替直接的
`RELAY_ADMIN_TOKEN`；两者不能同时设置。

示例 Compose 将容器的 `8443` 端口发布为宿主机的 `8443` 端口。直接连接时将
`RELAY_PUBLIC_URL` 设为 `https://relay.example.com:8443`。如果由反向代理对外
提供标准 443 端口，则使用 `https://relay.example.com`，并将代理转发到容器
8443 端口。

TLS 可选择外部 PEM（`RELAY_TLS_CERT`、`RELAY_TLS_KEY`）、域名 ACME
（`RELAY_ACME_DOMAIN`）或公网 IP ACME（`RELAY_ACME_IP`），ACME 可选
`RELAY_ACME_EMAIL`。证书标识必须与 `RELAY_PUBLIC_URL` 的主机一致；公网 IP 自动固定
使用 `shortlived` profile。ACME 模式还需保证公网 TCP 80 能到达
`RELAY_ACME_HTTP_LISTEN`，默认 `:80`；如果端口被占用、NAT 未转发或地址不属于本机，
签发会失败。私网 IP 和私有 CA 使用外部 PEM。

经过其他反向代理部署时，用 `RELAY_TRUSTED_PROXY_CIDRS` 配置可信代理网段；其他来源提交的转发地址不参与封禁身份判断。

## 配对

```bash
export RELAY_URL=https://relay.example.com
export RELAY_ADMIN_TOKEN='<管理令牌>'
coworker-relay instance create --name home-coworker
```

如果在生成的部署目录中执行，`coworker-relay` 会读取 `.env`，无需手动执行上述两个
`export`。

将输出的一次性配对码填入 Coworker 管理台的“远程访问”页面。配对码十分钟后过期且只能使用一次。配对完成后，把页面显示的 Base URL 和 Bearer Token 填入 Desktop。“测试远程连接”会携带当前通信 Token 请求公开实例的 `/status`，同时验证公网 HTTPS、Relay 前置认证、活动隧道和 Coworker 响应。

## 管理

```bash
coworker-relay health
coworker-relay version
coworker-relay instance list
coworker-relay instance update-auth cw_xxx optional
coworker-relay instance update-auth cw_xxx required
coworker-relay instance update-stats cw_xxx
coworker-relay bans list --instance cw_xxx
coworker-relay bans remove --instance cw_xxx --ip 203.0.113.8 --reason "误封"
coworker-relay cache inspect
coworker-relay metrics
coworker-relay gc
coworker-relay instance revoke cw_xxx
```

在 Coworker管理台点击“轮换实例凭据”会先在 Relay暂存新凭据摘要；旧凭据保持有效，
直到 Coworker持久化新凭据并用它完成 WSS认证后才原子失效。响应丢失或中途断网不会锁死
实例。管理员应急操作也可
执行 `coworker-relay instance rotate-credential cw_xxx`，但输出的新凭据必须同步写入对应
Coworker配置，否则隧道会保持离线。管理员 Token是单一共享高权限凭据，v1不提供
多管理员 RBAC；请通过 secret manager分发并限制 CLI主机。

轮换管理员 Token时，先生成至少 32 bytes随机值并更新部署 `.env` 中的
`RELAY_ADMIN_TOKEN`，再执行 `docker compose up -d --force-recreate relay`。旧 Token在
新进程启动后立即失效；轮换前应先完成数据库备份，并通过安全渠道更新 CLI配置。

桌面更新最初使用 `optional`，允许旧版 Desktop 匿名更新。新版会为更新检查和安装包下载携带 Coworker Bearer。确认旧客户端完成迁移后，再切换为 `required`。

## 备份、升级与恢复

升级前创建一致的在线 bbolt快照：

```bash
coworker-relay backup --output relay-before-upgrade.db
docker compose pull
docker compose up -d
coworker-relay health
```

恢复必须在 Relay停止后进行。`--force` 不直接删除旧数据库，而是把它重命名为带
`before-restore` 时间戳的可恢复文件：

```bash
docker compose stop relay
coworker-relay restore \
  --from relay-before-upgrade.db \
  --database /path/to/mounted/relay.db \
  --force
docker compose start relay
```

Relay数据库具有显式 schema version。程序拒绝打开比自身更新或不受支持的 schema，
不会猜测降级。备份数据库、`.env`、ACME状态和 Coworker本地实例凭据都属于敏感数据；
应加密保存并验证恢复演练。`coworker-relay gc` 可立即清理过期配对、失败和封禁记录，服务也会
每小时自动清理。撤销实例会级联删除其安全状态、统计和缓存。

容器收到 SIGTERM 后会停止接受新请求、关闭隧道并给 HTTP请求最多 30 秒 drain时间。
Compose配置使用 35 秒 stop grace period。

## 健康、指标与日志

- `/_relay/v1/livez` 只表示进程存活。
- `/_relay/v1/readyz` 和兼容端点 `/_relay/v1/health` 会检查 draining状态、数据库和
  缓存目录可用性，并返回构建与协议版本。
- `coworker-relay metrics` 返回受管理员 Bearer保护的 Prometheus文本，包括请求、认证失败、
  封禁、延迟、Argon2并发、隧道连接、在线隧道和缓存容量/命中率。

Relay输出结构化 JSON日志到 stdout。安全日志包含完整来源 IP、实例、路由类别、认证结果
和 Request ID，但不包含 Token、Cookie、正文、附件或完整原始 URL。部署方必须在容器
运行时设置日志轮转和保留期，并按个人数据处理来源 IP。建议对 ready失败、认证失败突增、
重连风暴、缓存接近配额和 TLS证书临期设置告警。

## 容量与拓扑边界

v1是单节点服务：bbolt位于本地卷中，隧道归属保存在进程内，不能把同一数据卷同时挂给
多个 Relay副本。可以使用冷备份恢复，但不支持 active-active或无状态滚动升级。

公网请求每个实例和来源 IP默认每分钟 600 次，匿名请求默认每分钟 60 次；Argon2验证具有
全局并发上限。可通过 `RELAY_REQUESTS_PER_MINUTE`、`RELAY_ANONYMOUS_PER_MINUTE`、
`RELAY_VERIFIER_CONCURRENCY`、`RELAY_BAN_FAILURE_LIMIT`、`RELAY_BAN_FAILURE_WINDOW`
和 `RELAY_BAN_DURATION` 调整策略。请求 Header上限 32 KiB、请求正文上限 32 MiB、
隧道帧上限 48 MiB；部署方可用 `RELAY_MAX_REQUEST_BODY_BYTES` 和
`RELAY_MAX_TUNNEL_FRAME_BYTES` 调低但不能突破协议上限。
上线前应按实际 SSE连接数、更新包大小和实例数进行负载测试，并配置足够的文件描述符、
内存和缓存卷。反向代理必须允许 WebSocket升级，关闭 SSE响应缓冲，并把空闲超时设为
大于 Relay的 90 秒；只有 `RELAY_TRUSTED_PROXY_CIDRS` 中的代理才可提供来源 IP。

协议的帧、Header顺序和重试边界见 [Relay v1 协议](relay-protocol.md)。

## 安全边界

- Relay 只开放 Desktop 通信和只读更新路径，不是通用 HTTP/TCP 代理。
- Relay 使用派生 Argon2id verifier 做前置认证；原始 Bearer 仍由 Coworker 接口再次认证。
- 同一实例和来源 IP 在十分钟内五次错误 Bearer 会被封禁一小时。
- 缺少 Bearer 不计密码失败，但受匿名速率限制。
- 配对、隧道认证、凭据轮换和管理员 API 在执行凭据校验前也有来源 IP 限速。
- Relay 追加 `X-Coworker-Relay-*`，同时保留客户端原有同名 Header。Coworker 依靠已认证隧道而不是 Header 判断来源。
- 私网 Relay 应使用系统信任的私有 CA；不能关闭证书校验。
- v1 不提供 P2P、离线消息存储、任意上游下载、通用反向代理或多节点高可用。
