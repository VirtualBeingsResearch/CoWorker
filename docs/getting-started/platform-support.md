# 平台支持与组件兼容

中文 · [English](platform-support.en.md)

[← 返回首次运行](README.md)

本页说明当前源码和构建流程覆盖的平台，不代表每个平台都提供官方预编译安装包。版本号以
仓库的 `VERSION`、manifest 和发布说明为准。

## 运行要求

| 组件 | 最低开发要求 | 平台说明 |
|---|---|---|
| Coworker Python 服务 | Python 3.13+、uv | macOS Apple Silicon、Windows、Linux；Intel macOS 使用 Dev Container 或 Docker |
| browser 工具 | Playwright Chromium | Debian/Ubuntu 可能需要 `--with-deps` 安装系统库 |
| Coworker Desktop | Node.js ^24.15 或 ≥26、稳定版 Rust（构建） | Windows NSIS、macOS dmg、Linux AppImage/deb 构建目标 |
| Explore Lab | Python workspace、Node.js | 本机开发工具，默认只监听 `127.0.0.1:8100` |
| Relay | Go 1.26.6+（构建）或 Docker | 当前 v1 为单节点，不共享 bbolt 卷 |

Desktop 不包含 Python 服务、Codex CLI 或 Claude Code CLI；它们分别进行健康检查。缺少
Codex 或 Claude 不会阻止本地用户和其他可用 actor 工作。

## CPU 与模型

仓库默认在所有平台使用 PyTorch CPU 索引。Windows/Linux 的 NVIDIA CUDA 13.0 需要按
`pyproject.toml` 注释切换 `torch` source，再重新生成锁和同步依赖。不要把不同平台生成的
虚拟环境或本地 wheel 缓存直接复制到另一架构。

Docker offline 镜像预置 embedding 模型；配置的 embedding 模型必须与缓存一致。对话模型
通常来自外部 Provider，不因镜像“offline”而自动离线。

## Desktop 制品

| 系统 | 常用安装制品 | 自动更新制品 |
|---|---|---|
| Windows | NSIS installer | Tauri updater + 签名 |
| macOS Apple Silicon | `.dmg` | `.app.tar.gz` + `.sig` |
| macOS Intel | x64 `.dmg` | x64 `.app.tar.gz` + `.sig` |
| Linux | AppImage / deb | 对应平台 updater + 签名 |

各平台通常要在对应 runner 构建。签名缺失或不匹配时 Desktop 必须拒绝更新并保留当前版本。

## 协议兼容

- Desktop 注册协议和消息信封当前使用版本 `1`；
- Relay v1 需要 Coworker、Desktop 和 Relay 使用兼容协议；
- API v0.x 允许响应增加字段，客户端应忽略未知字段；
- `/api/admin/*` 属于同版本 Web 管理端实现契约，不作为独立稳定 SDK；
- 旧版本默认不持续获得安全修复，见[安全策略](../../SECURITY.md)。

升级组合前阅读[升级与迁移](../operations/upgrading.md)和发布说明。

[← 返回项目首页](../../README.md)
