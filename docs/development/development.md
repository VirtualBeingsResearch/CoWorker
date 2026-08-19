# 开发指南

中文 · [English](development.en.md)

[← 返回开发与协作](README.md)

提交代码前请阅读 [贡献指南](../../CONTRIBUTING.md)；安全问题请按
[安全策略](../../SECURITY.md) 私下报告。

```bash
# 安装开发依赖
uv sync --dev
# 使用默认配置的 mem0 记忆后端时，需额外安装其可选依赖：uv sync --dev --extra mem0

# 安装 browser 工具使用的 Chromium（只需一次）
uv run playwright install chromium

# 代码检查
uv run ruff check src tests

# 类型检查
uv run mypy src

# 单元测试
uv run pytest
```

Web 前端需要 Node.js 22.12+（Vite 要求）；桌面端测试因 jsdom 需要 Node.js ^24.15 或
≥26。仓库 CI 与 Dev Container 统一使用 Node.js 24。管理界面的构建结果写入
`src/coworker/web/`，它是随 Python 包发布的静态资源：

```bash
npm ci --prefix web
npm --prefix web run build
git status --short -- src/coworker/web
```

Debian/Ubuntu 如果缺少浏览器系统库，使用
`uv run playwright install --with-deps chromium`。

### Dev Container

在 Intel macOS 上，当前 PyTorch 版本不再提供 `macosx_x86_64` wheel。仓库内的
[`.devcontainer`](../../.devcontainer/devcontainer.json) 配置会把开发环境运行在
Linux 容器中，因此 Intel Mac 会使用 PyTorch 的 `linux/x86_64` CPU wheel；Apple
Silicon 则使用原生 `linux/arm64`，不需要强制模拟 x86。

先安装 Docker Desktop（或兼容的容器运行时）以及 VS Code 的 Dev Containers
扩展，然后在仓库目录执行 **Dev Containers: Reopen in Container**。首次构建会：

- 安装 Python 3.14、uv、Node.js 24 和 FFmpeg；
- 通过锁文件安装 Python 开发依赖和 Linux CPU 版 PyTorch；
- 安装 Playwright Chromium 及其 Linux 系统库；
- Dev Container 默认不配置端口转发；需要从宿主机浏览器访问时，在 VS Code 的 Ports 视图手动转发 `8000`（CoWorker API），VS Code 检测到容器内监听端口时也可能自动转发。

源码仍由宿主机目录挂载，容器内的 Python 环境位于 `/opt/venv`。容器创建完成后，
可直接运行本文中的 `uv run ...`、`npm ...` 和测试命令。依赖或锁文件发生变化后，
执行 **Dev Containers: Rebuild Container** 以刷新缓存层。

Dev Container 是 Linux 环境，适合 Python 和 Web 开发，但不能生成或
验证 macOS 专属的 Tauri `.app`/`.dmg`、签名和公证；这些步骤仍需在 macOS 本机或
对应的 CI runner 上完成。

### 使用 offline 镜像开发

如果只需要运行和调试 Coworker 服务，可以复用已发布的严格离线镜像，将当前 checkout
直接挂载到容器的 `/app`：

```bash
COWORKER_IMAGE=ghcr.io/virtualbeingsresearch/coworker:offline \
docker compose up --pull always --no-build
```

`/app` 同时是 Python 实际加载的源码目录和 Agent 工作区，因此本机、Agent 与运行进程
看到的是同一份 Git checkout。镜像提供 Linux Python 环境、Chromium、FFmpeg、预置
embedding 模型，以及 `vim-tiny`、`nano`、`less`、`jq` 和 `ripgrep` 等轻量命令行工具；
源码修改后重启容器即可。若修改了 `pyproject.toml` 或 `uv.lock`，使用
`docker compose up --build` 重新构建依赖环境。

清理运行时缓存和数据可参考：

```bash
uv run python scripts/cleanup.py
```

[← 返回项目首页](../../README.md)
