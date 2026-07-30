# Coworker Desktop 使用指南

中文 · [English](desktop.en.md)

[← 返回通信与客户端](README.md)

Coworker Desktop 是本机协作工作台。它把本机用户、Codex、Claude Code 与一个或多个
Coworker 放在同一界面中，同时保持身份、项目和会话边界。Desktop 自带 Bridge，但不内置
Coworker Python 服务、Codex CLI 或 Claude Code CLI。

![Coworker Desktop 对话工作台，展示本机用户、Codex、Claude Code 与 Coworker](../assets/screenshots/desktop-conversations-zh.png)

<p align="center"><sub>状态、设置、会话和日志集中在同一个本机应用中。</sub></p>

## 开始之前

至少准备：

- 一台正在运行且已经完成首次初始化的 Coworker；
- Coworker 地址；
- 一个可用于 Desktop 通信的 Bearer token；
- 与操作系统和 CPU 架构匹配的 Desktop 安装包。

Codex 和 Claude Code 都是可选 actor。缺少其中一个不会阻止本机聊天或其他可用 actor
启动。若要使用对应会话，应先安装并登录其 CLI。

| 连接场景 | Coworker 地址 | 要求 |
|---|---|---|
| 同机本地调试 | `http://127.0.0.1:8000` | Coworker 与 Desktop 都显式开启 development mode |
| 可信网络直连 | `https://coworker.example.com` | HTTPS、强 Bearer token 和额外网络访问控制 |
| 公网远程使用 | Relay 提供的实例地址 | 推荐方式；Desktop 会识别 Relay 并使用端到端加密 |

不要为了让 Desktop 连通而直接把 Coworker 的 `8000` 端口暴露到公网。远程连接请先按
[自托管 Relay](../operations/relay.md)完成部署和配对。

## 安装

