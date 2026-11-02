# Preferred Models, Temporary Fallbacks, and Recovery

[中文](model-routing.md) · English

[← Back to Architecture and Core Concepts](README.en.md)

Model orchestration stores a user's lasting choice; routing handles temporary failures of an
individual request. A successful backup no longer becomes a permanent switch, and a restart no
longer replaces a confirmed main model with a legacy startup default.

## Preference, execution, and persistence

| State | Meaning | Changed by |
|---|---|---|
| `preferred` | Preferred main Provider/model | Explicit Web, API, or main `switch_model` selection; startup configuration for a fresh installation |
| `preference` | Confirmation state, provenance, selection time, and legacy candidate hint | Startup migration or explicit selection |
| `last_used` | Last completed execution and attempt history in this process | Actual response; never a startup selection source |
| `health` | Connection/model cooldown, blocking, and recovery verification | Classified successes and failures; never a selection source |

A Web switch validates registration and declared tool support, saves
`LLM__RUNTIME_CONFIG_FILE` with temporary-file replacement, then updates the in-memory preference
and interrupts old calls. If writing or replacement fails, the API returns an error and preserves
the previous preference and in-flight calls; an unsaved choice is never reported as successful.
Temporary Windows file-lock errors allow at most four replacement attempts, with at most 175ms
of added waiting. Persistent denial still fails. These retries add no model requests.

`preference.source` distinguishes `explicit`, `startup`, `legacy_config`, and `unresolved`.
Explicit selection records UTC `selected_at`; temporary fallback never changes these fields.
Reselecting the same model records intent and permits verification without declaring the remote
service healthy.

Runtime model configuration applies after `.env`, process environment, and admin startup settings.
Once saved, the preference survives later upgrades and restarts. A short-term snapshot's
`active_provider`/`active_model` represents historical execution only. Editing startup defaults in
Runtime Settings does not replace a saved preference; use Model Orchestration → Switch model.

`get_context` reports preference and the caller's last actual execution separately. Main,
Bubble, and subconscious tools read their own Brain's record. Before a completed call it shows
“No completed model call” rather than claiming that the preference executed. Temporary fallback
reports the backup, and successful recovery updates the execution record to the preference.

## Legacy configuration and first startup

1. A valid first-run wizard record explicitly requests a model and a clean start: save that
   selection first, without letting an older `preferred` or execution snapshot override it.
   `preference_revision` links the record to the prior preference; `selected_at` identifies this
   selection. Startup interrupted after saving may safely resume. A later explicit selection,
   including reselection of the same model, invalidates the old record. Policy edits and known
   model-ID migrations do not. A legacy wizard record without the link is accepted only when
   no saved preference exists.
2. Saved `preferred`: restore that choice. A saved preference without provenance becomes
   `legacy_config`. A removed connection or a model without declared tool support enters pending
   confirmation, retaining the old choice as a `candidate_source=saved_preference` hint rather
   than substituting a legacy startup default.
3. A genuinely fresh installation with no runtime file, snapshot, audit, or instance state:
   persist a valid startup default. With no available connection, keep the first-run setup flow.
4. An existing installation with no confirmed preference: save `status=pending`,
   `source=unresolved`, and a null `preferred`. Snapshot and successful admin-switch audit records
   supply hints only, including when a candidate connection has been removed.

While confirmation is pending, administration remains accessible. The default `/admin` entry
opens Model Orchestration; explicit Runtime Settings links still allow connection repair.
`GET /api/admin/bootstrap` returns `required=false` and `preference_pending=true`, so the console
does not enter the first-run wizard. `POST /api/admin/bootstrap` returns `409` in this state,
preventing recovery from being treated as a clean initialization. Existing snapshots and identity
remain intact.

Main, Bubble, summary, vision, and long-term model calls are rejected before the SDK; business channels and model-backed
services are deferred. Editing fallbacks, thinking effort, or routing policy does not unlock them.
Stale fallback, summary, or vision references left by removed connections can be repaired in the
console. Confirmation validates these references first: invalid ones return an error and keep
confirmation pending instead of saving a choice that cannot load on the next restart.
An explicit valid main selection saves the preference before requesting one controlled restart
to initialize these services. Subsequent ordinary switches do not need a restart. Corrupt runtime
files or inconsistent confirmation state produce an explicit error and preserve the original
file; they never silently authorize a startup default.

`--check` validates durable configuration and dependencies without migrating preference or
selecting from stale disk snapshots. `restart_self` still saves live context after the check
succeeds. Historical snapshots cannot authorize a selection.

## Routing one request

Candidates are the preference followed by configured fallbacks, deduplicated by exact
Provider/model. Unregistered targets and targets lacking capabilities required by this request
are filtered. Fallbacks are not a connection allowlist. Temporary failures never rewrite the
preference or notify persistent-switch observers; responses, usage, and source metadata identify
the actual execution target.

One logical request shares an attempt budget and deadline, including rerouting after explicit
switches. The router reserves opportunities and time for later candidates. SDK generation retries
are disabled to prevent retry multiplication. Programming errors still propagate rather than
being disguised as Provider outages.

