# Spec: Monitoring gap closure (five slices)

- **Status:** Draft — awaiting owner review
- **Owner:** project owner (ta.khongsap)
- **Date:** 2026-10-01
- **Risk tier:** R1 — the monitor advises humans; nothing here acts on a model, deletes
  producer data, or changes the telemetry contract
- **Intent:** [intent.md](intent.md)

This spec covers five slices that land as five PRs, in this order. Each slice has its
own "must do" and verification so a reader can stop after any of them.

| Slice | Name | Tier |
|---|---|---|
| A | Playbook baseline and `postMerge` fix | MUST |
| B | Monitoring correctness: `count=0` and label-lag backfill | MUST |
| C | Alerting: transition state machine, API, webhook, UI | MUST |
| D | Operational resilience | SHOULD |
| E | Repository hygiene and contract strictness | NICE |

## What it must do

### A. Playbook baseline

1. Add `CLAUDE.md` (≤120 lines, navigational, from the playbook `project-agents` template)
   and symlink-free `AGENTS.md` that says "see CLAUDE.md" so Codex-style tools find it.
2. Add `README.md` from the playbook `project-readme` template. `replit.md` shrinks to a
   Replit-specific pointer (secrets list, deploy commands) and loses its two wrong claims
   (`cd frontend`, "runs migrations").
3. Add `CHANGELOG.md` (Keep a Changelog), `DEVLOG.md` (template sections; first entry is
   this audit; Known gaps lists everything in slices B–E), and `TESTING.md`.
4. Change `scripts/post-merge.sh` so it no longer runs `pnpm --filter db push`. It keeps
   `pnpm install --frozen-lockfile`.
5. Link every new file from `README.md` ("Repository guide").
6. Non-goal: no ADR directory yet; the first ADR arrives with slice C (alert ownership).

### B. Monitoring correctness

1. **Empty window is a real observation.** When `/telemetry/inferences?tick=t` returns
   `count=0`, the runner stores an observation for the window with `record_count=0`, all
   signals `None` with reason `"empty window"`, Performance/Drift lanes Unknown, and the
   cursor advances. No `errors["telemetry"]`. Applies to churn (L01) and NBA (L03).
2. **Label-lag backfill.** After observing tick *t* (and while waiting at the tail), the
   runner also attempts realized metrics for every tick *u* in `[t - L - 1, t)` that is
   persisted but not yet final,
   where *L* = `label_lag_ticks` (ML) or `reward_lag_ticks` (NBA) from `/telemetry/meta`,
   falling back to 3 when absent. For each *u* it pulls `/telemetry/labels?tick=u`
   (or `/telemetry/rewards`), joins on `inference_id` to the inferences re-pulled for *u*
   and verified against the stored `content_sha256`, and stores the result in a new table
   `live_realized_metrics(source_id, tick, metric_key, value, coverage, status,
   computed_at)` with `status ∈ {realized, pending, insufficient_coverage, single_class,
   no_labels, evicted, error}`. A tick is "done" once `status == realized` or the label window reports
   `available_at_tick` ≤ current source tick (the producer's latest closed tick,
   `latest_tick - 1`) and still has no labels (then `no_labels`).
3. **Immutability preserved.** `live_observations.payload` and its digest are never
   rewritten. Realized metrics live only in the new table; realized sparklines for
   `realized_roc_auc` / `acceptance_rate` are served from it (`db.realized_history`),
   not from `live_signal_history`, whose `(observation_id, signal_key)` row already
   holds the pending value stored with the observation. *(Amended during slice B
   implementation; see plan Deviations.)*
4. **Grading uses the latest realized value.** `/api/live/use-case/{uc}` and the
   portfolio rollup take `realized_roc_auc` (and NBA `acceptance_rate`) from the most
   recent tick with `status == realized`, label it `as_of_tick`, and grade it with the
   existing bands. If none exists the lane stays reasoned-Unknown with the current reason
   text.
5. Non-goals: per-use-case thresholds, LIME in production, sampling policy §14.

### C. Alerting

1. **State machine.** After each committed observation, compute `overall` and per-lane
   health. Persist transitions in `live_alerts(alert_id, source_id, lane, from_health,
   to_health, tick, observation_id, opened_at, resolved_at, delivered_at,
   delivery_error, dedupe_key)`. An alert opens on `* → Red` and on `Green → Amber`,
   resolves when the same lane returns to Green, and is deduplicated on
   `(source_id, lane, to_health)` while open. A current Unknown never opens or resolves;
   a previous Unknown followed by Red opens (it is a `* → Red`). *(Amended during
   slice C: the shipped table splits delivery into `open_` / `resolve_` status, error and
   delivered-at columns plus `resolved_tick`, and has no `dedupe_key` column — dedupe is
   computed from open rows. See plan Deviations C1/C2.)*
2. **API.** `GET /api/live/alerts?uc=&open=true&limit=` on the strict router, redacted
   like other live routes. `actions` in the detail payload is replaced by `alerts`
   (open alerts for that use case).
