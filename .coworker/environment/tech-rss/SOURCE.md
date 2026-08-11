---
name: tech-rss
description: 订阅技术博客 RSS 源
mode: inline
language: python
script: source.py
schedule_trigger: periodic
every_seconds: 3600
timeout_seconds: 30
params:
  url: https://www.zhihu.com/rss
  max_items: 5
protected: false
---

# RSS 订阅源

定时拉取 RSS feed，有新条目时推送环境信号。

## 参数

- `url`：RSS 或 Atom feed 地址
- `max_items`：每次轮询最多推送的条目数

## 自定义

修改 `params.url` 为你想订阅的 RSS 地址。支持 RSS 2.0 和 Atom 格式。
