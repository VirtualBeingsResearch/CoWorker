---
name: my-source
description: 描述这个源感知什么（将显示在系统提示中）
mode: inline
language: python
script: source.py
schedule_trigger: periodic
every_seconds: 300
timeout_seconds: 60
params:
  # 在这里声明源参数，source.py 中通过 ctx.config 访问
  url: https://example.com/api
---

# 我的自定义环境源

在此撰写人类可读的说明（如 Markdown），描述这个源做什么、如何配置参数。