3. **Delivery.** If `LIVE_ALERT_WEBHOOK_URL` is set, POST a JSON body
   `{"text": "...", "blocks": [...] , "alert": {...}}` (Slack-incoming-webhook
   compatible, also fine for any generic receiver) on open and on resolve. Delivery is
   at-least-once with the retry policy from slice D (until D lands: one attempt, failure
   recorded in `delivery_error`, retried next cycle). Delivery never blocks grading.
4. **UI.** An "Alerts" strip on the home page (open count by severity, newest five) and
   an "Alerts" panel on the use-case page (open and last 20 resolved). Read-only; no
   acknowledge workflow in this slice.
5. **ADR 0001** records who owns alert triage (resolves contract §18 Q1 for this
   repository: the RAI team via the webhook channel; producers are not paged).
6. Non-goals: paging escalation, SLA timers, Slack bot, email.

### D. Operational resilience

1. **HTTP retry.** `telemetry_http.pull` and the ack/write-back paths retry idempotent
   GETs and acks on 429/502/503/504 and connection errors: 3 attempts, exponential
   backoff 0.5 s → 4 s with jitter, honouring `Retry-After`. 4xx other than 429 are not
   retried.
2. **Structured logging.** Replace `print` in the live plane with `logging` (JSON lines
   when `LOG_FORMAT=json`). Each cycle logs `cycle_id, source_id, tick, duration_ms,
   outcome, backlog`. `/api/readiness` exposes the last cycle's duration, outcome and
   the last error per source.
3. **Operator unstick.** `POST /api/live/sources/{uc}/skip` (worker token) marks the held
   tick as `skipped` with a reason, writes an observation stub (`record_count=0`,
   `skipped=true`, reason) so the window is auditable, and advances the cursor. Also
   `POST /api/live/sources/{uc}/reset-ack` to clear a poisoned pending ack.
4. **NBA baseline persistence.** The baseline offer mix per `(source_id, model_version)`
   is stored in a new table `live_baselines` and read on cold start, so
   `recommendation_drift` survives Autoscale restarts.
5. Non-goals: metrics export (Prometheus), tracing, CBPE baseline persistence (it
   refits from the artifact and is acceptable to recompute).

### E. Hygiene and contract strictness

1. Delete `.migration-backup/`, `artifacts/api-server/src` TS stub (keep the Replit
   `artifact.toml` wrapper), `lib/api-spec`, `lib/api-zod`, `lib/api-client-react`,
   `lib/db`, `artifacts/mockup-sandbox`, `backend/fly.toml`, `backend/Dockerfile`,
   `scripts/src/hello.ts`, the `@replit/connectors-sdk` dependency and unused frontend
   dependencies (`@tanstack/react-query`, `@workspace/api-client-react`). Update
   `pnpm-workspace.yaml`, root `package.json` scripts, and both CI workflows.
2. Validate `contract_version` on every pulled window: accept `"1.1"` and `"1.0"`,
   record `errors["contract"]` and hold the cursor otherwise.
3. Never default `latency_s` to 0.0: traces without it are excluded from the latency
   signal and counted in `latency_missing`.
4. Enforce HTTPS for producer URLs in strict live unless `ALLOW_INSECURE_LIVE_TESTING=1`.
5. Fix the judge-model docstring in `llm_eval/live_http.py`.

## Current state and gaps

| Control | State | Evidence |
|---|---|---|
| Playbook documents (README/AGENTS/CHANGELOG/DEVLOG/TESTING) | not met | none exist at repo root |
| `count=0` handled as observation (contract §6) | not met | `ml_monitor/live_http.py:142-146`, `live_runner.py:76-85` |
| Realized metrics with label lag (contract §7) | not met at tail | labels pulled only for tick *t*, `ml_monitor/live_http.py:202` |
| Alert on Red | not met | `live_portfolio.py:203,242` hard-codes `actions: []` |
| HTTP retry/backoff (contract §12) | not met | `telemetry_http.py:48-54` plain `httpx.get` |
| Operator path for a held cursor | not met | `db.clear_live_state` not routed |
| Postgres lease, digest-before-cursor, ack retry | met | `test_live_persistence.py`, `test_live_release_hardening.py` |
| CI runs backend tests against Postgres | met | `.github/workflows/backend-live.yml` |
| `postMerge` safe against backend DB | not met | `scripts/post-merge.sh` runs `drizzle-kit push` with empty schema |

## Contracts

| Contract | This change |
|---|---|
| User | Operators see realized performance once labels arrive, are notified on Red/Amber transitions, and can skip a poisoned window. On webhook failure the alert is still visible in the UI and API. |
| Capability | Monitor-side consumer of contract v1.1; no new producer obligations. Stated limits: alerts are advisory, LIME remains off in production, sampling policy §14 still uniform. |
| Data | Telemetry already classified by §13. New tables hold derived metrics and alert metadata only (no raw records). Webhook payload carries use case id, lane, health, tick, and the dashboard URL — no features, no trace text. |
| Tool | Outbound HTTPS POST to one operator-configured webhook; outbound GET/POST to producer endpoints (existing). Side-effect class: notification. |
| Evaluation | Deterministic pytest with fake producer servers; no LLM eval changes. |
| Operations | Same Autoscale deployment. New env: `LIVE_ALERT_WEBHOOK_URL` (optional), `LOG_FORMAT` (optional). Rollback: revert the PR; new tables are additive and ignored by the previous version. |

