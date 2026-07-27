# 自托管 Relay

Coworker Relay 让内网中的 Coworker 主动建立加密出站连接，并通过统一路径为 Desktop 提供状态、注册、消息、SSE 和桌面更新服务：

```text
https://relay.example.com/i/{instance_id}
```

Relay 会终止公网 HTTPS，并能在请求处理期间看到 Header 和正文。它不是端到端加密服务，但不会持久化消息正文、附件或 SSE 事件。

## 部署

Go 服务和管理 CLI 位于 `apps/coworker-relay/`：

```bash
cd apps/coworker-relay
docker build -t coworker-relay .
go build -o relayctl ./cmd/relayctl
./relayctl init --public-url https://relay.example.com:8443
cd coworker-relay-deploy
docker compose up -d
```

`relayctl init` 会生成权限为 `0600` 的部署 `.env`、`compose.yaml` 和
`.gitignore`，持久化数据使用独立 Docker 卷，并自动生成且仅显示一次随机管理员 Token。外部端口默认
为 8443；已有生成文件时会拒绝覆盖，只有显式传入 `--force` 才会替换。域名默认
使用 ACME，可用 `--acme-domain <domain>` 指定证书域名；私有 CA、公网 IP 或
已有证书使用 `--tls-cert <path> --tls-key <path>`。`relayctl` 会自动读取当前
目录的 `.env`，也可通过 `RELAY_CONFIG` 指定其他文件。私有 CA 可通过
`RELAY_CA_CERT` 指向 PEM 信任包；它只扩展正常的证书信任，CLI 不提供跳过证书
校验的选项。

生成的容器以非 root 用户运行。ACME 模式下，Compose 会把宿主机 80 端口映射到
容器 8080 端口，并将 ACME 状态保存在 Relay 数据卷中。

最低配置：

```text
RELAY_PUBLIC_URL=https://relay.example.com
RELAY_ADMIN_TOKEN=<至少 24 个字符的随机管理令牌>
RELAY_DATABASE=/var/lib/coworker-relay/relay.db
RELAY_LISTEN=:8443
```

示例 Compose 将容器的 `8443` 端口发布为宿主机的 `8443` 端口。直接连接时将
`RELAY_PUBLIC_URL` 设为 `https://relay.example.com:8443`。如果由反向代理对外
提供标准 443 端口，则使用 `https://relay.example.com`，并将代理转发到容器
8443 端口。

TLS 可选择外部 PEM（`RELAY_TLS_CERT`、`RELAY_TLS_KEY`）或域名 ACME（`RELAY_ACME_DOMAIN`、可选的 `RELAY_ACME_EMAIL`）。公网 IP 和私有 CA 使用外部 PEM。ACME 模式还需开放 `RELAY_ACME_HTTP_LISTEN`，默认 `:80`。

经过其他反向代理部署时，用 `RELAY_TRUSTED_PROXY_CIDRS` 配置可信代理网段；其他来源提交的转发地址不参与封禁身份判断。

## 配对

```bash
export RELAY_URL=https://relay.example.com
export RELAY_ADMIN_TOKEN='<管理令牌>'
relayctl instance create --name home-coworker
```

如果在生成的部署目录中执行，`relayctl` 会读取 `.env`，无需手动执行上述两个
`export`。

将输出的一次性配对码填入 Coworker 管理台的“远程访问”页面。配对码十分钟后过期且只能使用一次。配对完成后，把页面显示的 Base URL 和 Bearer Token 填入 Desktop。

## 管理

```bash
relayctl instance list
relayctl instance update-auth cw_xxx optional
relayctl instance update-auth cw_xxx required
relayctl bans list --instance cw_xxx
relayctl bans remove --instance cw_xxx --ip 203.0.113.8 --reason "误封"
relayctl instance revoke cw_xxx
```

桌面更新最初使用 `optional`，允许旧版 Desktop 匿名更新。新版会为更新检查和安装包下载携带 Coworker Bearer。确认旧客户端完成迁移后，再切换为 `required`。

## 安全边界

- Relay 只开放 Desktop 通信和只读更新路径，不是通用 HTTP/TCP 代理。
- Relay 使用派生 Argon2id verifier 做前置认证；原始 Bearer 仍由 Coworker 接口再次认证。
- 同一实例和来源 IP 在十分钟内五次错误 Bearer 会被封禁一小时。
- 缺少 Bearer 不计密码失败，但受匿名速率限制。
- Relay 追加 `X-Coworker-Relay-*`，同时保留客户端原有同名 Header。Coworker 依靠已认证隧道而不是 Header 判断来源。
- 私网 Relay 应使用系统信任的私有 CA；不能关闭证书校验。
