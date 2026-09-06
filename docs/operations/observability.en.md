# Observability and Routine Operations

[中文](observability.md) · English

[← Back to Configuration and Operations](README.en.md)

Observability should prove more than process liveness: Coworker uses the intended model, handles
messages, has no continuously failing background task, and exposes explainable cost, disk, and
connection state.

## Observation surfaces

| Surface | Question answered |
|---|---|
| `GET /status` | Is the Agent running or sleeping? With a communication token configured, a valid Bearer also reports the active model, cycle count, usage, and channel message totals |
| Life Overview | What is the current context, model, and high-level state? |
| Diagnostics and Audit | Where are background tasks waiting, what failed, and what did an administrator change? |
| Diagnostics and Audit → Message traffic | Which recent channel messages were received, sent, denied, ignored, or failed delivery? |
| Life History and `data/logs/` | What model, tool, or message event actually occurred? |
| `GET /api/debug/tasks` | Are event-loop tasks stuck on the same await? Trusted diagnostics only |
| `GET /metrics` | Runtime and cumulative metrics in Prometheus text format for scrape-based monitoring |
| Docker healthcheck / `docker compose ps` | Are the container and HTTP service reachable? |

`pending` often means waiting for a message or timer, not failure. Combine wait location, last
successful activity, and repeated errors before declaring a stall.

## Suggested health checks

After deployment or upgrade:

```bash
# When a token is configured: no Bearer returns basic status; a valid token returns the full snapshot
curl -fsS http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"
docker compose ps
```

Then send a test message that cannot trigger a high-risk tool and verify inbound, model, and reply
paths. A health probe should never call endpoints that incur model cost or mutate state.

## Usage and cost

When a communication token is configured, the `usage_stats` field returned by an authenticated `GET /status` exposes today, last_7_days, and lifetime
windows, split by model, Provider/model, and scopes such as main, summary, vision, bubble, subconscious, and long_term. This
ordinary status interface returns usage only, never monetary amounts.

The authenticated `GET /api/admin/usage` endpoint and Runtime analytics calculate local spend
estimates from current `llm.model_prices` for today, 7/30 days, lifetime, previous periods, custom
ranges, dates, hours, and scopes. The formula is “uncached input × input price + cached input ×
cached-input price + output × output price,” with every price quoted per million tokens. Anomalous
cached tokens are clamped to input tokens. Currencies are displayed and exported independently,
without conversion.

Unpriced tokens are not treated as free: amount subtotals include only priced usage, while
`priced_tokens`, `unpriced_tokens`, and `pricing_coverage` expose the gap. An explicit zero price is
still priced. Existing token data may carry the current exact/estimated markers; untracked calls
have no tokens available for pricing.

Watch for:

- sudden call or token growth;
- fallback handling most traffic, indicating primary Provider instability;
- unexpected Bubble or subconscious share;
- steadily rising thinking time;
- `unknown/<model>`, usually from older logs without Provider data.

Amounts are always local estimates, not Provider invoices. They exclude request fees, separate
image/video charges, cache writes, tiers, batch discounts, taxes, and account-level concessions.
Use the external service as the billing authority.

## Prometheus metrics (`GET /metrics`)

`/metrics` reuses the API port and serves the Prometheus text format (version 0.0.4).
Authentication matches `/status`: when `API__COMMUNICATION_TOKEN` is explicitly configured the
scrape must present a valid Bearer token — supply it as `bearer_token` in the Prometheus scrape
config. Without a configured token the endpoint behaves like the rest of the unauthenticated API
surface (the API binds to `127.0.0.1` by default; always configure a token before exposing it).
Set `API__METRICS_ENABLED=false` to disable collection entirely; during first-run setup the
endpoint redirects to `/admin` like every other path.

Example scrape configuration:

```yaml
scrape_configs:
  - job_name: coworker
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ["127.0.0.1:8000"]
    bearer_token: "<API__COMMUNICATION_TOKEN>"
```

Metrics fall into two classes. Runtime metrics (`coworker_http_requests_total{method,route,status}`,
`coworker_http_request_duration_seconds`, `coworker_sessions_active{transport}`,
`coworker_sessions_total{transport,state}`, `coworker_relay_connected`,
`coworker_relay_connects_total`, `coworker_relay_reconnects_total`,
`coworker_relay_frames_total{direction}`, `coworker_relay_errors_total`,
`coworker_model_switches_total{...}`) cover only the current process lifetime and reset on
restart; the HTTP route label is the route template (e.g. `/ws/{participant_id}`), so raw
participant IDs never become high-cardinality labels, and streaming responses measure until the
first body byte. Cumulative metrics come from the persisted aggregates
(`coworker_llm_calls_total{provider,model,scope}`, `coworker_llm_tokens_total`,
`coworker_tool_calls_total`, `coworker_tool_results_total`, `coworker_skill_loads_total`,
`coworker_bubble_runs_total{outcome}`, `coworker_memory_compressions_total{trigger}`,
`coworker_messages_in_total{source}`, `coworker_task_reminders_total`,
`coworker_auto_recalls_total`, `coworker_auto_recall_memories_total`,
`coworker_subconscious_spawned_total`, `coworker_subconscious_done_total`,
`coworker_channel_messages_total{channel,direction,status}`) and keep accumulating across
restarts. Two gauges, `coworker_uptime_seconds` and `coworker_agent_cycles_total`, are also
exposed.

`coworker_channel_messages_total` covers the retained channel-traffic window: records rotated
out of `channel_traffic.jsonl` no longer contribute, so in-process counters stay monotonic but a
rebuilt counter after a restart may be lower than before. Labeled series do not duplicate an
unlabeled total; aggregate with `sum by (...)` on the Prometheus side.

## Logs and sensitive information

Record time, timezone, participant, Channel, and the first error. Before sharing logs, remove
tokens, keys, message text, attachments, personal paths, Weixin QR codes, and Relay pairing
material. Never upload a complete configuration export.

`data/logs/channel_traffic.jsonl` is the metadata source for the administration console's Message
traffic view. It excludes message bodies, attachment contents, and credentials, but contains
potentially sensitive participant IDs. It rotates at 10 MiB with six backups; include these files
in access control, retention, and cleanup whenever changing the overall log-backup policy.

Retention must account for:

- incident audit and policy needs;
- raw interaction logs used for memory-tree backfill;
- sensitive or large attachments and tool output;
- separate Desktop, Coworker, and Relay log locations.

## Routine

- Daily: repeated task failures, anomalous usage, free disk, and pending alarms.
- Weekly: Providers/fallbacks, backup results, offline participants, and long-running tasks.
- Monthly or before major upgrades: recovery drill, capability review, version and capacity trend.

Alert at least on repeated health failure, low disk, growing failed-task count, unreachable Relay,
and stale backups. `/metrics` scrape-based monitoring covers most of these signals (Relay
connectivity, failed-task growth, usage spikes); process and disk signals that remain outside it
continue to come from host-level monitoring. Do not scrape logs containing sensitive message
bodies as a default metrics source.

## Incident response order

1. Record time, version, runtime, and impact.
2. Preserve the first error and a small surrounding log window.
3. Determine whether one model, Channel, participant, or client is affected.
4. Protect backups and evidence.
5. Apply the smallest reversible recovery.
6. Follow [Troubleshooting](troubleshooting.en.md), then decide on restart, rollback, or restore.

[← Back to project home](../../README.en.md)
