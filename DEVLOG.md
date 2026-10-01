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

1. Slice A — playbook baseline and `postMerge` fix: in progress (this branch).
2. Slice B — monitoring correctness (`count=0` observation, label-lag backfill): not started.
3. Slice C — alerting (transition state machine, API, webhook, UI, ADR 0001): not started.
4. Slice D — operational resilience (HTTP retry, structured logging, operator skip, NBA
   baseline persistence): not started.
5. Slice E — repository hygiene and contract strictness: not started.

## Work log

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

- `count=0` window handled as an error (contract §6): the cursor holds forever and every
  later window is never observed. Impact: a quiet hour stalls monitoring for that use case.
  Trigger: slice B. Evidence: `backend/app/adapters/ml_monitor/live_http.py` empty branch,
  `backend/app/scenario/live_runner.py` `_commit_tick`.
- Realized metrics with label lag (contract §7) not met at the tail: labels are pulled
  only for the tick being observed, so `realized_roc_auc` and NBA `acceptance_rate` stay
  Unknown indefinitely. Impact: the Performance lane never grades on real outcomes.
  Trigger: slice B.
- No alert on Red: `backend/app/api/live_portfolio.py` hard-codes `actions: []`; no
  transition detection, persistence or delivery. Impact: operators must watch the board.
  Trigger: slice C.
- No HTTP retry or backoff (contract §12): `backend/app/adapters/telemetry_http.py` uses
  a single `httpx.get`; 429/503 and connection errors hold the cursor until the next
  cycle. Impact: transient producer errors look like outages. Trigger: slice D.
- No operator path for a held cursor: `db.clear_live_state` is not routed; a poisoned
  window needs a database session. Trigger: slice D.
- Poller observability is `print`; readiness does not expose cycle duration or outcome.
  Trigger: slice D.
- NBA baseline offer mix lives in process memory and is lost on Autoscale restart, so
  `recommendation_drift` is pending after every cold start. Trigger: slice D.
- Dead scaffold remains (`.migration-backup/`, `lib/`, `artifacts/api-server/src`,
  `artifacts/mockup-sandbox`, `backend/fly.toml`, `backend/Dockerfile`,
  `scripts/src/hello.ts`, unused npm dependencies). Impact: misleading to new readers and
  slower installs. Trigger: slice E.
- Contract strictness: `contract_version` is not validated on pulled windows, `latency_s`
  defaults to 0.0 when absent, producer URLs may be `http://` in strict live mode, and the
  judge-model docstring in `backend/app/adapters/llm_eval/live_http.py` is wrong.
  Trigger: slice E.
- Alert triage ownership (contract §18 Q1) is open; resolved for this repository by
  ADR 0001 in slice C, contract-level question stays with the contract owners.
- Out of scope and unscheduled: per-use-case thresholds, LIME in production, §14 sampling
  policy, Alembic, Prometheus metrics, Slack SDK, paging/escalation, push ingest,
  skops/ONNX artifacts, retention pruning.
