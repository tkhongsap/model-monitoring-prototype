# Development Log

## Current outcome

An operator can trust the Green/Amber/Red board for the three live use cases, is told
when a use case goes Red, can see realized performance once labels arrive, and can
unstick a stalled source without a database session. A new agent or engineer can orient
from `README.md` / `CLAUDE.md` and find the change history in `CHANGELOG.md` / this log.
Why it matters: the strict-live plumbing (lease, digest-before-cursor, ack retry) is
sound, but the monitoring on top of it is not yet trustworthy enough for the pilot to
depend on. The spec is
[changes/2026-10-01-monitoring-gap-closure/spec.md](changes/2026-10-01-monitoring-gap-closure/spec.md).

## Done when

- A: `backend/tests/test_docs.py` passes (required files exist, one H1, `CLAUDE.md` within
  120 lines, no machine-local paths, links resolve) and `scripts/post-merge.sh` no longer
  contains `db push`.
- B: with a fake producer, a `count=0` tick stores an observation and advances the cursor;
  labels arriving two ticks later produce a `realized` row in `live_realized_metrics` and
  `as_of_tick` in the detail payload; 40 % coverage yields `insufficient_coverage`; a 404 on
  backfill yields `evicted` with the cursor untouched; stored payload and digest are
  byte-identical before and after backfill.
- C: state-machine unit tests (Green→Amber opens, Amber→Red opens, Red→Green resolves,
  Unknown never alerts, dedupe); webhook test asserts payload shape, that a 500 records
  `delivery_error` and retries next cycle; `/api/live/alerts` is redacted and filtered; the
  bundle check rejects `LIVE_ALERT_WEBHOOK_URL`.
- D: retry tests (429 with `Retry-After`, 503 twice then 200, 404 not retried); the skip
  endpoint requires the worker token, writes a stub observation and advances the cursor;
  the NBA baseline survives a new runner instance.
- E: `pnpm run typecheck`, `pnpm run build:live`, `pnpm run check:strict-live` pass after
  the deletions; contract tests for version rejection, missing `latency_s`, and `http://`
  rejection in strict mode.
- Every slice: full backend suite green, both CI workflows green on the PR.

## Current plan