从项目的
[GitHub Releases](https://github.com/VirtualBeingsResearch/CoWorker/releases)
下载最新已发布版本：

| 平台 | 安装包 |
|---|---|
| Windows | `.exe` NSIS installer |
| macOS Apple Silicon | 标有 `aarch64` / Apple Silicon 的 `.dmg` |
| macOS Intel | 标有 `x86_64` / Intel 的 `.dmg` |
| Linux | `.AppImage` 或 `.deb` |

优先使用项目正式 Release 中的安装包，并在需要时用同一 Release 的
`SHA256SUMS.txt` 校验下载内容。macOS 构建若未签名或未公证，系统可能显示额外安全警告；
不要绕过来源不明安装包的系统保护。

首次安装后打开 CoWorker Desktop。配置和日志保存在操作系统应用数据目录，不会因为删除
安装包而自动清除。升级前无需删除旧版本；应用检测到已发布且签名有效的更新时，会先征求
确认再安装。

## 首次配置

应用第一次打开会显示配置向导。建议按以下顺序完成：

1. **确认本机身份**
   - `Codex ID` 用于区分这台 Desktop 上的 Codex actor；
   - 显示名称用于在 Coworker 中识别这台设备；
   - Codex/Claude 命令通常保留自动发现结果，只有安装在非标准位置时才手动修改。
2. **连接 Coworker**
   - 填写稳定且唯一的 `coworker_id` 与易读显示名称；
   - 填写 HTTPS 直连地址或 Relay 实例地址；
   - 填写 Desktop 通信 token。
3. **选择工作目录**
   - 本机聊天工作区目录用于保存 Local actor 的会话资产；
   - 新建 Codex 会话时仍可单独选择具体项目目录。
4. **选择权限边界**
   - 初次使用建议保留 `read-only`；
   - 确认配置无误后再按任务需要调整。
5. **保存并启动**
   - 保存后点击启动 Bridge；
   - 返回“状态”页运行诊断，确认 Coworker 和需要的 actor 均可用。

服务端没有单独配置 `API__COMMUNICATION_TOKEN` 时，Desktop 可以暂时使用管理员令牌。
需要隔离通信权限与管理权限时，应在 Coworker 设置独立通信令牌，再更新 Desktop。

## 认识工作台

左侧 Coworker 列表决定当前查看或发送的目标实例。主导航包含：

- **状态**：查看 `actor → Bridge → Coworker` 路径、当前配置来源和诊断结果。
- **设置**：管理身份、启动行为、actor 命令、多个 Coworker、通信令牌、权限与更新地址。
- **会话**：在 Local、Codex 和 Claude Code 之间切换，新建或继续会话。
- **日志**：按级别查看 Bridge 运行记录；排障前可以临时提高日志级别。

启动按钮只启动 Bridge，不会替你启动远端 Coworker 服务。配置有未保存修改时，先保存再
启动或运行诊断。

### actor 与会话边界

| actor | 适合场景 | 注意事项 |
|---|---|---|
| Local | 不依赖外部编码 CLI 的本机聊天 | 会话由 Desktop 管理 |
| Codex | 项目开发、代码检查和工具任务 | 需要可用的 Codex CLI；已有 App/CLI 历史可能只读 |
| Claude Code | 使用 Claude Code 的项目会话 | 需要可用且已登录的 Claude Code CLI |

每个 actor 都有自己的会话历史。同一个 `conversation_id` 只在对应 actor 中解释，不应把
一个 actor 的会话 ID 当成另一个 actor 的会话。

### 新建与继续会话

1. 选择 actor。
2. 点击“新建会话”。
3. 对 Codex/Claude 会话选择项目目录；不选择时会创建无项目会话。
4. 选择可用模式，然后发送第一条消息。
5. 需要把结果交给 Coworker 时，使用界面的“发送给 Coworker”操作并确认目标实例。

普通 AI `final` 只保留在本机会话中，不会自动通知 Coworker。这个边界用于避免把草稿、
警告或中间结果意外发送给其他实例。

会话列表中的锁图标表示只读历史。Bridge 可以展示这类历史，但只有被当前 Bridge 拥有或
经原生 actor 验证可恢复的会话才能继续写入。双击可写会话标题可以重命名。

### 消息和附件

- 输入框支持 Markdown；界面会本地渲染常用 Markdown、表格、代码和数学内容。
- 可以复制或引用已有消息。
- 附件从本机选择后随当前消息发送；图片附件可在本地预览。
- 从不受信任来源取得的消息、附件和工具输出都可能包含提示注入，应先检查再授权高风险操作。

## 权限与审批

Desktop 把“可以访问什么”和“由谁审批”分开配置：

| 权限模式 | `approvals_reviewer=none` | `approvals_reviewer=coworker` |
|---|---|---|
| `read-only` | 需要提升权限的请求立即拒绝 | 发送给 Coworker 等待明确审批 |
| `workspace-write` | 需要额外审批的请求立即拒绝 | 发送给 Coworker 等待明确审批 |
| `danger-full-access` | 绕过审批并直接允许 | 不建议组合使用 |

推荐从 `read-only` 开始，只在可信项目中使用 `workspace-write`。`danger-full-access`
会绕过重要保护，不应作为“让报错消失”的排障手段。Coworker 代审超时会 fail closed。

## 启动、托盘与更新

“登录系统时启动 CoWorker”和“打开 CoWorker 时启动 Bridge”是两个独立开关。需要开机后
后台自动连接时同时开启。自动登录启动会把主窗口留在托盘；关闭按钮的行为可选择隐藏到
托盘或退出应用。

Desktop 启动时以及 Coworker 发布更新通知时会检查签名更新。检查到新版本后仍需用户确认
安装；Bridge 会在安装前停止，完成后应用重启。Relay 连接下，更新清单和制品仍通过同一条
端到端加密路径传输，客户端还会独立验证 updater 签名。

## 出现问题时

先在“状态”页运行诊断，再到“日志”页查看同一时间段的 ERROR/WARN。常见问题和恢复步骤见
[故障排查](../operations/troubleshooting.md)。需要开发运行、Bridge 配置 schema、协议行为、
构建、签名或发布说明时，转到
[Desktop 开发与发布](../development/desktop.md)。

[← 返回项目首页](../../README.md)
