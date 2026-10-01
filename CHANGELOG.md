# Changelog

Notable user- and operator-visible changes are recorded here. Development notes
and evidence belong in [DEVLOG.md](DEVLOG.md); decision rationale belongs in ADRs
(none yet; the first arrives with the alerting slice).

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
