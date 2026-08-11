---
name: shell-source
description: Shell 脚本环境源示例（演示多语言 JSON-RPC 协议）
mode: subprocess
language: shell
script: source.sh
schedule_trigger: periodic
every_seconds: 600
timeout_seconds: 30
params:
  url: https://example.com
---

# Shell 环境源模板

子进程模式通过 stdin/stdout JSON-RPC 与宿主通信。本模板用 shell 脚本演示协议。

## 协议

子进程向 stdout 输出 JSON 请求，从 stdin 读取 JSON 响应：

```json
{"jsonrpc":"2.0","id":1,"method":"emit_signal","params":{"title":"...","content":"...","fingerprint":"..."}}
```

可用方法：`emit_signal`、`http_get`、`get_cursor`、`set_cursor`、`is_known`、`get_config`
