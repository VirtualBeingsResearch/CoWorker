# Persona：可选的人物子机制

中文 · [English](persona.en.md)

[← 返回架构与核心概念](README.md)

> Person 是**可选的、轻量的子机制**：它给模型一个"人"的抽象，用来识别同一真人跨渠道的多个地址、维护对这个人的画像认知、合并重复人物。它嵌入系统既有机制，由 `MEMORY__PERSONA_ENABLED=false` 整体关闭——关闭时行为与现状完全一致。

在生命哲学中，"关系"是构成持续 Coworker 的生命概念之一（见[虚拟生命理念](lifeform-philosophy.md)）。Person 承载的正是这种关系：`person_id` 是稳定锚点，跨渠道的地址绑定与搭档维护的备注都围绕它组织，并随 `data/memory/persons.json` 跨重启存活。

## 边界

Person 只做三件事：跨渠道归并地址、记录并渲染画像（框架 + 备注）、合并重复人物。它**不做**：多租户/账号系统（`person_id` 只是将来账号的接缝）、participant 分类（谁是可绑定的人由模型依据 `[CHANNELS]` 信道语义判断）、改写 guidelines / 注入通信录、碰 mem0（长期记忆保持单桶）、后台任务与自动创建人物（Person 只由 `bind` 显式建立）。

## 工作机制

### ① 记住谁是谁 —— PersonStore 与备注

- `data/memory/persons.json`：`Person`（`person_id`、`display_name`、`notes[]`、`aliases[]`）。**没有独立的画像文件**——画像是从这份结构化数据渲染出来的框架。
- `notes` 有两层：人物级（`Person.notes`，个性化信息）与地址级（`PersonAlias.notes`，同一信道可以有多个备注）。
- 每个地址是 `{participant_id, conversation_id?, channel, notes[]}`。`conversation_id` 仅在信道需要它定位具体会话/真人时记录（如微信的 session）；`wecom:single:*` 这类靠地址唯一路由的不带。

### ② 模型记录认知 —— `persona` 工具

- `persona(action="bind", participant_id, conversation_id?, person_id?/name?, note?)`：把地址绑定到已知人物（按 `person_id` 或名字匹配）或新建人物；`note` 追加到该地址的备注列表；
- `persona(action="note", person_id, note, remove?)`：记录/移除**人物级**个性化备注（`remove=true` 遗忘过时信息）；
- `persona(action="card", person_id)`：读**画像框架**——系统按固定结构（称呼、个性化备注、全部绑定地址与备注、更新时间）渲染，个性化内容全部来自备注；
- `persona(action="unbind", person_id, participant_id, conversation_id?)`：解除地址绑定（关系中的地址不再有效时）；
- `persona(action="delete", person_id)`：删除人物（关系结束；relationship 记忆留在单桶不转移）；
- `persona(action="merge", keep_person_id, drop_person_id)`：地址与备注并入主人物、删除另一实体；relationship 记忆留在单桶不转移。

### ③ 让认知进上下文 —— 首消息前置注入

主循环处理入站消息时，按 `participant_id`（可含 `conversation_id`）查找绑定：查到且该人**已有绑定内容**（称呼、备注或地址，绑定本身即算）且本会话未出现过（`source="persona_card:{person_id}"` 标记去重）→ 在该人**本会话首条消息之前**注入渲染的画像框架——地址段列出全部绑定地址，让模型看到此人的其他渠道。无绑定、群、系统消息 → 不注入，照常处理。画像携带 `person_id`，供模型后续操作引用。

### ④ 信道提供语义 —— [CHANNELS] 提示词

各信道在既有 `agent_instructions()`（`[CHANNELS]` 段）中描述地址语义：如 wecom 的 `wecom:single:*`=人、`wecom:group:*`=群；weixin 的 `weixin:{bot}`=1:1 连接、`conversation_id`=会话 session、`weixin:control`=控制消息。模型据此判断"谁是能绑定的人、conversation_id 指什么"。

### 软边界

默认记忆/画像定位只覆盖当前对话人（模型复制消息头 `participant_id`）；跨人物需显式传参。共享部署下，`_auto_recall` 的近期活动召回在解析出人物时按该 participant 过滤，不把别人的近期事件喂给当前人。

## 配置与数据

```env
MEMORY__PERSONA_ENABLED=true        # 关闭后与现状完全一致
MEMORY__PERSONA_STORE_PATH=data/memory/persons.json
```

管理端提供 `GET/POST /api/admin/persons`、`GET/PATCH/DELETE /api/admin/persons/{id}`（`PATCH` 可整体替换 `display_name`/`notes`/`aliases`）、`POST /api/admin/persons/{id}/merge`、`GET /api/admin/persons/{id}/card`（只读渲染框架；需管理员令牌）。

## 边界与注意事项

- **群聊/群内发件人**：`wecom:group:*` 无绑定故无人物上下文；群内发件人 v1 不做人物绑定（群是通信目标，不是 Person）。
- **画像由模型维护**：可能滞后或含编造内容，视作不可信输入；与 relationship 记忆矛盾时以画像为当前认知、记忆为可召回证据，由搭档自行调和。
- **模型误绑非人地址**：无硬校验，靠 `[CHANNELS]` 知识 + 工具描述约束，管理端可修正。
- **bubble 继承人物上下文**：主线已注入画像随 fork 上下文进入泡泡；fresh_start 泡泡需显式加载（后续能力）。
- **不混淆状态**：画像（当前认知）≠ relationship 记忆（可召回事实）≠ 日志（记录），各自存储、分开语义。

[← 返回项目首页](../../README.md)