1. Slice A — playbook baseline and `postMerge` fix: merged (#9).
2. Slice B — monitoring correctness (`count=0` observation, label-lag backfill): merged
   (#10).
3. Slice C — alerting (transition state machine, API, webhook, UI, ADR 0001): merged
   (#11).
4. Slice D — operational resilience (HTTP retry, structured logging, operator skip, NBA
   baseline persistence): merged (#12).
5. Slice E — repository hygiene and contract strictness: done (this branch,
   `chore/hygiene-and-contract-strictness`).

## Work log

### 2026-10-02 — repository hygiene and contract strictness (slice E)

- Changed: deleted the dead scaffold (`.migration-backup/`, `lib/*`,
  `artifacts/mockup-sandbox`, `artifacts/api-server/src` + `build.mjs` + `tsconfig.json`,
  `backend/fly.toml`, `backend/Dockerfile`, `scripts/src/hello.ts`,
  `scripts/tsconfig.json`); `artifacts/api-server` keeps `.replit-artifact/artifact.toml`
  and a minimal `package.json`. `pnpm-workspace.yaml` lists `artifacts/*` and `scripts`
  only and drops the `@tanstack/react-query`, `drizzle-orm`, `tsx` catalog entries, the
  `@expo/ngrok-bin` overrides and the drizzle-kit `@esbuild-kit/esm-loader` override;
  root `package.json` loses `@replit/connectors-sdk` and `typecheck:libs`;
  `artifacts/control-tower` loses `@tanstack/react-query` /
  `@workspace/api-client-react` and its `lib/api-client-react` project reference;
  `.gitignore` / `.replitignore` no longer mention `.migration-backup`. Backend:
  `telemetry_http.pull` validates `contract_version` (`SUPPORTED_CONTRACT_VERSIONS =
  {"1.0", "1.1"}`, `ContractVersionError`), `pull_meta` checks it when present;
  `llm_eval/live_http.py` excludes traces without `latency_s` from the p95, counts
  them in `metadata["latency_missing"]`, stores `None` instead of 0.0, and its docstring
  names `claude-haiku-4-5`; `config.live_configuration_errors` requires `https` for the
  four producer URLs. Docs: CHANGELOG, README, `docs/STRICT-LIVE.md`, this log.
- Evidence: from `backend/`, `.venv/bin/python -m pytest -q -m "not slow"` → 159 passed,
  9 deselected (144 before this slice). New `tests/test_contract_strictness.py` (15 tests:
  `"0.9"` / `"2.0"` / `"1"` / missing rejected, `"1.0"` / `"1.1"` accepted, `pull_meta`
  strict vs advisory, half-missing latency → p95 9.6 and `latency_missing == 5`, all
  missing → `p95_latency_s` None, docstring guard, `http://` → four configuration
  errors, `https://` → none, insecure switch bypass). From the repo root `pnpm install`
  (lockfile regenerated: 3 added, 248 removed), `pnpm install --frozen-lockfile` → Done,
  `pnpm run typecheck` → Done for `artifacts/control-tower` (the only package with a
  `typecheck` script left). `bash -n` on the three `scripts/*.sh`; no `lib/`, `mockup`,
  `fly` or `Dockerfile` reference remains in `.replit`, `scripts/`, the workflows or the
  docs. `pnpm run build:live` and `pnpm run check:strict-live` are unavailable on this
  macOS host (lockfile drops `@rollup/rollup-darwin-arm64`); the strict-live frontend
  workflow runs them on the PR. Unavailable: real producer, Langfuse, live Claude judge.
- Learned: the existing fakes already carried `contract_version: "1.1"` (the
  `fake_producer` fixture and the `httpx.get` seams in `test_http_retry.py`), so the
  validation landed without touching a test. `docs/LIVE-DEMO.md` never had a
  mockup-sandbox / port 8081 clash note — port 8081 there is the producer's account API,
  which is correct and stays.
- Remaining: nothing in the five-slice plan. Known gaps below are out of scope or
  unscheduled.

### 2026-10-01 — live resilience: retry, logging, operator skip, baseline persistence (slice D)

- Changed: new `backend/app/http_retry.py` (`request_with_retry`: three attempts on
  429/502/503/504 and `httpx.TransportError`, backoff 0.5 s → 4 s with ±25 % jitter,
  `Retry-After` honoured and capped, other statuses returned at once, the last response
  returned when retryable statuses are exhausted); `telemetry_http` (`pull`,
  `pull_meta`, `pull_build_version`, `pull_model`, `push_scores`,
  `acknowledge_observation`) and `alert_delivery._default_post` go through it via a
  `send` callable that still calls the module-level `httpx.get` / `httpx.post`. New
  `backend/app/logging_setup.py` (`configure`: JSON lines when `LOG_FORMAT=json`, plain
  otherwise, idempotent); `main.py` configures it and its startup `print`s are log
  calls; `LivePoller.last_cycle` records cycle id, timing, outcome, backlog and per-source
  tick/duration/outcome/error, logged as one structured line per source and exposed by
  `/api/readiness` under `poller.last_cycle`. New `db.skip_live_tick` (`NothingToSkip`
  unless the cursor state is `error`; stub observation through `put_live_observation`)
  and `db.abandon_live_acks`; `POST /api/live/sources/{uc}/skip` and `/reset-ack` on the
  strict router behind the worker token; the strict-live middleware allows POST only for
  `/api/live/poll` and `/api/live/sources/*`; `realized_keys_for(uc)` lets the skip route
  mark the tick's realized rows final; the detail view passes `skipped` / `skip_reason`
  through. New `live_baselines` table (migration 7) with `put_baseline` / `get_baseline`;
  `LiveHttpNBAAdapter` stores the captured offer mix per model version and reads it back
  on cold start and rebaseline. Docs: CHANGELOG, README, `docs/STRICT-LIVE.md`
  ("Unsticking a source", retry policy, cycle logs, baselines).
- Evidence: from `backend/`, `.venv/bin/python -m pytest -q -m "not slow"` → 141 passed,
  9 deselected (112 before this slice). New tests: `test_http_retry.py` (429 with
  `Retry-After`, cap, 503 ×2 then 200 with exact backoff, jitter bound, connection errors
  retried then raised, 404 not retried, exhaustion returns the last response, `pull` /
  `acknowledge_observation` / the webhook default go through the policy and the `httpx`
  monkeypatch seam still intercepts), `test_poller_metrics.py` (per-source `ok` /
  `waiting` / `held` / `error` outcomes, structured log fields, JSON formatter,
  idempotent `configure`, readiness `last_cycle`), `test_operator_routes.py`
  (`test_skip_requires_held_cursor` → 409 and no cursor movement, 401 without the token,
  stub observation audited and realized rows final, 422 on a blank reason, 404 unknown
  use case, reset-ack abandons only the named source's acks, every other POST under
  `/api/live/` is 404), `test_nba_baseline.py` (capture persists; a second adapter
  instance measures drift 0.4 against the stored baseline instead of re-capturing a
  shifted mix — the test fails when the cold-start read is disabled; keyed by model
  version; rebaseline reloads; `clear_live_state` drops; migration 7 recorded). No file
  under `artifacts/`, `scripts/`, `lib/` or the workspace manifests changed, so the pnpm
  checks were not rerun. Unavailable: real producer, Langfuse, live Claude judge, a real
  webhook receiver.
- Learned: a default argument bound to `time.sleep` cannot be monkeypatched through the
  module, so `request_with_retry` resolves `sleep` / `rng` per call (the first version
  of the retry test slept for real and still passed). A restart test must change what
  the producer serves between the two instances, or re-capturing the baseline passes it
  vacuously. The skip stub becomes the newest observation, so the detail view, portfolio
  summary and alert evaluation have to render it: `signals: {}` and all-Unknown lanes
  do, and Unknown never opens an alert.
- Remaining: slice E below.

### 2026-10-01 — live alerting: state machine, webhook, API, UI (slice C)

- Changed: new pure engine `backend/app/engines/alerts.py` (`transitions(prev, curr,
  open_keys)`: opens on `* → Red` and `Green → Amber`, resolves on Green, dedupes on
  `(lane, to_health)` while open; a current Unknown never opens or resolves, a previous
  Unknown followed by Red opens per `* → Red`). New tables `live_alerts`
  and `live_health_snapshots` (migration 6) with `open_alert`, `resolve_alerts`,
  `list_alerts`, `open_alert_keys`, `alerts_pending_delivery`, `mark_alert_delivery`.
  New `backend/app/alerting.py` evaluates each use case after its tick inside the
  lease-held cycle, on the grading view (`apply_realized`) so a realized-AUC Red alerts.
  New `backend/app/alert_delivery.py` POSTs a Slack-compatible body to
  `LIVE_ALERT_WEBHOOK_URL` on open and resolve, records failures per phase and retries
  next cycle (one attempt per cycle until slice D), logs the host only; a missing webhook
  marks the phase `skipped`. `GET /api/live/alerts` on both routers; detail `alerts`
  replaces `actions`; portfolio `open_alerts`. Frontend `components/alerts.tsx`
  (`AlertsStrip`, `AlertsPanel`), `LiveAlert` type; bundle guard gains
  `LIVE_ALERT_WEBHOOK_URL`. `docs/adr/0001-alert-ownership.md` and the ADR index.
- Evidence: from `backend/`, `.venv/bin/python -m pytest -q -m "not slow"` → 112 passed,
  9 deselected (86 before this slice). New tests: `test_alert_engine.py`,
  `test_alerting.py` (including a poll cycle that survives a webhook outage),
  `test_alert_delivery.py` (500 → connection error → 200 delivers exactly once; logs
  carry the host, never the URL path or body), `test_alert_routes.py` (strict router
  behind the real middleware: listing, `uc`/`open`/`limit` filters, POST → 404, columns
  only; detail and portfolio fields). From the repo root `pnpm run typecheck` → Done for
  every package. `pnpm run build:live` and `pnpm run check:strict-live` are unavailable
  on this macOS host (lockfile drops `@rollup/rollup-darwin-arm64`; the build fails with
  that exact error); the strict-live frontend workflow is the proof. Unavailable: real
  producer, Langfuse, live Claude judge, a real webhook receiver.
- Learned: `app.main` chooses its router when first imported, and
  `test_strict_live_mode.py` imports it at collection time in demo mode, so a full run
  never mounts the strict router on `main.app`; a strict-router test must assemble
  `live_routes.router` plus `main.strict_live_route_isolation` itself. The plan's
  "Unknown in either position never opens" conflicts with the spec's `* → Red`; the
  spec wins (Unknown → Red opens, dedupe still prevents the Red → Unknown → Red
  duplicate), because a lane that was never measured and now reads Red is the alert
  the monitor exists to raise.
- Remaining: slices D and E below; retry with backoff for webhook delivery arrives with
  D1.

### 2026-10-01 — count=0 windows and label-lag realized metrics (slice B)

- Changed: `backend/app/adapters/ml_monitor/live_http.py` stores a `count=0` window as an
  observation (`errors["empty_window"]`, reason "empty window", NBA Feedback/mix pending)
  and uses the new pure `realized.join_realized`; `telemetry_http.pull` raises
  `WindowEvicted` on 404. New `live_realized_metrics` table (migration 4) with
  `put_realized_metric` (realized/evicted rows are final), `latest_realized`,
  `realized_history`, `ticks_needing_realization`. New `backend/app/label_backfill.py`
  revisits ticks in `[t - L - 1, t)` after each observed tick and while waiting at the tail
  (`L` from `/telemetry/meta`, default 3), verifies the re-pulled window digest against the
  stored observation, and writes `realized` / `pending` / `insufficient_coverage` /
  `single_class` / `no_labels` / `evicted` / `error`. New `backend/app/realized_view.py`
  grades the detail, portfolio rows and summary on the latest realized value with
  `as_of_tick`; runners persist `rollup_meta` so the rollup can be recomputed at read time.
- Evidence: from `backend/`, `.venv/bin/python -m pytest -q -m "not slow"` → 75 passed,
  9 deselected (40 before this slice). New tests: `test_realized_join.py`,
  `test_count_zero_window.py`, `test_realized_store.py`, `test_label_backfill.py`,
  `test_realized_view.py`; shared `tests/conftest.py` carries `isolated_db` and a
  deterministic `fake_producer`. No file under `artifacts/`, `scripts/`, `lib/` or the
  workspace manifests changed, so the pnpm checks were not rerun. Unavailable: real
  producer, Langfuse, live Claude judge.
- Learned: the backfill cannot append to `live_signal_history` (its unique key is
  `(observation_id, signal_key)` and the original observation already holds the pending
  row), so realized sparklines come from `live_realized_metrics`. In the fake-producer
  tests `estimated_roc_auc` is unmeasured (no model artifact) and is not a reasoned
  exclusion, so the Quality lane stays Unknown even when the realized AUC is Green — the
  rollup is doing what it should.
- Review fix: the spec's "`available_at_tick` ≤ current tick with no labels ⇒ final
  `no_labels`" rule is now implemented rather than approximated. Migration 5 adds a
  `final` flag to `live_realized_metrics` (set for `realized`, `evicted`, and overdue
  `no_labels`); `realize_tick` takes `current_tick` / `due_tick`, a 404 on the labels
  window alone is `pending` (not `evicted`) until the due tick passes, and `pending` rows
  that slipped below the window during an outage are swept once more. Fast suite: 84
  passed, 9 deselected.
- Review fix (second pass): finality is judged against the producer's source tick
  (`latest_tick - 1`), not the monitor's tick. While waiting at the tail the monitor's
  tick equals the producer's still-open window, so the previous rule finalized
  `no_labels` one tick early and, because final rows are immutable, lost labels published
  later in that window. `label_backfill.run` takes `source_tick` and forwards it to
  `realize_tick` for both the `available_at_tick` and the `due_tick` comparisons; the
  monitor's tick now only bounds the window. Also: an empty labels window with no
  `available_at_tick` follows the 404 rule (pending until `t + L` closes, then final
  `no_labels`) instead of lingering as non-final `no_labels`, and `count=0` / undersized
  inference windows are final at once so they are not re-pulled every cycle. Fast suite:
  86 passed, 9 deselected.
- Remaining: slices C–E below.

### 2026-10-01 — audit and playbook baseline

- Changed: three read-only audit passes (backend, frontend/deploy, playbook) produced the
  intent, spec and plan under `changes/2026-10-01-monitoring-gap-closure/`. Added
  `README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `TESTING.md` and this log from the
  playbook templates; shrank `replit.md` to Replit-specific notes and removed its two wrong
  claims (`cd frontend`, "runs migrations"); `scripts/post-merge.sh` no longer runs
  `pnpm --filter db push`; added `backend/tests/test_docs.py`.
- Evidence: from `backend/`, `.venv/bin/python -m pytest -q -m "not slow"` → 40 passed,
  9 deselected (35 before this slice); `.venv/bin/python scripts/migrate.py` against a
  scratch SQLite file prints versions 1–3. From the repo root, `pnpm run typecheck` → Done
  for `scripts`, `api-server`, `control-tower`, `mockup-sandbox`. `pnpm run build:live` and
  `pnpm run check:strict-live` are unavailable on macOS (lockfile drops
  `@rollup/rollup-darwin-arm64`); the strict-live frontend workflow is the proof. The venv
  setup line was verified in a scratch directory (157 packages installed). Unavailable:
  real producer, Langfuse, live Claude judge.
- Learned: the lockfile's platform overrides make the Vite build Linux-only, so every
  document must say so instead of listing `build:live` as a local check. The docs test
  must match actual home-directory paths (a `Users` folder followed by a user name), not
  the mention of the rule, or the spec and plan trip it.
- Remaining: slices B–E below; the `.migration-backup/` copy of the old `AGENTS.md` and
  `README.md` is skipped by the docs test until slice E deletes it.

## Known gaps

- Two pre-existing slow-test failures: `backend/tests/test_calibration.py::
  test_c3_estimated_auc_early_warning` and `::test_c8_recovery` raise `TypeError` on a
  `None` `estimated_roc_auc` (`.venv/bin/python -m pytest -q -m slow`). Impact: the slow
  demo-calibration suite is red; CI does not run `-m slow`. Trigger: unscheduled; not
  touched by slice E.
- Alert triage ownership (contract §18 Q1) is open at the contract level; resolved for
  this repository by [docs/adr/0001-alert-ownership.md](docs/adr/0001-alert-ownership.md)
  (RAI team via the webhook channel; producers are not paged). Alerts have no
  acknowledge workflow, SLA timer or escalation.
- Out of scope and unscheduled: per-use-case thresholds, LIME in production, §14 sampling
  policy, Alembic, Prometheus metrics, Slack SDK, paging/escalation, push ingest,
  skops/ONNX artifacts, retention pruning.