| Error category | Current request | Later requests |
|---|---|---|
| `transient` / `invalid_response` | Retry within budget, then try backups | Cool down after the logical-failure threshold |
| `rate_limit` | Try backups without waiting out the whole rate-limit window | Honor server `Retry-After`, otherwise the rate-limit cooldown |
| `quota` | Try backups | Honor the quota reset time, otherwise the quota cooldown |
| `authentication` / `permission` / `model_unavailable` | Try other allowed candidates | Block the affected connection or model until configuration changes or explicit reselection |
| `content_policy` / `context_length` / `invalid_request` | Return the rejection; do not forward unchanged to backups | Do not count as an outage; main waits for a new event or configuration change |
| `unknown` | No blind retry on the same target; backups may be tried | Do not open a breaker based on an unknown cause |

Structured vendor codes precede HTTP/transport metadata. Refusal and failure finish reasons in
HTTP 200 responses are also classified. Routing errors retain limited codes, status, request
identifiers, and numeric usage rather than full request or error bodies. Connection-wide
authentication/quota failures may affect multiple models on that connection; model-level
failures do not block the whole connection.

## Cooldown and automatic recovery

Health transitions through `closed` (normal), `open` (cooling), and `half_open` (verification).
Failures requiring attention use `blocked`. Transient thresholds count failed logical requests,
not every internal retry.

Cooldown expiry alone sends no request. The next real business request acquires one recovery
permit and tries the preference. Concurrent callers sharing the fault scope use backups or wait
rather than probing together. The first successful request already uses the preferred response;
two consecutive successes by default restore normal concurrency. Failed verification extends
cooldown. Cancellation, suspension, and exceptional exit release the permit.

Health persists in `MEMORY__DB_PATH/model_routing_state.json`; restarts respect cooldowns and
blocks. A connection-type, key, or endpoint change changes its digest so old health cannot poison
the replacement; the digest contains no raw key. In-flight verification does not survive a
restart: it is retried on demand after cooldown. Corrupt health produces a warning and on-demand
revalidation without changing preference. There are no scheduled billable probes.

## Defaults and configuration

`LLM__ROUTING` accepts a JSON object. Both `PATCH /api/admin/model` and `PATCH /model_config`
accept partial `routing` updates; omitted fields keep their values. A saved runtime policy wins
over environment configuration. The admin Automatic recovery policy panel hot-updates common
limits and shows cooldown/block details.

`max_cooldown_seconds` must be greater than or equal to `cooldown_seconds`. Web exposes both
initial and maximum cooldowns and prevents saving invalid combinations. Partial API patches merge
with the current policy before validation; failure preserves in-memory and saved configuration.
A 1200-second initial cooldown is therefore not silently capped at a hidden 900-second maximum:
set the maximum to at least 1200 seconds as well. This bound applies to exponential transient
cooldown; it does not shorten quota/rate-limit cooldowns or explicit server wait times.

| Field | Default |
|---|---:|
| `attempts_per_target` | 2, including the first attempt |
| `max_attempts` | 4 per logical request |
| `attempt_timeout_seconds` | 90 |
| `request_timeout_seconds` | 180 |
| `retry_delay_seconds` | 1, with exponential backoff and jitter |
| `failure_threshold` | 3 |
| `failure_window_seconds` | 60 |
| `cooldown_seconds` | 60 |
| `max_cooldown_seconds` | 900 for transient cooldown; never shortens an explicit server wait |
| `rate_limit_cooldown_seconds` | 30 |
| `quota_cooldown_seconds` | 1800 |
| `recovery_successes` | 2 |

## Main, Bubbles, and background work

| Call | Preference and fallback | Explicit switch or pause |
|---|---|---|
| Main | Saved preference + ordered fallbacks | Switching aborts the old route and reroutes within the original budget; pause aborts and parks |
| Bubble without an explicit target / subconscious | Follow main preference and policy; share health | Main controls do not cancel an independent child Brain's in-flight work; its next call follows the new preference |
| Bubble with an explicit target | Keep its own target; do not expand to main fallbacks | A child's selection never rewrites the persisted main preference |
| Summary/background compression following main | Main preference and fallbacks | Participates in switching/suspension when main-owned, retaining retryable original memory |
| Explicit summary / vision | Fixed targets, shared relevant health, no implicit backups | Unaffected by main switch or suspension |
| mem0 long-term extraction | Follow main unless a specialist is explicitly selected | Synchronous worker threads share routing health and limits without holding an async lock during network I/O |

Provider model selection is request-local, so main and background roles can use different models
on one connection concurrently. Provider outages do not trigger destructive main-context reset.
Failed background work retains its existing task-level handling; route recovery does not promise
automatic replay of every failed task.

## Compatibility and validation

API snapshots retain `active` as a compatibility view of the preference and add `preferred`,
`preference`, `last_used`, `routing`, and `health`. Clients expecting successful fallback to
permanently change `active` should read the actual response or `last_used`. Only admin switches
resolving legacy pending state return `restart_required=true`; ordinary switches remain hot.

Back up runtime configuration and data before upgrading. Older strict schemas reject unknown
fields; rolling back code requires restoring a compatible configuration backup. The old format
cannot represent the new preference or policy. A previously incorrect saved `preferred` cannot
be reliably inferred from execution history; an administrator must reselect it.

Regression coverage includes startup precedence, legacy confirmation, unavailable-connection
repair, repeated and stale wizard recovery, failed writes, restart recovery, error classification,
concurrent cooldown, merged-policy validation, actual-model reporting, real-request recovery,
Bubble/subconscious/mem0 routing, usage attribution, switch/pause races, and background compression.
Browser tests cover pending confirmation, policy repair, and explicit selection in both console
languages. Tests use fake
Provider/SDK responses rather than real billable models. See the
[Development Guide](../development/development.en.md) for validation entry points.
