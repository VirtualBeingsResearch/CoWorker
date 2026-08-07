# 升级与迁移

中文 · [English](upgrading.en.md)

[← 返回配置与运维](README.md)

本页适用于已经保存身份、记忆、任务或客户端连接的实例。升级前不要只更新代码：先记录
版本、备份状态，并检查本次版本是否包含数据格式或协议变化。

## 升级前检查

1. 阅读 [CHANGELOG](../../CHANGELOG.md)，重点检查迁移、兼容性和已知限制。
2. 在管理后台确认没有持续失败的任务、正在进行的记忆回溯或关键工具调用。
3. 记录 Coworker、Desktop 和 Relay 版本以及当前运行方式。
4. 按[备份与恢复](backup-and-restore.md)备份 `data/`、`.coworker/`、配置和外部组件。
5. 保留当前可启动的提交或镜像标签，不要用浮动标签作为唯一回滚点。

> [!WARNING]
> 不要在工作区存在未确认修改时强制同步、重置或覆盖代码。Coworker 自己也可能在当前
> checkout 中留下分支和提交。

## 让搭档升级自己

对于源码 checkout，推荐先让 Coworker 自己检查并执行升级。她可以使用文件、代码和命令
工具检查工作区与远端，审阅上游变更，处理能够明确判断的冲突，运行相关检查，并在代码
更新完成后单独调用 `restart_self`。

可以这样提出任务：

> 检查当前仓库和运行状态，备份升级会影响的数据，然后安全地把上游更新集成到当前分支。
> 保留本地提交和修改，审阅迁移与依赖变化，运行相关检查；任何需要丢弃、覆盖或无法明确
> 解决的冲突先询问我。全部通过后单独调用 `restart_self` 完成升级，并在恢复后汇报版本和
> 验证结果。

`restart_self` 的保护流程是：

1. 在当前 Python 环境运行 `python -m coworker --check`，最长等待 30 秒；
2. 校验失败或超时时返回错误，保持当前进程运行；
3. 校验通过后保存包含悬空工具调用的短期记忆快照；
4. 请求主循环退出，并由启动器原地替换进程；Windows 由父进程重新拉起 worker；
5. 新进程恢复短期记忆和闹钟，把悬空调用补成真实的重启成功结果。

该工具必须由主线单独调用，Bubble 不能触发它。它只验证“新代码能够加载配置并完成
Provider 注册”，不能证明所有集成测试、数据迁移或外部服务都正常，因此升级任务仍应先
完成备份、差异审阅和相关测试。

如果 Coworker 由不支持进程自替换的外部包装器运行，或本次升级改变了容器镜像、系统依赖、
启动命令或 Python 环境，仍需由宿主机或编排系统完成外层升级；`restart_self` 只能重启当前
运行环境。

## 人工升级源码 checkout

先停止 Coworker，再检查当前分支和远端：

```bash
git status --short
git branch --show-current
git remote -v
```

确认要集成的上游后，使用普通 Git 工作流获取并审阅更新。依赖锁发生变化时运行：

```bash
uv sync --frozen
uv run playwright install chromium
uv run coworker --check
```

`--check` 只验证启动环境，不进入持续 Agent 循环。验证通过后再正常启动
`uv run coworker`，并检查 `/status` 和管理后台诊断。

## Docker Compose 升级

Compose 默认用当前 checkout 作为工作区，用发布镜像提供执行环境。停止写入并保存备份后，
先把 `COWORKER_IMAGE` 固定到准备验证的版本标签或 digest，再执行：

```bash
docker compose stop
docker compose pull
docker compose up --no-build -d
docker compose ps
```

如果 checkout 包含尚未进入所用发布镜像的 `pyproject.toml`、`uv.lock` 或系统依赖修改，
需要执行 `docker compose build` 和 `docker compose up -d`，而不是继续复用旧执行环境。
`coworker-state` 和 `coworker-models` 是独立卷；更新或重建容器不会自动迁移、备份或
删除它们。

旧版本的 Compose 默认使用 `coworker-workspace` 命名卷。首次升级到以当前 checkout 为
默认工作区的版本时，该旧卷不会被删除，但会被新的 bind mount 遮住。启动前先通过
[备份与恢复](backup-and-restore.md)确认实际卷名，并备份其中的分支、提交和修改。若要暂时
继续使用旧工作区，在 `.env` 中设置 `COWORKER_WORKSPACE_SOURCE=coworker-workspace`；
确认内容已经安全迁移后，再移除此覆盖项并切换到当前 checkout。

## 数据与记忆迁移

- 不要假设降级版本能够读取新版本写入的数据。
- 记忆树升级后默认只从新的压缩事件继续增长；若需要把历史日志回填为多分辨率树，
  在管理后台运行回溯，或使用 `POST /backfill_tree`。回溯会产生模型调用。
- 更换 embedding 模型会改变长期记忆向量空间。已有记忆时不要直接切换，除非版本说明
  提供了明确的重建流程。
- Identity、Skill、Palace、潜意识模式、任务和历史内容不会因切换运行时语言而自动翻译。

记忆内部机制和特定旧格式说明见[核心概念与能力](../architecture/concepts.md)。

## 组件兼容

- Relay v1 的 Go Relay、Python Coworker 和 Rust Desktop 使用协议版本 `1`。协议或密钥
  派生变化时应协同升级，详见 [Relay v1 协议](relay-protocol.md)。
- Desktop 与 Coworker 连接会协商协议版本；旧客户端可能继续本地工作，但无法连接不兼容
  的 Coworker 或 Relay。
- Desktop 自动更新失败不会删除当前安装版本。签名或平台资产不完整时必须保持旧版本。

## 升级后验证

- `/status` 返回运行状态、预期 Provider 和模型；
- 管理后台没有持续增长的失败任务；
- 发送一条测试消息并确认回复路径；
- 检查长期记忆、任务、闹钟和最近交互仍可读取；
- 使用 Desktop 或 Relay 时分别验证本地和远程连接；
- 记录升级时间、目标版本、备份和验证结果。

## 回滚

代码回滚和数据恢复是两件事。只有确认旧版本兼容当前数据时，才能只切回旧提交或镜像。
若新版本已经写入不兼容数据，应停止服务，保留故障现场，再恢复升级前的完整备份。不要
通过删除 `data/` 或 Docker 卷来“试试看”。

[← 返回项目首页](../../README.md)
