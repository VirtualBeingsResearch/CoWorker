# 备份与恢复

中文 · [English](backup-and-restore.en.md)

[← 返回配置与运维](README.md)

Coworker 有三类不同的“备份”。开始前先确认你要恢复的是短期对话、`data/` 运行数据，
还是包含配置和外部组件的完整实例。

| 机制 | 覆盖范围 | 适合 |
|---|---|---|
| 管理后台应急备份 | 一份短期上下文快照 | Agent 连续错误后的摘要或完整上下文恢复 |
| `scripts/cleanup.py` | `data/` 下除 `data/_backups/` 外的文件 | 本地运行数据快照、重置与恢复 |
| 整实例灾难备份 | 工作区、`data/`、`.coworker/`、配置、客户端与 Relay 数据 | 升级、迁移设备、磁盘故障和完整恢复 |

应急备份和 cleanup 快照都不能替代整实例备份。

## 完整备份清单

停止 Coworker 后，按实际部署核对：

- Git 工作区及未推送分支、提交和未提交修改；
- `data/`：身份、记忆、任务、闹钟、日志、收发件箱和运行状态；
- `.coworker/`：Skill、Palace、潜意识模式和附属资源；
- `.env`、`providers.json` 与其他外部注入配置；
- Desktop 操作系统数据目录中的配置、凭据、对话索引和日志；
- 直接 Docker 运行产生的匿名工作区、状态和模型卷，或 Compose 的
  `coworker-workspace`、`coworker-state`、`coworker-models` 卷及对应绑定目录；
- Relay 的 bbolt 数据库、签名密钥、`.env` 和部署文件。

备份包含模型密钥、管理员令牌、Relay 私钥、对话和文件内容，应加密保存并限制访问。
不要把它提交到 Git、上传到 issue 或放进普通共享网盘。

## 备份和恢复 `data/`

查看范围：

```bash
uv run python scripts/cleanup.py status
```

创建时间戳快照：

```bash
uv run python scripts/cleanup.py backup
```

快照写入 `data/_backups/<timestamp>/`。它不包含 `.env`、`providers.json`、
`.coworker/`、Desktop 数据或 Docker 卷。

恢复前停止 Coworker，并确认目标快照：

```bash
uv run python scripts/cleanup.py restore
uv run python scripts/cleanup.py restore --from 20260428_123456
```

恢复会复制快照文件并覆盖同名当前文件，但不会删除当前目录中快照没有的额外文件。若需要
精确回到某个时点，应先另存当前状态，再在隔离目录验证恢复结果。

> [!WARNING]
> `delete` 和 `backup-delete` 会删除 `data/` 中的运行文件。不要在运行进程仍写入时执行，
> 也不要把 `--yes` 用在未检查范围的自动化中。

## 恢复短期上下文

管理后台“运行中心”的应急备份来自 Agent 连续错误时保存的短期记忆快照：

- **摘要恢复**：调用摘要模型，把恢复内容作为新输入注入，不替换当前上下文；
- **完整恢复**：用备份替换当前主线短期上下文，并修剪不完整工具调用链。

优先使用摘要恢复。完整恢复前记录当前消息数量、备份文件名和时间。两种方式都会影响后续
模型上下文，但不会恢复长期记忆数据库、Skill 或配置。

## Docker 和 Relay

### 直接运行 Docker 镜像

直接 `docker run` 默认会为工作区、运行状态和模型缓存创建匿名卷。它们会随
容器重启保留，但删除容器后就不再有易识别的关联。先停止写入、查看实际卷名，再用
一个临时容器将工作区和状态打包到宿主机：

```bash
umask 077
mkdir -p coworker-backup
docker stop coworker
docker inspect coworker \
  --format '{{range .Mounts}}{{println .Destination "->" .Name}}{{end}}'
docker run --rm \
  --volumes-from coworker \
  --mount type=bind,src="$PWD/coworker-backup",dst=/backup \
  --entrypoint sh \
  ghcr.io/virtualbeingsresearch/coworker:offline \
  -ec 'tar -C /app --exclude=./data -czf /backup/workspace.tgz .; tar -C /var/lib/coworker -czf /backup/state.tgz .'
```

`workspace.tgz` 包含 Git 历史、未提交文件、`.coworker/` 和工作区配置；
`state.tgz` 包含身份、记忆、任务、日志、附件、管理员令牌和可能的模型密钥。
打包时排除 `/app/data` 链接，避免把状态重复收进工作区归档。默认 `offline` 镜像的
模型缓存可从镜像重建；如果添加了不能重建的自定义模型，再用同样方式单独归档
`/opt/huggingface`。

备份后可以用 `docker start -a coworker` 继续使用原容器。直接 Docker 的镜像更新、卷复用和
迁移 Compose 流程见[升级与迁移](upgrading.md#直接-docker-升级)。不要在未验证备份时执行
`docker rm -v coworker`。

### Docker Compose

先用 `docker compose stop` 停止 Coworker，再通过 `docker compose config`、
`docker volume ls` 和 `docker volume inspect <name>` 解析实际挂载。完整备份必须同时覆盖
作为工作区的宿主机 checkout（或旧版 `coworker-workspace` 卷）和状态卷；模型缓存可以
重建，但备份可减少恢复时间。不要只复制容器的可写层。全新主机上先用
`docker compose create --no-build` 创建状态卷；如果主机上已有旧卷，必须先删除或清空它，再在
Coworker 启动前导入备份，最后验证工作区与状态来自同一备份时点。

### Relay

Relay 使用自己的备份命令创建一致数据库快照：

```bash
coworker-relay health
coworker-relay backup --output relay-backup.db
```

数据库快照不自动包含 Relay `.env`、签名密钥和反向代理配置，需分别保护。Relay 恢复流程
见[自托管中继](relay.md)。

## 恢复演练

至少在重大升级前验证一次：

1. 在隔离目录或临时主机恢复，不覆盖唯一生产副本；
2. 使用相同或明确兼容的 Coworker 版本；
3. 验证 `/status`、身份、记忆、任务和一条测试消息；
4. 验证 Desktop/Relay 时不要复用会造成冲突的在线实例身份；
5. 记录恢复耗时、缺失项和下一次改进。

[← 返回项目首页](../../README.md)
