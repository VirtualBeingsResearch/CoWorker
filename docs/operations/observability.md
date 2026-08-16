# 可观测性与日常运维

中文 · [English](observability.en.md)

[← 返回配置与运维](README.md)

可观测性的目标不是“进程还在”，而是确认 Coworker 正在使用预期模型、能够处理消息、
后台任务没有持续失败，并且成本、磁盘和连接状态可解释。

## 观测入口

| 入口 | 回答的问题 |
|---|---|
| `GET /status` | Agent 是否运行/休眠；已配置令牌时携带通信 Bearer 还包括当前模型、周期数和用量 |
| 管理后台“生命总览” | 当前上下文、模型和关键状态 |
| “诊断与审计” | 后台任务在哪里等待、最近错误和管理员操作 |
| “诊断与审计 → 消息流量” | 各信道最近哪些消息被接收、发送、拒绝、忽略或投递失败 |
| “生命全史”与 `data/logs/` | 某次模型、工具或消息实际发生了什么 |
| `GET /api/debug/tasks` | 事件循环任务是否卡在同一 await；仅可信诊断环境 |
| Docker healthcheck / `docker compose ps` | 容器和 HTTP 服务是否可达 |

`pending` 常表示等待消息或定时器，不等于故障。判断卡死要结合等待位置、最近成功活动和
错误是否重复。

## 建议健康检查

每次部署或升级后：

```bash
# 已配置令牌时：无 Bearer 只返回基础状态，携带令牌返回完整快照
curl -fsS http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"
docker compose ps
```

再发送一条不会触发高风险工具的测试消息，确认入站、模型和回复路径。健康探针不要频繁
调用会产生模型费用或改变状态的接口。

## 用量与成本

已配置通信令牌并携带 Bearer 的 `GET /status` 会返回 `usage_stats` 字段，提供 today、last_7_days 和 lifetime 窗口，并
按模型、Provider/模型和 main、summary、vision、bubble、subconscious、mem0 等 scope 拆分；这个普通
状态接口只返回用量，不返回金额。

管理员 `GET /api/admin/usage` 和“运行分析”会使用当前 `llm.model_prices` 实时计算本地消费
估算，覆盖 today、7/30 日、lifetime、上一周期、自定义范围、日期、小时和职责桶。输入金额
按“未缓存输入 × 输入价 + 缓存输入 × 缓存输入价 + 输出 × 输出价”计算，所有价格均按每百万
Token 折算；异常的缓存 Token 会钳制到输入 Token。不同币种独立显示和导出，不做换汇。

未定价 Token 不按零价处理：金额小计只包含已定价部分，同时返回 `priced_tokens`、
`unpriced_tokens` 和 `pricing_coverage`。零价配置仍视为已定价。已有 Token 可能包含现有
“精确/估算”标记的本地估算值，未追踪调用则没有可用于定价的 Token。

日常关注：

- 调用量或 Token 突增；
- fallback 持续接管，说明主 Provider 不稳定；
- Bubble 或潜意识占比与预期不符；
- thinking 时间持续升高；
- `unknown/<model>`，通常来自缺少 Provider 信息的旧日志。

金额始终是本地估算，不是 Provider 账单。它不覆盖请求费、图片/视频独立计费、缓存写入、
阶梯价、批处理折扣、税费或账户级优惠；最终费用以外部服务为准。

## 日志与敏感信息

记录问题发生时间、时区、participant、Channel 和第一个错误。分享日志前移除令牌、密钥、
消息正文、附件、个人路径、微信二维码和 Relay 配对材料。不要上传完整配置导出包。

`data/logs/channel_traffic.jsonl` 是管理端“消息流量”的元数据来源：不含消息正文、附件内容或
凭据，但包含可能敏感的 participant ID。文件达到 10 MiB 后轮转并保留 6 份备份；调整整体
日志备份策略时应把这些文件一起纳入访问控制、保留和清理范围。

日志保留策略要同时考虑：

- 故障审计和合规需要；
- 原始交互日志可能用于记忆树历史回溯；
- 附件和工具结果可能包含敏感或大体积数据；
- Desktop、Coworker、Relay 各有独立日志位置。

## 日常节奏

- 每日：检查持续失败任务、异常用量、磁盘余量和待处理闹钟。
- 每周：检查 Provider/fallback、备份结果、离线 participant 和长期任务。
- 每月或重大升级前：执行恢复演练、审查能力内容、记录版本和容量趋势。

告警至少覆盖：进程/健康检查连续失败、磁盘空间不足、错误任务持续增长、Relay 不可达和
备份过期。当前项目不直接提供 Prometheus 指标；外部监控应轮询轻量状态和进程/磁盘信号，
不要抓取包含敏感正文的日志作为默认指标。

## 事件响应顺序

1. 记录时间、版本、运行方式和影响范围；
2. 保存第一个错误和少量相邻日志；
3. 确认是否只影响一个模型、Channel、participant 或客户端；
4. 保护备份和故障现场；
5. 做最小可逆恢复；
6. 按[故障排查](troubleshooting.md)定位，再决定重启、回滚或恢复。

[← 返回项目首页](../../README.md)
