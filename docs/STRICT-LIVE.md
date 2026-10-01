# Strict live control tower

Set `CONTROL_TOWER_MODE=live` for the deployed monitor. In this mode the backend does
not bake or reset `DEMO-FULL`, does not seed the simulated registry, and returns 404 for
scenario, simulation, export, baked artifact, and browser-driven tick routes.

## Required deployment configuration

- `DATABASE_URL`: managed PostgreSQL; SQLite is rejected by readiness.
- `LIVE_CHURN_URL`, `LIVE_CHATBOT_URL`, `LIVE_NBA_URL`: external telemetry service URLs.
- `LIVE_PRODUCER_URL`: portfolio gateway used for observation acknowledgements.
- `LIVE_TELEMETRY_TOKEN`: shared bearer token used for pulls, score write-back, and ack.
- `LIVE_WORKER_TOKEN`: dedicated bearer token for `POST /api/live/poll`. Set the same
  value as GitHub Actions secret `MONITOR_WORKER_TOKEN` (never expose it to the SPA).
- `ANTHROPIC_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`.
- `LIVE_POLL_SECONDS` greater than zero; `LIVE_POLL_LEASE_SECONDS` defaults to 30.

`GET /api/readiness` returns HTTP 503 and `not_ready` until the durable database,
external URLs, tokens, poller, and judge configuration are present. Health endpoints
also return 503 for an invalid strict-live configuration. `ALLOW_INSECURE_LIVE_TESTING=1` is the only
bypass and is intended solely for isolated automated tests.

## Runtime behavior

The producer remains a Reserved VM. The monitor is Autoscale, with cursors,
observations, history, leases, and live artifact bytes in a separate monitor-owned
PostgreSQL database. Because Autoscale may scale to zero, `.github/workflows/autoscale-poll.yml`
wakes the monitor every five minutes and waits for an authenticated, lease-protected
`POST /api/live/poll` cycle. A process-local poller improves latency while an instance is
warm; neither browsers nor GET health checks advance monitoring state. A heartbeat
renews lease ownership while Evidently, SHAP, or Claude work is in flight.
Each observation and its signal history are committed before the source cursor advances.
Duplicate `window_id` values are idempotent; the same id with a different
`content_sha256` is an integrity error and holds the cursor. The monitor independently
recomputes that digest from the exact public primary-record JSON before grading. Failed
producer acknowledgements remain durable and are retried on later lease cycles.

Closed churn/NBA windows below 500 records and chatbot windows below 8 traces are
persisted as real observations, but statistical health stays `Unknown` with an explicit
insufficient-sample reason. A closed window with `count=0` is likewise a real observation
(every signal `Unknown`, reason "empty window") and advances the cursor; only a missing
window (HTTP 404) or an integrity error holds it. Public observations omit raw chat
input/output and per-instance LIME values. Live artifact bytes are served from PostgreSQL,
not an Autoscale filesystem; per-instance LIME HTML is not public.

## Label-lag backfill

Labels (churn) and rewards (NBA) arrive `label_lag_ticks` / `reward_lag_ticks` after a
window closes (contract §7; the monitor reads the lag from `/telemetry/meta`, default 3).
The observation for tick *t* therefore records realized metrics as `pending`. Inside the
same lease-held cycle, after observing *t* and while waiting at the tail, each runner
revisits the ticks in `[t - L - 1, t)`, re-pulls their inferences, verifies the digest
against the stored observation, joins the labels and writes one row per
`(source_id, tick, metric_key)` to `live_realized_metrics` with status `realized`,
`pending`, `insufficient_coverage` (below 50 %), `single_class`, `no_labels`, `evicted`
(the inference window answered 404; final) or `error` (digest changed; retried). A tick
is done (`final`) once it is `realized` or `evicted`, or once the labels window's
`available_at_tick` is at or before the producer's source tick — its latest closed tick,
`latest_tick - 1`, never the open window it is still writing — and still carries no
labels: then it is final `no_labels` and never pulled again, even if labels appear
later. While the monitor waits at the tail its own tick equals the open window, so
nothing due in that window is finalized until it closes. A 404 on the labels window,
or an empty one without `available_at_tick`, is `pending` until the tick's own due tick
(`t + L`) is at or before the source tick. A `count=0` or undersized inference window
is final `no_labels` at once (no label can realize it). `pending` rows that slipped
below the window during a producer outage are swept once more. Final rows are never overwritten, and the backfill never
rewrites an observation or touches the cursor. `GET /api/live/use-case/{uc}` and the
portfolio grade `realized_roc_auc` and `acceptance_rate` on the most recent `realized`
tick, reported as `as_of_tick`; with no realized row the lane stays reasoned-Unknown with
its pending reason.

Sync states are `connecting`, `catching_up`, `at_tail`, `idle`, `stale`, and `error`.
Inspect them through `GET /api/live/sync`, the live portfolio response, or a use-case
detail response. Live observation evidence is under `/api/live/observations` and live
artifacts are served only from `/api/live/artifacts/{artifact_id}`.

Run `python backend/scripts/migrate.py` (or start the service) to apply ordered schema
migrations. PostgreSQL runs them under an advisory transaction lock, so concurrent
Autoscale starts are safe. `GET /api/version` reports both the baked monitor Git SHA and
the producer gateway Git SHA for direct comparison with their respective GitHub repos.
