# 故障排查

中文 · [English](troubleshooting.en.md)

[← 返回配置与运维](README.md)

本页提供 Coworker 服务、管理后台、模型、Desktop、Relay 和容器部署的统一排查顺序。先收集
证据再改变状态；不要把删除 `data/`、配置文件、Docker 卷或重装应用当作第一步。

## 通用检查顺序

1. **缩小范围**：服务没启动、管理页不可用、模型调用失败，还是只有某个 Channel/actor
   失败？
2. **记录时间和版本**：记下问题发生时间、Coworker/Desktop 版本、运行方式和最近改动。
3. **检查状态**：查看终端、管理后台“诊断与审计”、Desktop“状态”或 Relay 健康信息。
4. **对齐日志**：查看同一时间段最早出现的 ERROR/WARN，不要只看最后一条连锁错误。
5. **验证配置来源**：确认当前进程实际读取的配置文件、环境和工作目录。
6. **做最小恢复**：重试单个连接或安全重启；先备份再执行恢复、清理或迁移。

报告问题时不要附带 Bearer token、API Key、Relay 私钥、二维码内容、完整消息正文或未经
检查的配置导出包。

## Coworker 无法启动

先确认：

```bash
uv --version
python3 --version
uv run python scripts/check_version.py
```

- Python 必须满足项目要求；
- 依赖应从当前 checkout 的锁文件安装；
- 同一个工作目录不要同时启动多个 Coworker 进程；
- 检查 `data/` 是否可写、磁盘是否已满；
- 检查 `8000` 端口是否已被其他进程使用；
- Intel macOS 应通过 Dev Container 或 Docker 运行 Python 服务。

浏览器工具单独失败但 Agent 能启动时，通常只需安装 Chromium：

```bash
uv run playwright install chromium
```

Debian/Ubuntu 缺少系统库时：

```bash
uv run playwright install --with-deps chromium
```

不要因为浏览器依赖失败就清空记忆或身份数据。

## 管理页面打不开或无法登录

### 页面完全不可达

- 确认 Coworker 进程仍在运行；
- 默认地址是 <http://127.0.0.1:8000/admin>；
- 容器部署确认端口映射和容器健康；
- 如果从另一台设备访问，不要临时公开 `8000`；应使用受控反向代理或项目支持的 Relay
  场景。

### 令牌被拒绝

- 使用当前启动终端显示的有效管理员令牌；
- 检查进程是否读取预期工作目录下的 `data/admin_config.json`；
- 不要混用 Desktop 通信 token、Relay token 和管理员 token；
- 浏览器保存了旧 token 时，退出管理会话后重新输入。

首次设置未完成时，普通页面和 API 会被引导到 `/admin`，Agent 主循环和外部 Channel 不会
启动。这是 setup mode，不是运行故障。

## 模型或 Provider 调用失败

依次检查：

1. 管理后台“模型编排”中的当前 Provider、模型 ID 和 fallback；
2. “运行设置”中 Provider API Key、Base URL 和 TLS 配置是否已保存；
3. 模型或网关是否支持 tool/function calling；
4. 账号配额、速率限制和网络访问；
5. 摘要、视觉和主线模型是否错误地指向不同或已失效的 Provider；
6. 日志中的第一个上游响应状态，而不是后续的恢复错误。

手动输入的模型没有经过在线能力探测。普通文本生成成功并不代表它能执行工具调用。

如果刚更换长期记忆 embedding 模型，停止继续写入并检查迁移要求。已有 Chroma 数据不能
假定与另一个 embedding 模型兼容。

## 记忆、任务或上下文异常

- 短期上下文过大：先在“记忆中心”检查消息尾部和记忆树，再考虑全量压缩。
- 回溯一直运行：查看 `GET /backfill_tree` 或管理页进度；不要同时执行离线回溯。
- 长期记忆搜不到：确认 mem0 Provider、embedding 模型和数据库路径没有改变。
- 重启后缺少近期状态：检查短期快照和 `data/logs`，再查看“运行中心”的应急备份。
- 任务或闹钟不触发：确认时区、Passive mode、Agent 是否正在运行，以及任务是否已被取消。

应急备份恢复分两种：

- 先用摘要恢复把历史重新注入当前上下文；
- 只有需要完整替换当前短期上下文时才使用完整恢复。

恢复前记录当前版本、备份文件名和消息数量。应急短期备份不能替代整个运行目录的备份。

