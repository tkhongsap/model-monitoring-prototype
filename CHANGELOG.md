# Changelog

Notable user- and operator-visible changes are recorded here. Development notes
and evidence belong in [DEVLOG.md](DEVLOG.md); decision rationale belongs in ADRs under
[docs/adr/](docs/adr/README.md).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
