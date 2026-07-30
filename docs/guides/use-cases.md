# 典型使用场景

中文 · [English](use-cases.en.md)

[← 返回 Web 管理后台](README.md)

这些场景展示如何组合现有能力，不是要求 Coworker 自动获得更多权限。关键操作仍应保留
人工复核。

## 个人长期搭档

目标：跨天延续项目背景、任务和提醒。

1. 在身份档案设定姓名和协作风格；
2. 用稳定的 participant 与她持续对话；
3. 把确认过的事实写入长期记忆，把必须持续可见的少量信息固定；
4. 用任务和闹钟承接下一步；
5. 每周审查过期任务、记忆和用量。

不要把整份项目资料全部固定到短期上下文；流程写 Skill，事实进入记忆。

## 团队项目记忆

目标：让不同成员从共享背景继续工作，同时隔离各自短期对话。

- 为每个成员使用独立 `participant_id`；
- 用 Palace 聚合项目心智模型、关键 Skill 和项目记忆标签；
- 重要决策在人工确认后再写入长期记忆；
- 用 Channel 或 Desktop 传递结果，不把对话隔离误当权限系统。

## Desktop 多 Agent 协作

目标：在一个工作台中协调 Local、Codex、Claude Code 与 Coworker。

1. 连接目标 Coworker，并检查各 actor 健康；
2. 在正确项目和对话中发起任务；
3. 让 Codex/Claude 完成有边界的工作；
4. 明确“发送给 Coworker”，由她汇总、记录或继续执行；
5. 检查 tool activity 和目标 participant，避免把结果发给错误身份。

## 搭档自升级

目标：由 Coworker 审阅并集成上游更新，然后恢复原有上下文。

- 让她先检查工作区、远端、版本和备份；
- 明确保留本地提交，冲突或覆盖操作先询问；
- 运行与改动相关的检查；
- 通过后单独调用 `restart_self`；
- 恢复后验证版本、模型、消息路径和数据。

完整边界见[升级与迁移](../operations/upgrading.md)。

## 自动化与自定义 Channel

目标：把已有服务接入持续上下文。

- 简单集成使用 `POST /messages` 和 SSE/WS；
- 稳定标识 participant 和 conversation；
- 需要独立传输语义时实现 `BaseChannel` 或 `StreamProfile`；
- 对重试、附件、鉴权、离线 outbox 和错误恢复建立明确契约；
- 不直接依赖 `/api/admin/*` 作为长期公共 API。

## 领域 Palace

目标：在专业任务出现时加载足够领域背景，而不污染持续主线。

- Palace 卡片只写心智模型、触发条件和指针；
- critical Skill 放必须执行的完整流程；
- related Skill 按需加载；
- 事实通过 `memory_tags` 召回和写回；
- 定期审查园丁产出与过期记忆。

[← 返回项目首页](../../README.md)