## Desktop 无法连接

在 Desktop 中保存所有修改，选择目标 Coworker，然后在“状态”运行诊断。

### Coworker 诊断失败

- 地址是否属于当前目标实例；
- 直连生产地址是否使用 HTTPS；
- Bearer token 是否是管理员或独立 Desktop 通信 token；
- Coworker 是否已经完成首次设置；
- Coworker 的 Desktop Channel runtime 是否正在启动或重启；
- 使用 Relay 时，地址是否精确包含正确实例路径。

身份、协议或端到端加密失败不会降级为明文直连。不要把 Relay URL 改成公开的 Coworker
端口来规避错误。

### Codex 或 Claude 不可用

- 确认对应 CLI 已安装并能在普通终端运行；
- 确认已经完成登录；
- 仅在自动发现失败时填写绝对命令路径；
- 修改命令后保存配置并重新运行诊断；
- 一个 actor 失败不会阻止 Local 或另一个健康 actor。

### 会话无法继续

- 锁图标表示只读历史；
- 原生 App/CLI 会话要先由相应 app-server 验证才能恢复；
- 已删除或过期的原生会话 ID 会在写入前被拒绝；
- actor 活跃 turn 中可以追加输入，但不能在同一消息中切换模式；
- Bridge 停止时不能新建或写入会话。

### 消息没有发送给 Coworker

普通 Codex/Claude `final` 只留在本机会话。需要显式使用“发送给 Coworker”或
`send_to_coworker`。确认：

- 选择了正确 Coworker；
- Bridge 正在运行；
- 目标仍是已知 participant；
- 附件路径仍可读取；
- 日志中没有 outbox/ACK 错误。

### 更新失败

- 确认更新 URL 与当前 Coworker/Relay 实例一致；
- 检查客户端版本与目标架构；
- 签名缺失或与内置公钥不匹配时必须拒绝安装；
- Relay 临时更新适配器只允许当前实例固定路径，不接受任意 URL 或跨实例重定向。

更新失败不影响继续使用当前已安装版本。保留日志后再联系发布维护者。

## Relay 连接失败

先判断故障位于：

`Desktop → Relay`、`Coworker → Relay`，还是 Relay 自身。

- 在 Coworker 管理后台“远程访问”运行连接测试；
- 检查 Relay 服务健康、DNS、证书、系统时间和实例状态；
- 检查 token 是否已轮换或实例是否被撤销；
- 检查来源 IP 是否因连续失败进入封禁；
- 不要把 Relay 日志中的连接元数据与“能够解密消息”混为一谈。

部署命令、配对、封禁、备份和恢复见[自托管 Relay](relay.md)。协议或证书身份失败不会
自动降级；这是安全边界的一部分。

## Docker 与 offline 镜像

- 代码、状态和模型缓存可能在不同卷中，先确认实际挂载；
- 严格离线镜像不会在运行时从 Hugging Face 或 Git 远端补齐缺失内容；
- 预置 embedding 模型必须与运行时配置一致；
- 修改 `pyproject.toml` 或 `uv.lock` 后要重新构建依赖环境；
- 挂载 checkout 模式下，宿主机与 Agent 看到同一个 Git 工作区。

检查数据范围：

```bash
uv run python scripts/cleanup.py status
```

需要备份后清理运行数据时：

```bash
uv run python scripts/cleanup.py backup-delete
```

`backup-delete` 只处理 `data/` 范围，仍保留 `data/_backups/`，也不会删除 `.env`、
`providers.json`、`.coworker/`、Desktop 数据或 Docker 卷。执行前阅读
[数据与信任边界](../architecture/data-boundaries.md#查看备份与清理)。

## 收集可共享的诊断信息

建议提供：

- Coworker/Desktop/Relay 版本；
- 操作系统、CPU 架构和运行方式；
- 问题发生时间与时区；
- 最小复现步骤；
- 第一个相关错误及前后少量日志；
- 是否只影响一个 actor、Channel 或实例；
- 最近一次成功操作；
- 已尝试的恢复动作及结果。

分享前移除：

- Authorization Header、token、API Key 和私钥；
- 完整配置导出包；
- 用户消息、附件和文件内容；
- Relay 配对材料和微信二维码；
- 不相关的个人路径与身份信息。

安全漏洞或可能泄露凭据的问题请按[安全策略](../../SECURITY.md)私下报告，不要提交公开
issue。

[← 返回项目首页](../../README.md)
