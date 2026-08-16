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

## 将工作区关联到自己的仓库

无论是源码、Compose 还是直接 Docker 运行，Coworker 使用的工作区都是完整 Git
仓库。直接 Docker 的 `/app` 及其 remote 配置保存在工作区卷中，不需要先进入
容器手动配置。想用自己的仓库管理后续修改时，推荐将它设为 `origin`，将
Coworker 官方仓库或其他上游设为 `upstream`。如果你只跟踪一个仓库，保留
`origin` 即可。

可以直接把仓库地址和目标分支告诉 Coworker：

> 检查当前工作区、分支和 remote。把 `<我的仓库 URL>` 配置为 `origin`；如果现有
> `origin` 指向 Coworker 官方仓库，将它保留为 `upstream`。获取两边的更新，保留
> 所有本地提交和修改，把 `upstream/main` 安全集成到当前分支，运行相关检查，
> 然后把当前分支推送到 `origin`。任何需要强制推送、丢弃、覆盖或无法明确解决的
> 冲突先询问我。

手动检查时，先确认工作区和现有远端，再按实际仓库调整：

```bash
git status --short
git branch --show-current
git remote -v
git remote add upstream <上游仓库 URL>
git fetch upstream
git merge upstream/main
```

如果默认分支不是 `main`，请替换为实际分支名；已有同名 remote 时不要再执行
`git remote add`。仓库 URL 不应包含 Token、密码或私钥。公开仓库拉取无需额外
凭据；私有仓库或推送操作需要事先在容器或运行账户中配置专用、最小权限的
Git 凭据，不要把凭据发送到聊天中。

需要定期同步时，在指令中写明频率、要维护的本地分支、上游分支和是否推送，
避免任务触发时误用另一个当前分支。

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

## 直接 Docker 升级

直接 `docker run` 会为 `/app`、`/var/lib/coworker` 和 `/opt/huggingface`
创建挂载。升级前先按[备份与恢复](backup-and-restore.md#直接运行-docker-镜像)
记录卷名并备份工作区和状态。然后保留旧容器，让新容器复用其挂载：

```bash
docker stop coworker
docker rename coworker coworker-before-upgrade
docker pull ghcr.io/virtualbeingsresearch/coworker:offline
docker run --name coworker \
  --volumes-from coworker-before-upgrade \
  -p 127.0.0.1:8000:8000 \
  -e API__HOST=0.0.0.0 \
  ghcr.io/virtualbeingsresearch/coworker:offline
```

工作区处于镜像托管的默认分支、没有本地改动且可快进时，新镜像会从内置
Git bundle 快进它；本地修改、提交、其他分支和分叉历史都会保留。新容器未验证前
不要删除 `coworker-before-upgrade` 或备份；但两个容器共用同一状态卷，因此回退旧镜像前
仍要先确认数据格式兼容，必要时恢复升级前备份。

### 从直接 Docker 迁移到 Compose

迁移前先用备份文档中的命令生成 `workspace.tgz` 和 `state.tgz`。将工作区
解压到新的宿主机目录，保留旧容器，再创建 Compose 容器并恢复状态：

```bash
mkdir coworker-compose
tar -xzf /absolute/path/to/coworker-backup/workspace.tgz -C coworker-compose
docker stop coworker
docker rename coworker coworker-direct-backup
cd coworker-compose
docker compose pull
docker compose create --no-build
docker run --rm \
  --volumes-from coworker \
  --mount type=bind,src=/absolute/path/to/coworker-backup,dst=/backup,readonly \
  --entrypoint sh \
  ghcr.io/virtualbeingsresearch/coworker:offline \
  -ec 'tar -C /var/lib/coworker -xzf /backup/state.tgz'
docker compose up --no-build -d
docker compose ps
```

`workspace.tgz` 中已包含 Git 历史、`.coworker/` 和工作区配置，`state.tgz` 则恢复到
Compose 的独立状态卷。确认身份、记忆、任务和消息都正常后，再决定是否保留
`coworker-direct-backup` 和密文备份。自定义模型缓存如果不能从镜像重建，还需单独备份
`/opt/huggingface`。

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

### 迁移 checkout 中现有的 `data/`

Compose 入口脚本只会将不存在或空的 `/app/data` 替换为状态卷链接。如果你曾在
同一 checkout 中通过 `uv run coworker` 运行，`data/` 可能已非空；此时启动会拒绝覆盖，
而不是静默丢失数据。

停止源码进程，将原目录移到 checkout 之外的受保护位置，再在 Compose 创建的
状态卷中恢复内容：

```bash
mv data ../coworker-data-before-compose
docker compose pull
docker compose create --no-build
docker run --rm \
  --volumes-from coworker \
  --mount type=bind,src="$PWD/../coworker-data-before-compose",dst=/backup,readonly \
  --entrypoint sh \
  ghcr.io/virtualbeingsresearch/coworker:offline \
  -ec 'cp -a /backup/. /var/lib/coworker/'
docker compose up --no-build -d
```

迁移目录包含管理员令牌、模型密钥、对话和附件，应使用只有运行账户可读的位置。
验证新容器完整后再处理 `../coworker-data-before-compose`；不要在验证前删除唯一副本。

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

- 携带通信 Bearer 的 `/status` 返回运行状态、预期 Provider 和模型；
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
