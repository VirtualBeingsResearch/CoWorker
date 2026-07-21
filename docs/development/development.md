# 开发指南

中文 · [English](development.en.md)

[← 返回开发与协作](README.md)

提交代码前请阅读 [贡献指南](../../CONTRIBUTING.zh-CN.md)；安全问题请按
[安全策略](../../SECURITY.zh-CN.md) 私下报告。

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

### Explore Lab

Explore Lab 的后端可以直接托管前端构建产物，日常使用只需要启动后端：

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
