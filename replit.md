# Model Monitoring Prototype — Replit notes

Strict-live AI observability control tower for the `ai-use-cases` producer. The monitor
pulls authenticated telemetry, persists cursors and observations in its own PostgreSQL
database, evaluates real closed windows, and acknowledges matching window digests back
to the producer. Production never serves baked scenario data.

Project documentation lives in [README.md](README.md); agent instructions in
[CLAUDE.md](CLAUDE.md). Deployment safety and exact URL mapping are in
[docs/STRICT-LIVE.md](docs/STRICT-LIVE.md) and [docs/LIVE-DEMO.md](docs/LIVE-DEMO.md).

## Run and operate

- `bash scripts/deploy-build.sh` — install workspace dependencies, build the strict-live
  SPA, verify the bundle, and build the backend virtualenv. It does not run migrations.
- `bash scripts/deploy-run.sh` — start the single-port strict-live deployment. The backend
  applies its ordered schema migrations at startup under an advisory lock.
- `scripts/post-merge.sh` (the `.replit` `postMerge` hook) only runs
  `pnpm install --frozen-lockfile`; it never pushes a schema at the backend database.
- Local checks are listed in [TESTING.md](TESTING.md).

The root `.replit` publishes an Autoscale deployment. `scripts/deploy-run.sh` always sets
`CONTROL_TOWER_MODE=live`; local demo mode must use a separate development command.

## Required Replit Secrets

- `DATABASE_URL` — monitor-owned managed PostgreSQL, never the producer database
- `LIVE_CHURN_URL`, `LIVE_CHATBOT_URL`, `LIVE_NBA_URL` — producer gateway service prefixes
- `LIVE_PRODUCER_URL` — producer gateway root for version checks and acknowledgements
- `LIVE_TELEMETRY_TOKEN` — same value as producer `RAI_TELEMETRY_TOKEN`
- `LIVE_WORKER_TOKEN` — same value as GitHub secret `MONITOR_WORKER_TOKEN`
- `ANTHROPIC_API_KEY` — real Claude judge
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- `LIVE_POLL_SECONDS` — positive warm-instance polling interval

Optional secrets:

- `LIVE_ALERT_WEBHOOK_URL` — Slack incoming webhook (or any JSON receiver) that gets one
  POST per opened and resolved alert; treat as a credential. Unset: alerts are recorded
  and shown but not delivered.
- `LIVE_DASHBOARD_URL` — public origin of the deployed dashboard, linked from each
  alert notification.

`.env` files are excluded from Replit deployment images. Use Replit Secrets for runtime
credentials. `ALLOW_INSECURE_LIVE_TESTING` must remain unset in production.
