# Explore Lab 使用与开发

中文 · [English](explore-lab.en.md)

[← 返回开发与协作](README.md)

Explore Lab 从一个运行中的 Coworker 导入敏感配置快照，在隔离工作目录中创建可暂停、
单步、分叉和回放的实验分支。它适合比较 Prompt、配置和行为，不应作为生产消息入口。

## 启动

```bash
npm ci --prefix apps/explore-lab/frontend
npm --prefix apps/explore-lab/frontend run build
uv run --project apps/explore-lab/backend python -m explore_lab
```

打开 <http://127.0.0.1:8100/>。后端默认托管
`apps/explore-lab/frontend/dist`；可用 `--ui-dir` 指定其他构建目录。

## 导入实验

“导入”会使用管理员令牌请求目标 Coworker 的 `/api/export_config`。导出包包含有效配置、
`data/`、`.coworker/` 和 `providers.json`，可能包含全部密钥、消息和附件。只在本机或
隔离可信网络使用，不要提交实验工作目录。

导入完成后创建一个 baseline 根分支。分支运行时使用模拟通信对象：
`communicate` 只记录出站，不投递到真实 Channel。

## 实验工作流

1. 在 baseline 上输入消息，使用 step/step N 控制循环；
2. 在稳定状态创建 fork，并设置标签、备注和配置/Prompt 覆盖；
3. 分别运行分支，查看 transcript、Bubble、潜意识和状态；
4. 给分支标记 verdict；
5. 在对比视图检查输出、周期数、Prompt、`thinking.md` 和配置差异。

回退步骤恢复分支自己的快照，不会修改来源 Coworker。

## Scenario 与 replay

Scenario 是带 `participant_id` 和可选延时的消息序列。Replay 会先从选中分支创建 N 个
子分支，再向每个分支发送相同事件并恢复运行，适合观察非确定性和配置差异。不要用一次
结果宣称模型行为稳定；保留样本数、版本、模型和配置。

## 分支生命周期

- Lab 重启后会尝试恢复分支元数据和可运行状态；
- 休眠分支在打开时可唤醒；
- 有子分支的父分支不能删除；
- 批量操作可对多个分支执行 step、pause、resume 等控制；
- 退出 orchestrator 时会暂停并终止其分支 runner。

## 安全与清理

- 管理员令牌只用于导入请求，不要写入截图或实验备注；
- 导出包和分支目录按凭据文件处理；
- 不要让 Explore Lab 监听公网；其开发 CORS 和分支控制接口不是生产授权边界；
- 清理前保留需要复现的实验元数据、版本、配置差异和结论；
- 发现真实缺陷时，在最小分支复现后再回到源码测试中固化。

[← 返回项目首页](../../README.md)
