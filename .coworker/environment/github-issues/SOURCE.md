---
name: github-issues
description: 跟踪 GitHub 仓库的新 issue 和评论
mode: inline
language: python
script: source.py
schedule_trigger: periodic
every_seconds: 300
timeout_seconds: 60
params:
  repository: VirtualBeingsResearch/CoWorker
  state: open
  include_comments: true
  per_page: 10
protected: true
---

# GitHub Issues 感知源

轮询指定 GitHub 仓库的新 issue 和评论，有新内容时推送环境信号。

## 参数

- `repository`：`owner/repo` 格式的仓库全名
- `state`：issue 状态过滤（`open` / `closed` / `all`）
- `include_comments`：是否同时跟踪 issue 评论
- `per_page`：每次轮询最多拉取的条目数

## 自定义

修改 `params.repository` 为你想跟踪的仓库。如需鉴权（提高 rate limit），
在 `params` 中添加 `token`。
