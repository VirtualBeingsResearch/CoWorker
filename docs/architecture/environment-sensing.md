# 环境感知（Environment Sensing）

中文 · [English](environment-sensing.en.md)

[← 返回架构索引](README.md)

## 概述

环境感知让 Agent 拥有**感知器官**——被动接收外部世界的变化信号，主动查询自身
运行环境，并通过脚本定义新的感知来源。

环境信息源是**只进不出的 Channel**：它们把外部信号推送进 Agent 收件箱，但 Agent
不能"回复"它们（`send()` 返回不支持错误）。这复用了成熟的 Channel 基础设施
（access control、traffic 记录、routing、system prompt 注入、生命周期管理）。

## 三种信息流向

### 1. 被动推送（环境源轮询）

环境源按各自声明的触发条件定时执行 `poll()`，产出信号后通过标准
`publish_inbound()` 路径推送到 Agent 收件箱：

```
.coworker/environment/<name>/source.py  poll(ctx) → ctx.emit_signal(...)
    ↓ EnvironmentRuntime 调度
EnvironmentChannel.publish_inbound(IncomingEvent)
    ↓ access control + traffic 记录
InboxWatcher.push(wake=True)
    ↓ Agent 被唤醒，看到 [环境信号 · <源>] 消息
```

### 2. 主动查询（工具）

Agent 随时可调用：
- `get_system_status` — CPU/内存/磁盘/进程数实时快照
- `get_runtime_context` — 容器检测/云环境/主机信息
- `manage_environment` — 列出/启停/重载/立即触发环境源

### 3. 系统提示注入

活跃环境源列表自动注入 `[CHANNELS]` 系统提示段，让 Agent 知道它感知到什么。

## 创建环境源

在 `.coworker/environment/<名称>/` 下创建 `SOURCE.md` + `source.py`：

```
.coworker/environment/my-source/
├── SOURCE.md     # frontmatter 元数据
└── source.py     # async def poll(ctx): ...
```

### SOURCE.md frontmatter

```yaml
---
name: my-source           # 源名称（也是 participant_id 后缀）
description: 我的自定义源
mode: inline              # inline（进程内）或 subprocess（子进程隔离）
language: python          # 脚本语言
script: source.py         # 入口文件名
enabled: true
protected: false          # 受保护源不可删除（仅可禁用）

# 调度触发（可组合，任一满足即触发）
schedule_trigger: periodic  # periodic | cold_floor | manual
every_seconds: 300          # 每 300 秒
# every_n_cycles: 10        # 每 10 个 agent 周期
# every_n_tool_calls: 50    # 每 50 次工具调用
# cold_floor_seconds: 60    # 启动后 60 秒一次
# cron: "0 * * * *"          # cron 表达式
# min_interval_seconds: 60   # 最小间隔保护

timeout_seconds: 60        # 单次 poll 超时
params:                    # 传给脚本的自定义参数
  url: https://example.com/api
  token: ${MY_TOKEN}
---
```

### source.py（inline 模式）

```python
async def poll(ctx):
    """框架会在每次触发时调用此函数。"""
    # ctx.config — SOURCE.md 中的 params 字典
    # ctx.http — 共享的 httpx.AsyncClient
    # ctx.logger — 带源标签的 loguru logger

    resp = await ctx.http.get(ctx.config["url"])
    for item in resp.json()["items"]:
        # emit_signal 会自动去重（fingerprint 相同的只推送一次）
        ctx.emit_signal(
            title=item["title"],
            content=item["body"],
            fingerprint=f"item:{item['id']}",  # 稳定的去重键
            url=item.get("url"),
        )

    # 保存增量游标供下次使用
    ctx.set_cursor(resp.headers.get("etag"))
```

### ctx API

| 方法 | 说明 |
|---|---|
| `ctx.config` | params 字典（只读） |
| `ctx.http` | 共享 httpx.AsyncClient |
| `ctx.logger` | loguru logger（带源标签） |
| `ctx.emit_signal(title, content, fingerprint, url?, severity?)` | 产出信号，返回是否接受（去重后） |
| `ctx.get_cursor()` | 获取上次保存的游标 |
| `ctx.set_cursor(cursor)` | 保存游标 |
| `ctx.is_known(fingerprint)` | 检查某指纹是否已推送过 |

## 执行模式

### inline（推荐，默认）

Python 脚本在主进程中执行，`ctx` 直接注入命名空间。最简单、最高效，源代码
`async def poll(ctx): ctx.emit_signal(...)` 即可工作。

### subprocess

脚本在独立子进程中运行，通过 stdin/stdout JSON-RPC 协议与宿主通信。任何能
读写 stdin/stdout 的语言都可用。适合需要隔离或非 Python 的场景。

## 调度模型

每个源声明自己的触发条件，照搬潜意识模式的范式：

| 字段 | 语义 |
|---|---|
| `every_seconds` | 每隔 N 秒触发 |
| `interval_seconds` | 同 every_seconds（别名） |
| `every_n_cycles` | 每 N 个 agent cycle 触发 |
| `every_n_tool_calls` | 每 N 次工具调用触发 |
| `cold_floor_seconds` | 启动后 N 秒触发一次 |
| `cron` | 标准 5 字段 cron 表达式 |
| `min_interval_seconds` | 最小间隔保护（防止过频） |
| `schedule_trigger: manual` | 从不自动触发（仅 manage_environment run_now） |

多个触发条件可组合——**任一满足即触发**。

## Agent 编辑环境

Agent 可以：
- 用 `write_file` 创建新的 `SOURCE.md` + `source.py`
- 用 `manage_environment(action="reload")` 重新扫描目录发现新源
- 用 `manage_environment(action="enable/disable")` 启停源
- 用 `manage_environment(action="run_now")` 立即触发某源
- 用 `get_system_status` 查询自身资源状态

这让 Agent 能像搭档扩展 `autodl_sdk` 那样，正式化地扩展自己的感知能力。

## 配置

环境感知通过 `ENVIRONMENT__` 前缀的环境变量配置：

```bash
ENVIRONMENT__ENABLED=true
ENVIRONMENT__SOURCES_DIR=.coworker/environment
ENVIRONMENT__STATE_PATH=data/environment/state.json
ENVIRONMENT__DEFAULT_TIMEOUT_SECONDS=60
ENVIRONMENT__MAX_CONCURRENT_POLLS=5
```

## 内置源

- **github-issues** — 跟踪 GitHub 仓库的 issue 和评论
- **tech-rss** — 订阅 RSS/Atom feed

这些源随项目分发，放在 `.coworker/environment/` 下。修改它们的 `params` 即可自定义。
