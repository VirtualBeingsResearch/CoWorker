# 微信 Claw

中文 · [English](weixin-claw.en.md)

[← 返回通信与客户端](README.md)

微信 Claw 信道通过腾讯个人微信 iLink ClawBot 接口连接 Coworker。它不同于企业微信智能机器人：一个 iLink Bot 实例只能绑定一个个人微信账号，并由独立的长轮询任务收发消息；同一个 Coworker 可以创建多个彼此独立的 Bot 实例。

## Participant 与连接

每个已绑定的 Bot 实例对应一个通信 participant：

```text
weixin:<bot_instance_id>
```

微信侧用户 ID、凭据、游标和 context token 都属于该实例的内部协议状态，不进入 participant ID。`list_connections` 还会列出固定的管理入口：

```text
weixin:control
```

`ConnectionInfo.kind` 分别为 `weixin:direct` 与 `weixin:control`，无需额外的 participant role。

## 由搭档管理连接

微信连接没有独立模型工具。信道启用时会向系统 Prompt 注入简短操作说明，搭档继续使用通用 `communicate`。

创建配对会话：

```json
{
  "participant_id": "weixin:control",
  "extra": {"action": "connect"}
}
```

结果返回 `session_id` 和本地 `qrcode_path`。二维码不会自动发送给任何 participant；搭档根据当前对话选择接收者，再使用普通 `communicate` 将该路径作为 `image` 附件发送。需要让身份证卡片页面的 ChatDock 显示连接状态时，可以同时携带纯展示元数据：

```json
{
  "connection_status": {
    "channel": "weixin",
    "status": "wait",
    "session_id": "..."
  }
}
```

配对状态由微信信道在后台自动轮询，搭档不需要反复调用工具或
`list_connections`。需要主动查看当前状态时：

```json
{
  "participant_id": "weixin:control",
  "extra": {"action": "status"}
}
```

手机要求验证码时，再发送：

```json
{
  "participant_id": "weixin:control",
  "extra": {
    "action": "verify",
    "session_id": "...",
    "verify_code": "手机显示的数字"
  }
}
```

确认后会得到新的 `weixin:<bot_instance_id>`。二维码接收者与新连接没有底层绑定，搭档自行组织它们之间的联系人关系。

仅在用户明确要求并确认后移除本地连接：

```json
{
  "participant_id": "weixin:control",
  "extra": {
    "action": "remove",
    "bot_instance_id": "...",
    "confirm": true
  }
}
```

移除会停止轮询并删除 Coworker 本地凭据和运行状态，但不会远程注销微信侧授权，也不会删除既有聊天记录。

## 管理页面与运行行为

在 `/admin` 打开「微信 Claw」也可以扫码添加、重命名、停用或移除实例。前端通过通用的
`/api/admin/channels/{channel}/management` 接口访问模块贡献的能力，Admin 后端不理解微信命令。未结束的配对会话由后端维护；离开再返回设置页时，页面会恢复二维码和当前状态。配对会话是临时状态，Coworker 进程重启后不会恢复。

已绑定实例保存在 `MEMORY__DB_PATH/weixin_connections.json`，不属于
`admin_config.json` 设置。正常空轮询和管理页状态查询只记录为 DEBUG。首次消息轮询故障记录 WARNING，重复重试降为 DEBUG，恢复时记录一次 INFO；鉴权或协议错误仍保持可见。日志不会记录 token、二维码内容、context token 或消息正文。

身份证卡片页面的 ChatDock 会把二维码作为聊天展示数据保存在 localStorage，因此切换页面后仍能恢复，不要求终端聊天接口维护额外的二维码资源。

当前微信 Claw 入站会提取文本和语音转写；图片、文件和视频以本地化占位说明交给 Agent。普通出站目前发送文本。

协议兼容以腾讯的 [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 实现为参考。
