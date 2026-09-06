# Observability and Routine Operations

[中文](observability.md) · English

[← Back to Configuration and Operations](README.en.md)

Observability should prove more than process liveness: Coworker uses the intended model, handles
messages, has no continuously failing background task, and exposes explainable cost, disk, and
connection state.

## Observation surfaces

| Surface | Question answered |
|---|---|
| `GET /status` | Is the Agent running or sleeping? With a communication token configured, a valid Bearer also reports the active model and usage |
| Life Overview | What is the current context, model, and high-level state? |
| Diagnostics and Audit | Where are background tasks waiting, what failed, and what did an administrator change? |
| Diagnostics and Audit → Message traffic | Which recent channel messages were received, sent, denied, ignored, or failed delivery? |
| Life History and `data/logs/` | What model, tool, or message event actually occurred? |
| `GET /api/debug/tasks` | Are event-loop tasks stuck on the same await? Trusted diagnostics only |
| Docker healthcheck / `docker compose ps` | Are the container and HTTP service reachable? |

`pending` often means waiting for a message or timer, not failure. Whether a stall is real depends
on the wait location, the last successful activity, and whether errors repeat.

## Suggested health checks

After deployment or upgrade:

```bash
# When a token is configured: no Bearer returns basic status; a valid token returns the full snapshot
curl -fsS http://127.0.0.1:8000/status \
  -H "Authorization: Bearer <API__COMMUNICATION_TOKEN>"
docker compose ps
```

Then send a test message that cannot trigger a high-risk tool and verify inbound, model, and reply
paths. `GET /status` and the Docker healthcheck have no side effects; `POST /messages`,
`POST /backups/restore`, and similar endpoints incur model cost or mutate state and are not probe
targets.

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

Signals worth watching:

- sudden call or token growth;
- fallback handling most traffic, indicating primary Provider instability;
- unexpected Bubble or subconscious share;
- steadily rising thinking time;
- `unknown/<model>`, usually from older logs without Provider data.

Amounts are always local estimates, not Provider invoices. They exclude request fees, separate
image/video charges, cache writes, tiers, batch discounts, taxes, and account-level concessions.
The external service is the billing authority.

## Logs and sensitive information

An incident record typically includes the time, timezone, participant, Channel, and the first
error. Logs and configuration exports contain sensitive material such as tokens, keys, message
text, attachments, personal paths, Weixin QR codes, and Relay pairing material; a complete
configuration export concentrates the whole configuration and is equally sensitive.

`data/logs/channel_traffic.jsonl` is the metadata source for the administration console's Message
traffic view. It excludes message bodies, attachment contents, and credentials, but contains
potentially sensitive participant IDs. It rotates at 10 MiB with six backups; these files are also
part of access control, retention, and cleanup scope when the overall log-backup policy changes.

Retention factors include:

- incident audit and policy needs;
- raw interaction logs used for memory-tree backfill;
- sensitive or large attachments and tool output;
- separate Desktop, Coworker, and Relay log locations.

## Routine

- Daily: repeated task failures, anomalous usage, free disk, and pending alarms.
- Weekly: Providers/fallbacks, backup results, offline participants, and long-running tasks.
- Monthly or before major upgrades: recovery drill, capability review, version and capacity trend.

Typical alerting covers at least repeated health failure, low disk, a growing failed-task count, an
unreachable Relay, and stale backups. Coworker does not currently expose Prometheus metrics;
external monitoring typically polls lightweight state and host signals, and logs containing
sensitive message bodies are not the default metric source.

## Incident response order

1. Record time, version, runtime, and impact.
2. Preserve the first error and a small surrounding log window.
3. Determine whether one model, Channel, participant, or client is affected.
4. Protect backups and evidence.
5. Apply the smallest reversible recovery.
6. Follow [Troubleshooting](troubleshooting.en.md), then decide on restart, rollback, or restore.

[← Back to project home](../../README.en.md)
