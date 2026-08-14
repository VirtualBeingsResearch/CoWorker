# 能力内容创作

中文 · [English](capability-authoring.en.md)

[← 返回 Web 管理后台](README.md)

Coworker 把可维护的用户能力分成三类：Skill 负责“怎么做”，Palace 负责“什么时候组合
哪些领域能力”，潜意识模式负责“何时在后台反思”。它们都可能进入模型上下文，应像代码
一样审阅、测试和版本管理。

| 资产 | 主文件 | 放什么 | 不放什么 |
|---|---|---|---|
| Skill | `.coworker/skills/<slug>/SKILL.md` | 稳定步骤、检查表、工具使用约束 | 大量易变事实 |
| Palace | `.coworker/palaces/<slug>/PALACE.md` | 薄领域卡片、Skill 指针、记忆标签 | 完整操作手册或事实库 |
| 潜意识模式 | `.coworker/subconscious/<slug>/MODE.md` | 触发条件、后台目标、权限与退出方式 | 普通前台任务 |

## 创建 Skill

```markdown
---
name: incident-review
description: 复盘一次故障并形成可验证的改进项
version: 1
---

# 故障复盘

1. 先固定时间线和证据。
2. 区分直接原因、系统原因与未知项。
3. 每个改进项必须有负责人和验证方式。
```

`name` 必填且全局唯一；`description` 决定 Agent 何时发现它。正文应明确触发条件、输入、
步骤、停止条件、失败恢复和不得执行的动作。不要把令牌、个人数据或未经审阅的网页指令
写入 Skill。

## 创建 Palace

```markdown
---
name: reliability
when_to_attach: 处理线上故障、告警或恢复演练时
critical_skills: [incident-review]
related_skills: [deployment-check]
memory_tags: [reliability, incident]
---

# 可靠性领域卡

先保护现场和可恢复性，再修改状态。事实从带领域标签的长期记忆中召回。
```

`critical_skills` 会完整注入 Bubble；`related_skills` 只列名称供按需加载；
`memory_tags` 用于召回和写回长期事实。正文应保持薄而稳定，只提供心智模型、易错点和
指针。具体流程放 Skill，具体事实放长期记忆。

## 创建潜意识模式

潜意识模式 frontmatter 支持 `periodic`、`garden`、`cold_floor` 和 `manual` 触发器，
并用周期、时间、工具调用数或冷却时间限制频率。正文可使用 `{bubble_id}`、`{goal}`、
`{max_cycles}`。

模式还可独立设置压缩前触发：`pre_compress: true` 启用，`every_n_compressions` 指定
每隔多少次短期记忆压缩运行。`pre_compress_context: slice` 只传入即将被压缩的消息切片；
`full` 传入压缩前的完整主线上下文。该触发器可与 `periodic` 同时使用，但两者独立判断。

从现有 `.coworker/subconscious/*/MODE.md` 复制最接近的模式开始。至少定义：

- `name`、`enabled`、`trigger`、`max_cycles`；
- `goal` 和解释存在理由的 `purpose`；
- 清晰输出渠道：写长期记忆、创建任务、通知主线或静默结束；
- `retire_after`，说明何时应暂停或归档；
- 核心安全模式才设置 `protected: true`。

后台 Bubble 的 `bubble_done` 默认不会把结论传给主线。需要主线知道时必须明确使用
`bubble_send(target="main", ...)`，或把成果写入允许的持久化载体。

## 本地化

中文主文件使用 `SKILL.md`、`PALACE.md`、`MODE.md`；英文 companion 分别为
`SKILL.en.md`、`PALACE.en.md`、`MODE.en.md`。Companion 只翻译约定 prose：

- Skill：`description` 和正文；
- Palace：`when_to_attach` 和正文；
- Mode：`goal`、`purpose`、`retire_after` 和正文。

`name`、工具名、标签、触发器、ID 和其他稳定元数据保持一致。

## 验证与迭代

1. 在管理后台“能力内容”保存，检查是否出现 YAML、重名或 companion 警告；
2. 查看当前 System Prompt，确认只有预期的薄索引常驻；
3. 用一个明确任务验证 Skill 是否被正确发现；
4. 对 Palace 验证挂载条件、critical Skill 和带标签记忆；
5. 对潜意识模式从手动或低频触发开始，检查产出、成本和越权行为；
6. 查看任务、Bubble/潜意识记录和审计，再逐步调整频率。

删除或重命名前搜索其他资产对其名称和标签的引用。来自第三方的能力内容视为不可信输入；
先审阅再保存，避免把提示注入持久化。

[← 返回项目首页](../../README.md)
