# 开发指南

中文 · [English](development.en.md)

[← 返回开发与协作](README.md)

提交代码前请阅读 [贡献指南](../../CONTRIBUTING.md)；安全问题请按
[安全策略](../../SECURITY.md) 私下报告。

```bash
# 安装开发依赖
uv sync --dev

# 安装 browser 工具使用的 Chromium（只需一次）
uv run playwright install chromium

# 代码检查
uv run ruff check src tests

# 类型检查
uv run mypy src

# 单元测试
uv run pytest
```

修改管理界面时还需要 Node.js 20+。构建结果写入 `src/coworker/web/`，它是随
Python 包发布的静态资源：

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

- 安装 Python 3.13、uv、Node.js 24 和 FFmpeg；
- 通过锁文件安装 Python 开发依赖和 Linux CPU 版 PyTorch；
- 安装 Playwright Chromium 及其 Linux 系统库；
- 转发 CoWorker API 的 `8000` 端口和 Explore Lab 的 `8100` 端口。

源码仍由宿主机目录挂载，容器内的 Python 环境位于 `/opt/venv`。容器创建完成后，
可直接运行本文中的 `uv run ...`、`npm ...` 和测试命令。依赖或锁文件发生变化后，
执行 **Dev Containers: Rebuild Container** 以刷新缓存层。

Dev Container 是 Linux 环境，适合 Python、Web 和 Explore Lab 开发，但不能生成或
验证 macOS 专属的 Tauri `.app`/`.dmg`、签名和公证；这些步骤仍需在 macOS 本机或
对应的 CI runner 上完成。

### Explore Lab

Explore Lab 的后端可以直接托管前端构建产物，日常使用只需要启动后端。分支运行时使用模拟通信对象（默认 `explore_lab`）：`communicate` 只把出站消息记录到分支状态，不会投递到外部；`list_connections` 会将这些模拟对象显示为活跃连接。

```bash
# 1. 安装依赖并构建前端静态资源
npm ci --prefix apps/explore-lab/frontend
npm --prefix apps/explore-lab/frontend run build

# 2. 启动后端并托管 UI
uv run --project apps/explore-lab/backend python -m explore_lab
# 等价方式：uv run --project apps/explore-lab/backend explore-lab

# 3. 打开
# http://127.0.0.1:8100/
```

默认读取 `apps/explore-lab/frontend/dist`。如需使用其它构建目录：

```bash
uv run --project apps/explore-lab/backend python -m explore_lab --ui-dir path/to/dist
```

清理运行时缓存和数据可参考：

```bash
uv run python scripts/cleanup.py
```

[← 返回项目首页](../../README.md)
