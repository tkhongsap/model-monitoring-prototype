# Changelog

Notable user- and operator-visible changes are recorded here. Development notes
and evidence belong in [DEVLOG.md](DEVLOG.md); decision rationale belongs in ADRs under
[docs/adr/](docs/adr/README.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Every pulled telemetry window must carry `contract_version` `"1.0"` or `"1.1"`;
  any other value (or a missing field) is a `ContractVersionError`, recorded as the
  window's telemetry error, and holds the source cursor. `/telemetry/meta` is checked
  only when it carries the field (spec E.2).
- Strict live mode requires `https://` for `LIVE_CHURN_URL`, `LIVE_CHATBOT_URL`,
  `LIVE_NBA_URL` and `LIVE_PRODUCER_URL`; `GET /api/readiness` lists
  `<NAME> must use https in strict live mode` otherwise. `ALLOW_INSECURE_LIVE_TESTING=1`
  remains the only bypass (spec E.4).
- Root `pnpm run typecheck` now runs the `artifacts/*` packages only (the `tsc --build`
  over `lib/*` is gone with the libraries).

### Deprecated or removed

- Dead scaffold deleted (spec E.1): `.migration-backup/`, `lib/api-spec`, `lib/api-zod`,
  `lib/api-client-react`, `lib/db`, `artifacts/mockup-sandbox`, the TypeScript stub under
  `artifacts/api-server/src` (plus its `build.mjs` / `tsconfig.json`; the Replit
  `artifact.toml` wrapper and a minimal `package.json` stay), `backend/fly.toml`,
  `backend/Dockerfile`, `backend/.dockerignore`, `scripts/src/hello.ts` and
  `scripts/tsconfig.json`.
- Unused npm dependencies removed: `@replit/connectors-sdk`, `@tanstack/react-query`,
  `@workspace/api-client-react`, the `drizzle-orm` / `tsx` catalog entries and the
  `@expo/ngrok-bin` platform overrides. `pnpm-lock.yaml` regenerated.

### Added

- Operational resilience (spec D). Producer pulls, acknowledgements, score write-back
  and the alert webhook retry on 429/502/503/504 and connection errors: three attempts,
  exponential backoff 0.5 s → 4 s with jitter, `Retry-After` honoured; other 4xx are
  never retried (`backend/app/http_retry.py`, contract §12).
- Structured logging: `LOG_FORMAT=json` emits one JSON object per line; every poll
  cycle logs `cycle_id, source_id, tick, duration_ms, outcome, backlog` per source, and
  `GET /api/readiness` exposes the last cycle (duration, outcome, last error per source)
  under `poller.last_cycle`. Startup `print`s are gone.
- Operator unstick: `POST /api/live/sources/{uc}/skip` (worker token, `{"reason"}`)
  skips the tick a source is held on — only when the cursor state is `error` (409
  otherwise) — writing an auditable stub observation (`record_count=0`, `skipped=true`,
  reason, `ack_status=skipped` so it is never acknowledged to the producer), finalising
  the tick's realized rows and advancing the cursor in one transaction;
  `POST /api/live/sources/{uc}/reset-ack` abandons a poisoned pending acknowledgement. Every other POST under `/api/live/` is still 404.
  Runbook: `docs/STRICT-LIVE.md` "Unsticking a source".
- The NBA baseline offer mix is persisted per `(source, model_version)` in the new
  `live_baselines` table (migration 7) and read on cold start, so
  `recommendation_drift` survives Autoscale restarts.
- Live alerting (spec C). After every poll cycle each use case's graded health
  (including lagged realized metrics) is diffed against its last snapshot: `* → Red` and
  `Green → Amber` open an alert, a return to Green resolves it, a current Unknown never
  opens or resolves, and
  an open `(lane, health)` is never duplicated. Alerts live in the new `live_alerts`
  table (migration 6, with `live_health_snapshots`).
- `GET /api/live/alerts?uc=&open=&limit=` on the strict router (read-only, derived
  columns only); the live use-case detail carries `alerts` (its open alerts) instead of
  the demo-era `actions: []`; the portfolio summary reports `open_alerts` by severity.
- Webhook delivery: when `LIVE_ALERT_WEBHOOK_URL` is set, each opened and resolved alert
  is POSTed once as a Slack-incoming-webhook-compatible `{text, blocks, alert}` body
  (use case, lane, health, tick, dashboard link from `LIVE_DASHBOARD_URL`; no features,
  no trace text). A failed POST is recorded on the alert and retried next cycle; logs name
  the webhook host only. The strict-live bundle check rejects the secret name.
- Dashboard: an "Alerts" strip on the home page (open Red/Amber counts, newest five) and
  an "Alerts" panel on each use-case page (open and the last 20 resolved). Read-only.
- `docs/adr/0001-alert-ownership.md`: the RAI team owns alert triage via the webhook
  channel; producers are not paged.

- Playbook baseline documents: `README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`,
  `DEVLOG.md`, `TESTING.md`.
- `backend/tests/test_docs.py` enforces the documentation contract (required files,
  one H1, `CLAUDE.md` within 120 lines, internal links resolve, no machine-local paths,
  `postMerge` hook never pushes a schema).

### Fixed

- A chatbot trace without `latency_s` is no longer read as 0.0 seconds: it is excluded
  from `p95_latency_s` and counted in the observation's `latency_missing`; its stored
  and sampled latency is `null` (spec E.3). The judge docstring in
  `backend/app/adapters/llm_eval/live_http.py` now names the configured default model
  (`claude-haiku-4-5`) instead of a wrong hard-coded id (spec E.5).
- A closed producer window with `count=0` is stored as an observation (every signal
  Unknown, reason "empty window") and the source cursor advances; it no longer holds the
  cursor as a telemetry error (contract §6).
- Realized ROC-AUC and NBA acceptance rate are computed once lagged labels arrive: the
  poller backfills the ticks within `label_lag_ticks` / `reward_lag_ticks` into the new
  `live_realized_metrics` table (migrations 4 and 5), and the live use-case detail,
  portfolio and summary grade the Performance and Feedback lanes on the latest realized
  value with an `as_of_tick` (contract §7). A tick whose labels never arrive is final
  `no_labels` once the producer's `available_at_tick` is at or before its latest closed
  tick (`latest_tick - 1`). Stored observations are never rewritten.
- The Replit `postMerge` hook no longer runs `pnpm --filter db push`, which pushed an
  empty Drizzle schema at the backend's PostgreSQL database. It only reinstalls
  workspace dependencies; the backend migrates itself at startup.
- `replit.md` no longer tells operators to `cd frontend` (that directory is not a
  workspace package) or that the deploy build "runs migrations".
