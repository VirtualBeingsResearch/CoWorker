# Persona：可选的人物子机制

中文 · [English](persona.en.md)

[← 返回架构与核心概念](README.md)

> Person 是**可选的、轻量的子机制**：它给模型一个"人"的抽象，用来识别同一真人跨渠道的多个地址、维护对这个人的画像认知、合并重复人物。它嵌入系统既有机制，由 `MEMORY__PERSONA_ENABLED=false` 整体关闭——关闭时行为与现状完全一致。

在生命哲学中，"关系"是构成持续 Coworker 的生命概念之一（见[虚拟生命理念](lifeform-philosophy.md)）。Person 承载的正是这种关系：`person_id` 是稳定锚点，跨渠道的地址绑定、搭档维护的画像都围绕它组织，并随 `data/persons.json` 与画像文件跨重启存活。

## 它做什么、不做什么

**做**：

- **跨渠道归并**：把多个 `participant_id`（可含 `conversation_id`）绑定到同一个 Person；
- **搭档维护画像**：每个 Person 一份 markdown 画像，由主模型在对话中通过 `persona` 工具整体重写（可修正、可遗忘），在活跃人物本会话首条消息前注入上下文；
- **合并重复人物**：模型或照看者可以把两个 Person 实体合并为一个。

**不做**：

- 不做多租户/账号系统（`person_id` 是将来账号系统的接缝，但不内置账号）；
- 不做 participant 的 kind 分类——"这个地址是不是人"由模型依据 `[CHANNELS]` 信道语义自行判断；
- 不改写系统提示词 guidelines、不注入通信录（人物列表经 `persona` 工具按需查询）；
- 不碰 mem0（长期记忆保持单桶现状，按人认知由"画像 + relationship 记忆的 participant 标签"承载）；
- 不引入后台任务、不自动创建人物（Person 只由 `bind` 显式建立）。

## 工作机制

### ① 记住谁是谁 —— PersonStore 与画像文件

- `data/memory/persons.json`：`Person`（`person_id`、`display_name`、`aliases[]`）。
- 每个地址是 `{participant_id, conversation_id?, channel, notes[]}`。`conversation_id` 仅在信道需要它定位具体会话/真人时记录（如微信的 session）；`wecom:single:*` 这类靠地址唯一路由的不带。`notes` 按地址累积——同一信道可以有多个备注，模型 `bind` 时可多次追加。
- `data/memory/cards/{person_id}.md`：搭档维护的画像，整体重写而非追加。

### ② 模型维护认知 —— `persona` 工具

- `persona(action="bind", participant_id, conversation_id?, person_id?/name?, note?)`：把地址绑定到已知人物（按 `person_id` 或名字匹配）或新建人物；`note` 会追加到该地址的备注列表；
- `persona(action="card", person_id, content?)`：空 `content` 读画像；传 `content` 整体重写画像。画像**何时建立、怎么写由模型自行把握**；
- `persona(action="merge", keep_person_id, drop_person_id)`：地址并入主人物、删除另一实体；relationship 记忆留在单桶不转移。

### ③ 让认知进上下文 —— 首消息前置注入

主循环处理入站消息时，按 `participant_id`（可含 `conversation_id`）查找绑定：查到且该画像本会话未出现过（`source="persona_card:{person_id}"` 标记去重）→ 在该人**本会话首条消息之前**注入画像。无绑定、群、系统消息 → 不注入，照常处理。画像携带 `person_id`，供模型后续 `bind`/`card` 引用。

### ④ 信道提供语义 —— [CHANNELS] 提示词

各信道在既有 `agent_instructions()`（`[CHANNELS]` 段）中描述地址语义：如 wecom 的 `wecom:single:*`=人、`wecom:group:*`=群；weixin 的 `weixin:{bot}`=1:1 连接、`conversation_id`=会话 session、`weixin:control`=控制消息。模型据此判断"谁是能绑定的人、conversation_id 指什么"。

### 软边界

默认记忆/画像定位只覆盖当前对话人（模型复制消息头 `participant_id`）；跨人物需显式传参。共享部署下，`_auto_recall` 的近期活动召回在解析出人物时按该 participant 过滤，不把别人的近期事件喂给当前人。

## 配置与数据

```env
MEMORY__PERSONA_ENABLED=true        # 关闭后与现状完全一致
MEMORY__PERSONA_STORE_PATH=data/memory/persons.json
MEMORY__PERSONA_CARDS_DIR=data/memory/cards
```

管理端提供 `GET/POST /api/admin/persons`、`GET/PATCH/DELETE /api/admin/persons/{id}`、`POST /api/admin/persons/{id}/merge`、`GET/PUT /api/admin/persons/{id}/card`（需管理员令牌；DELETE 同步删除画像；merge 后 drop 人物的画像并入 keep）。

## 边界与注意事项

- **群聊/群内发件人**：`wecom:group:*` 无绑定故无人物上下文；群内发件人 v1 不做人物绑定（群是通信目标，不是 Person）。
- **画像由模型维护**：可能滞后或含编造内容，视作不可信输入；与 relationship 记忆矛盾时以画像为当前认知、记忆为可召回证据，由搭档自行调和。
- **模型误绑非人地址**：无硬校验，靠 `[CHANNELS]` 知识 + 工具描述约束，管理端可修正。
- **bubble 继承人物上下文**：主线已注入画像随 fork 上下文进入泡泡；fresh_start 泡泡需显式加载（后续能力）。
- **不混淆状态**：画像（当前认知）≠ relationship 记忆（可召回事实）≠ 日志（记录），各自存储、分开语义。

[← 返回项目首页](../../README.md)
