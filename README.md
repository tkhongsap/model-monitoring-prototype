# Model Monitoring Prototype

A strict-live Responsible-AI control tower that lets the RAI team see whether three
production models (churn, support chatbot, next-best-action) are Green, Amber or Red,
graded from real closed telemetry windows rather than a demo scenario.

## First success

**Prerequisites:** Python 3.12 and [`uv`](https://docs.astral.sh/uv/); Node 24 with
corepack (`corepack enable && corepack prepare pnpm@10.15.1 --activate`). No producer,
database server, or API key is needed for the local path below.
**Working directory:** `backend/` for the first three commands, repository root for the rest.

```bash
cd backend
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/python -m pytest -q -m "not slow"
cd ..
pnpm install --frozen-lockfile
pnpm run typecheck
```

Expected result: pytest reports all tests passed with `9 deselected` (the deselected
tests are the slow DEMO-FULL bakes; exact counts live in `DEVLOG.md`) and
`pnpm run typecheck` ends with `Done` for every package.

To see the API, from `backend/` run
`CONTROL_TOWER_MODE=demo LIVE_POLL_SECONDS=0 .venv/bin/python -m uvicorn app.main:app --port 8000`
and open `http://127.0.0.1:8000/api/readiness`. It returns HTTP 200 with
`{"status": "ready", "database": {"ok": true, ...}, "mode": "demo"}`. In strict live
mode the same endpoint returns 503 (`not_ready`, or `degraded` when only the judge key
is missing) until the configuration in [docs/STRICT-LIVE.md](docs/STRICT-LIVE.md) is
present.

## Scope

- Users: the RAI team operating the monitor; producer teams read the dashboard.
- Owner: project owner (ta.khongsap).
- Risk tier: R1 — assisted internal workflow; the monitor informs humans and never acts on
  a model ([intent](changes/2026-10-01-monitoring-gap-closure/intent.md)).
- In scope: pull-based consumption of telemetry contract v1.1 for ML, LLM and NBA lanes;
  drift, label-free performance estimation, realized performance, LLM-as-judge scoring;
  durable observations with digest verification; a read-only redacted dashboard.
- Out of scope: acting on models (retrain, roll back), paging or escalation, push ingest,
  per-use-case thresholds, LIME in production, non-uniform sampling policy.

## Architecture

```
ai-use-cases producer (Reserved VM)              model-monitoring-prototype (Autoscale)
  /telemetry/meta | inferences | labels |  <--pull--  live_poller (Postgres lease, one worker)
  reference | traces | rewards | /model/artifact        └─ live_runner per use case
  POST /api/sync/observed  <--ack--                          ├─ adapters/telemetry_http  (HTTP, digest check)
                                                              ├─ adapters/ml_monitor      (Evidently drift, NannyML CBPE,
                                                              │                            realized AUC, NBA offer mix)
                                                              ├─ adapters/llm_eval        (Claude judge, Langfuse write-back)
                                                              ├─ adapters/explain         (LIME/SHAP, off in production)
                                                              └─ engines/health           (pure Green/Amber/Red rollup)
                                                                    │ durable write, then cursor advance
                                                              PostgreSQL (observations, signal history,
                                                                          cursors, leases, artifact bytes)
                                                                    │
                                                              FastAPI /api/live/*  ──►  React SPA (read-only)
```

Sources of truth: [docs/MONITORING-CONTRACT.md](docs/MONITORING-CONTRACT.md) for the
telemetry contract; `backend/app/db.py` for the schema (ordered in-code migrations);
`backend/app/engines/health.py` for the grading bands.

Trust boundaries: the browser is read-only and never advances cursors. Mutating routes
are limited to the worker-token `POST /api/live/poll`. Telemetry pulls and
acknowledgements use `LIVE_TELEMETRY_TOKEN`; the SPA bundle is checked for leaked
demo identifiers and secret names (`scripts/check-strict-live-bundle.mjs`).

Models: churn classifier (`AICT-L01`, ML lane), support chatbot (`AICT-L02`, LLM lane
judged by `claude-haiku-4-5`), NBA recommender (`AICT-L03`, ML + feedback lanes).
Deployment: the producer is a Replit Reserved VM; the monitor is Replit Autoscale with its
own managed PostgreSQL. Local development uses SQLite and `CONTROL_TOWER_MODE=demo`.

Layout: `backend/app/engines/` are pure functions, `backend/app/adapters/` do I/O,
`backend/app/scenario/live_runner.py` orchestrates, `backend/app/api/` exposes routes,
`artifacts/control-tower/` is the Vite + React SPA.

## Development

| Task | Command | Evidence |
|---|---|---|
| Build | `pnpm run build:live` (repo root; Linux x64 only, see [TESTING.md](TESTING.md)) | `artifacts/control-tower/dist/public/` produced |
| Unit tests | `.venv/bin/python -m pytest -q -m "not slow"` (from `backend/`) | all passed, `9 deselected` (slow bakes) |
| Integration tests | `.venv/bin/python -m pytest -q` (from `backend/`) | adds the slow DEMO-FULL bake and calibration tests; SQLite, no external services |
| AI evaluations | none automated | the LLM judge is exercised only with a fake judge in `backend/tests/test_live_judge_strict.py`; no live Claude call |
| Lint/type/security | `pnpm run typecheck`; `pnpm run check:strict-live` (repo root) | `Done` per package; bundle contains no forbidden strings |
| Migrations | `.venv/bin/python scripts/migrate.py` (from `backend/`) | prints the ordered schema versions; idempotent |

## Release and operations

- Environments: local demo (SQLite, `CONTROL_TOWER_MODE=demo`); CI (PostgreSQL 16
  service, placeholder producer URLs); production (Replit Autoscale,
  `CONTROL_TOWER_MODE=live`, managed PostgreSQL).
- Release procedure: [docs/STRICT-LIVE.md](docs/STRICT-LIVE.md). `.replit` builds with
  `scripts/deploy-build.sh` and runs `scripts/deploy-run.sh`; `scripts/post-merge.sh` only
  reinstalls dependencies. Replit-specific notes are in [replit.md](replit.md).
- Running identity: `GET /api/version` reports `build_sha` (the deployed Git commit),
  `contract_version`, and the producer gateway SHA.
- Monitoring: `GET /api/readiness` (database, poller, judge, configuration) and
  `GET /api/live/sync` (per-source cursor state). `.github/workflows/autoscale-poll.yml`
  wakes the Autoscale deployment every five minutes with the worker token.
- Runbook and rollback: redeploy the previous Replit revision; migrations are additive.
  Demo runbook: [docs/LIVE-DEMO.md](docs/LIVE-DEMO.md).
- Incident path: none yet (pilot); see [DEVLOG.md](DEVLOG.md) Known gaps.

## Repository guide

- Agent instructions: [CLAUDE.md](CLAUDE.md) (mirrored for other tools by [AGENTS.md](AGENTS.md))
- Testing contract: [TESTING.md](TESTING.md)
- Current work, evidence and known gaps: [DEVLOG.md](DEVLOG.md)
- Change history: [CHANGELOG.md](CHANGELOG.md)
- Change packages (intent, spec, plan per change): [changes/](changes/) — current:
  [monitoring gap closure](changes/2026-10-01-monitoring-gap-closure/)
- Telemetry contract (source of truth): [docs/MONITORING-CONTRACT.md](docs/MONITORING-CONTRACT.md)
- Strict-live deployment and configuration: [docs/STRICT-LIVE.md](docs/STRICT-LIVE.md)
- Local demo and onboarding a fourth model: [docs/LIVE-DEMO.md](docs/LIVE-DEMO.md)
- Replit deployment notes and secrets list: [replit.md](replit.md)
- ADRs: none yet; the first (alert ownership) arrives with the alerting slice.

## Known limitations

- Realized ROC-AUC and NBA acceptance rate lag the observed tick by the producer's
  `label_lag_ticks` / `reward_lag_ticks`; the Performance and Feedback lanes grade on the
  latest realized tick (shown as `as_of_tick`), not on the tick being observed. A window
  below 500 records is never realized, and a window the producer evicts (404) before its
  labels arrive stays `evicted`.
- No alerting exists in the live plane: Red/Amber transitions are visible on the board
  only; the detail payload returns `actions: []`.
- Producer HTTP calls have no retry or backoff; a stuck cursor has no operator endpoint.
- LIME per-instance explanations are off in production; the sampling policy is uniform
  (contract §14); alerts, when added, are advisory only.
- `pnpm run build:live` cannot run on macOS because non-Linux native binaries are
  excluded from the lockfile; CI on Linux is the proof.
