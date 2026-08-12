# Telegram

中文 · [English](telegram.en.md)

[← 返回通信与客户端](README.md)

Coworker 可以通过一个或多个 Telegram Bot 接收私聊、群组、话题和频道消息，并向已发现的
聊天发送文本或附件。每个 Bot 都有独立的凭据、Bot API 地址、长轮询 offset 和联系人集合，
因此同一个 Telegram chat 通过不同 Bot 接入时不会混淆。

## 创建并配置 Bot

先在 Telegram 中通过 [@BotFather](https://t.me/BotFather) 创建 Bot 并取得 token。不要把
token 提交到 Git、日志或截图中。可以在管理端「运行设置 → Telegram」添加多个 Bot；每个
实例使用一个稳定的 `instance_id`，只允许小写字母开头，并包含小写字母、数字、`_` 或 `-`，
最长 32 个字符。

无人值守部署也可以在 `.env` 中把多个实例写成一个 JSON 对象：

```dotenv
TELEGRAM__BOTS={"main":{"enabled":true,"display_name":"主机器人","bot_token":"replace-with-main-token","api_base_url":"https://api.telegram.org","local_mode":false,"poll_timeout_seconds":30},"work":{"enabled":true,"display_name":"工作机器人","bot_token":"replace-with-work-token"}}
```

每个实例支持以下字段：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否运行这个 Bot；关闭后会停止它自己的长轮询 |
| `display_name` | 空 | 仅供管理员识别的名称 |
| `bot_token` | 空 | BotFather 签发的机密 token；管理 API 和页面不会回显原值 |
| `api_base_url` | `https://api.telegram.org` | Telegram Bot API 根地址，可为每个 Bot 配置不同的官方、自托管或代理地址 |
| `local_mode` | `false` | 是否按本地 Bot API Server 模式处理文件路径 |
| `poll_timeout_seconds` | `30` | `getUpdates` 长轮询超时，范围 1～50 秒 |

`api_base_url` 填根地址即可，末尾不要手工追加 `/bot`；Coworker 会分别构造 Bot API 与文件
API 地址。使用自托管 [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api)
时通常还应启用 `local_mode`。保存管理端配置后，新增、删除、启停或修改某个实例会热应用，
不需要重启 Coworker。

Telegram Bot API 的 `getUpdates` 与 webhook 互斥；如果这个 token 之前配置过 webhook，需先
调用 `deleteWebhook` 清除它。参见 Telegram 的[更新接收说明](https://core.telegram.org/bots/api#getting-updates)。

## 聊天发现与 participant ID

Telegram participant ID 使用短前缀：

```text
tg:<instance_id>:<chat_id>
```

例如 `tg:main:123456789` 或 `tg:work:-1001234567890`。`instance_id` 是配置中的 Bot 实例，
`chat_id` 是 Telegram 的数字聊天 ID；负数常用于群组或频道。这个命名空间让多个 Bot 即使
遇到相同的 `chat_id` 也保持为不同对象。

Coworker 只允许向已发现的聊天发送消息。用户需要先私聊 Bot，或把 Bot 加入群组/频道并让
它收到消息；之后完整 ID 才会出现在 `list_connections`。只输入 `instance_id:chat_id` 可以
精确解析；只输入 `chat_id` 仅在它只属于一个已知 Bot 时有效。Bot 不能主动发起与陌生用户的
私聊，这是 Telegram 平台限制。

每条 Telegram 入站内容都会以信道自有头明确标注 `私聊`、`群聊` 或 `频道`，不改变
Coworker 的通用入站消息外壳。群组中，Coworker 还会在正文前保留发送者 ID、用户名和显示名，避免把群组 participant 误当成单个成员。
Telegram forum topic 的 `message_thread_id` 会作为 `conversation_id`；回复时传回它即可
发送到同一话题。频道需要让 Bot 具备读取和发送消息所需的管理员权限。

如果 Bot 在群组中看不到普通消息，请在 BotFather 检查
[Privacy Mode](https://core.telegram.org/bots/features#privacy-mode)。保持 Privacy Mode 时，
Bot 通常只会收到命令、回复和与它相关的消息；需要处理全部群消息时应按部署需求关闭。

## 消息与附件

入站支持文本、caption、图片、文档、视频、音频、语音和动画。附件在通过信道访问规则后
才会下载，单个文件最大 20 MiB，并保存到 Coworker 的附件目录；不超过 10 MiB 的图片和 PDF
还会以内联内容交给模型。出站支持文本、图片和文件，长文本按 Telegram 的 4096 字符限制
自动拆分，单个上传文件最大 50 MiB。

Telegram 消息和附件均属于不可信外部输入，可能包含提示注入或恶意文件。只给 Bot 必需的
群组/频道权限，并结合信道访问规则限制来源：

```dotenv
CHANNEL_ACCESS={"telegram":{"inbound_allow":["tg:main:*"],"inbound_deny":["tg:main:-1009999999999"],"outbound_allow":["tg:main:*"],"outbound_deny":[]}}
```

规则使用信道名 `telegram`，participant 则使用 `tg:` 前缀；deny 优先于 allow。被拒绝的
入站消息会在下载附件、记录联系人和交给 Agent 之前丢弃，并尽力向原聊天发送通用拒绝提示。

## 状态与排障

每个实例的 offset 与已发现联系人保存在
`MEMORY__DB_PATH/telegram/<instance_id>.json`。该文件不保存 Bot token。若同一
`instance_id` 换成了另一个 Bot 的 token，Coworker 会根据 Bot user ID 清空旧 offset 和
联系人，避免把新 Bot 的消息发往旧 Bot 发现的聊天。

- Bot 一直离线：检查 `enabled`、token、`api_base_url` 和代理/防火墙；日志只会周期性报告
  长轮询失败，恢复后会自动继续。
- 收不到任何更新：确认该 token 没有活动 webhook，并检查群组 Privacy Mode 或频道权限。
- 自定义 API 可发送但不能下载：确认 `api_base_url` 是服务根地址，且反向代理同时转发
  `/bot...` 与 `/file/bot...` 路径；本地 Bot API Server 同时启用 `local_mode`。
- 同一聊天出现多个 ID：这是多个 Bot 的预期隔离；选择目标 Bot 对应的完整 `tg:` ID。

[← 返回项目首页](../../README.md)
