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
2. Slice B — monitoring correctness (`count=0` observation, label-lag backfill): in
   progress (this branch, `fix/count-zero-and-label-backfill`).
3. Slice C — alerting (transition state machine, API, webhook, UI, ADR 0001): not started.
4. Slice D — operational resilience (HTTP retry, structured logging, operator skip, NBA
   baseline persistence): not started.
5. Slice E — repository hygiene and contract strictness: not started.

## Work log

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
