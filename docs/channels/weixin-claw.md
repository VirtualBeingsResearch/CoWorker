# 微信 Claw

中文 · [English](weixin-claw.en.md)

[← 返回通信与客户端](README.md)

微信 Claw 信道通过腾讯个人微信 iLink ClawBot 接口连接 Coworker。它不同于企业微信智能机器人：每个个人微信账号通过二维码授权，并由独立的长轮询任务收发私聊消息。

## 添加和管理账号

在 `/admin` 打开「微信 Claw」，启用信道后点击「扫码连接微信」。管理页会在本地生成并显示真正的二维码 PNG，扫码确认后凭据写入管理员覆盖配置并立即热加载，无需重启。可以重复扫码添加多个账号，并在同一页面重命名、停用或移除各账号。

每个账号拥有稳定 UUID。参与者 ID 格式为：

```text
weixin:<account_uuid>:<weixin_user_id>
```

因此同一个联系人即使出现在两个绑定账号中，其上下文、游标和回复路由也不会混淆。Token 会在管理 API 响应中遮蔽，并只保存在管理员配置文件中。

## 由搭档发起扫码邀请

当已知的私聊用户明确要求接入微信 Claw 时，搭档仍使用通用 `communicate`，在 `extra.channel_action` 中传入 `{"channel":"weixin","type":"connect"}`：

1. Channel Action Registry 为指定的已知 `participant_id` 生成一次性二维码；
2. Coworker 把二维码 PNG 附件和备用链接定向发送到该私聊，并在工具结果中返回连接 `session_id`；
3. 再次使用 `communicate`，传入 `{"channel":"weixin","type":"poll","session_id":"..."}` 查询扫码结果；手机要求数字时同时传 `verify_code`；
4. 扫码者从微信发出第一条消息后，系统为这段微信联系人关系产生新的 `weixin:*` participant，搭档再自行确认和组织它与既有联系人的关系。

二维码的接收 `participant_id` 只决定邀请投递到哪里，不会与新 ClawBot 或后来产生的微信 participant 建立底层绑定。微信连接不是独立模型工具，而是可注册的通用信道动作；后端拒绝把连接卡片发送到群聊。管理员仍可独立在管理页完成扫码。是否发起邀请、以及如何识别和维护新旧联系人关系，都由搭档根据对话决定。

当前微信 Claw 入站会提取文本和语音转写；图片、文件和视频以本地化占位说明交给 Agent。出站目前发送文本；二维码图片借助发起邀请所在信道的附件能力发送，若该信道不支持附件，用户仍会收到备用链接。

协议兼容以腾讯的 [openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 实现为参考。