## Design

```
poller cycle
  └─ for uc in LIVE_UCS
       ├─ runner.tick()            observe closed tick t  (existing)
       │     └─ _commit_tick       count=0 → observation, cursor advances   [B1]
       ├─ runner.backfill_labels() ticks [t-L, t) → live_realized_metrics   [B2]
       ├─ alerts.evaluate(uc)      diff previous vs current health → live_alerts [C1]
       └─ alerts.deliver_pending() webhook POST, at-least-once           [C3,D1]
  readiness ← cycle stats, per-source last error                         [D2]

API (strict router)
  GET  /live/use-case/{uc}   + realized as_of_tick, alerts[]              [B4,C2]
  GET  /live/alerts                                                      [C2]
  POST /live/sources/{uc}/skip, /reset-ack   (worker token)              [D3]
```

Trust boundaries are unchanged: the browser is read-only; worker-token routes are the
only mutating surface; the webhook URL is a secret and never reaches the bundle (the
strict-live bundle check gains the string `LIVE_ALERT_WEBHOOK_URL`).

Module placement: `backend/app/engines/alerts.py` (pure state machine, testable without
DB), `backend/app/alert_delivery.py` (webhook), `backend/app/label_backfill.py`,
`backend/app/http_retry.py`. Runners call these; adapters stay adapter-only.

## Flagged concerns

| Concern | Raised by | Severity | Resolution | Accepted by |
|---|---|---|---|---|
| Backfill re-pulls inferences for old ticks; producers may evict windows (404). | spec author | Material | Treat 404 on backfill as `status=evicted`, never hold the cursor; surfaced in detail payload. | pending |
| Re-grading an old tick could change history shown in sparklines. | spec author | Minor | Realized values live only in `live_realized_metrics` and are read from there; `live_signal_history` rows are never updated or appended to by the backfill. | pending |
| Webhook secret in logs. | spec author | Material | Log the host only; never the full URL or body. Test asserts it. | pending |
| Slice E deletes the TS API stub that Replit's `artifact.toml` references by directory. | spec author | Material | Keep `artifacts/api-server/artifact.toml` and `package.json` with no `src`; verify `pnpm run typecheck` and `deploy-build.sh` still pass. | pending |
| Two Autoscale instances could both run backfill. | spec author | Minor | Backfill runs inside the existing lease-held cycle; `live_realized_metrics` has a unique key on `(source_id, tick, metric_key)`. | pending |

## Alternatives considered

- **Re-observe old ticks entirely** (recompute drift/CBPE too) — rejected: violates
  observation immutability and doubles engine cost; only labels change over time.
- **Alert from the frontend** on polling diffs — rejected: browser presence must never
  own monitoring behaviour (strict-live invariant).
- **Slack SDK** — rejected for now: a webhook needs no token management and covers the
  pilot; the payload is Slack-compatible so the upgrade path is open.
- **Alembic** for the new tables — deferred: the in-code migration list with advisory lock
  already works and is tested; three additive tables do not justify a new tool.

## How it will be verified

All deterministic; run from `backend/` with `.venv/bin/python -m pytest tests`.

- **A:** playbook `scripts/check_docs.py` logic reproduced as `tests/test_docs.py`
  (required files exist, one H1, ≤120 lines in CLAUDE.md, no `/Users/` paths, links
  resolve). `post-merge.sh` no longer contains `db push` (grep in test).
- **B:** fake producer (httpx `MockTransport`) serving: a `count=0` tick → observation
  stored, cursor advances; labels appearing two ticks later → `live_realized_metrics`
  row `realized`, detail payload shows `as_of_tick`; coverage 40% → `insufficient_coverage`;
  404 on backfill → `evicted` and cursor untouched; stored observation payload/digest byte-identical before and after backfill.
- **C:** state machine unit tests (Green→Amber opens, Amber→Red opens new, Red→Green
  resolves both, Unknown never alerts, dedupe); webhook delivery test with
  `MockTransport` asserting payload shape and that a 500 records `delivery_error` and
  retries next cycle; route test for `/api/live/alerts` redaction and filtering;
  bundle check rejects `LIVE_ALERT_WEBHOOK_URL`.
- **D:** retry tests (429 with `Retry-After`, 503 ×2 then 200, 404 not retried);
  skip endpoint requires worker token, writes stub observation, advances cursor;
  NBA baseline survives a new runner instance.
- **E:** `pnpm run typecheck`, `pnpm run build:live`, `pnpm run check:strict-live` pass
  after deletions; contract tests for version rejection, `latency_s` missing, and
  `http://` rejection in strict mode.
- **Every slice:** full backend suite green, both CI workflows green on the PR.

Unavailable in this environment and reported as such: a real producer, Langfuse, and a
live Claude judge call. They are not required by any test above.

## Open questions carried forward

- Alert triage ownership (intent): resolved for this repo by ADR 0001 in slice C; the
  contract-level question §18 Q1 stays open with the contract owners.
