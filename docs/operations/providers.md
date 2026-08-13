# Provider 配置指南

中文 · [English](providers.en.md)

[← 返回配置与运维](README.md)

Coworker 内置 `anthropic`、`openai`、`deepseek`、`qwen`、`zhipu` 和 `minimax`
Provider。首次调用可能计费；项目不会在向导中主动探测模型能力。

## 选择模型

对主线模型至少确认：

- 支持 tool/function calling，并能稳定返回对应 Provider 方言；
- 上下文窗口覆盖预期短期记忆和工具结果；
- 输出 Token 上限、thinking 模式、延迟和价格可接受；
- 服务条款允许发送任务所需的对话、记忆和附件。

推荐目录只列静态声明支持工具调用的模型。手动输入目录外模型时，需要在首次设置或
Provider 连接中声明它是否支持工具、图片和视频；系统不会发起在线能力探测。

## 配置方式

优先使用首次设置或管理后台；无人值守环境使用 `.env`。常用字段：

```env
LLM__DEFAULT_PROVIDER=deepseek
LLM__DEFAULT_MODEL=deepseek-chat
LLM__DEEPSEEK_API_KEY=...
LLM__DEEPSEEK_BASE_URL=
```

Base URL 留空时使用对应 Provider 默认值。使用 OpenAI-compatible 网关时仍应选择与其
实际请求/响应方言匹配的 Provider 类型；“兼容”不保证工具调用、thinking、视频或错误结构
完全一致。

## 同类型多实例

复制 `providers.json.example` 为未纳入版本控制的 `providers.json`：

```json
[
  {
    "name": "zhipu-team-a",
    "type": "zhipu",
    "api_key": "...",
    "base_url": "",
    "default_model": "glm-5.1",
    "model_capabilities": [
      { "model": "custom-omni-model", "tools": true, "vision": true, "video": false }
    ]
  }
]
```

`name` 是 `switch_model` 和 fallback 使用的注册名，必须唯一；`type` 决定 API 方言。
文件同名实例覆盖扁平环境配置。

`model_capabilities` 按精确模型 ID 声明 `tools`、`vision` 和 `video`。声明项会覆盖接口
协议的内置模型判断；未列出的模型继续使用内置目录。`video: true` 时也必须设置
`vision: true`。主线和 fallback 模型必须支持 `tools`，视觉专用模型必须支持 `vision`。

## 定义模型价格

管理后台“运行设置 → 模型与 Provider”提供独立定价表，也可通过 `LLM__MODEL_PRICES`
传入 JSON。定价按 Provider 注册名和模型 ID 精确匹配，不要求该连接由管理后台维护，因而
可以覆盖 `.env` 或 `providers.json` 中的连接，也可以保留已停用的历史 Provider/模型价格。

输入、输出和可选缓存输入价格均按每百万 Token 填写。缓存输入价留空时使用普通输入价；
不同币种分别累计，不做汇率换算。修改价格立即重算管理端已有 Token 用量，不会修改
`usage_stats.json`，也不会记录调用发生时的旧价格。

## 模型分工

- 主线：对话、工具规划和持续任务；
- summary：短期压缩与摘要，通常关闭 thinking 以降低成本；
- vision：图片/视频分析，必须由 Provider 声明相应能力；
- mem0：长期记忆提取；
- fallback：主模型失败后的有序接棒链。

先验证每个专用模型，再加入 fallback。不要把失效 Provider 留在链首制造额外延迟。

## 常见问题

- **Provider 未注册**：检查 API Key 是否存在，`DEFAULT_PROVIDER` 是否为注册名。
- **401/403**：检查密钥、Base URL、账户权限和代理是否修改 Header。
- **404/模型不存在**：模型 ID 会原样传给 Provider；使用服务端实际 ID。
- **工具调用失败**：确认模型和网关同时支持 tool/function calling。
- **thinking 参数失败**：关闭该专用模型的 thinking，或换用明确支持的模型。
- **高延迟/高成本**：在管理端“运行分析”按 main、summary、vision、bubble、subconscious
  和 mem0 区分职责，结合定价覆盖率和 Provider 账单再调整模型分工。

完整变量表见[配置与模型](configuration.md)，数据外发范围见
[数据与信任边界](../architecture/data-boundaries.md)。

[← 返回项目首页](../../README.md)
