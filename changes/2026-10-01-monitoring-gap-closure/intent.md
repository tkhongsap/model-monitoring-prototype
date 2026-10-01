# Intent: Close the monitoring gaps before the pilot depends on it

- **Status:** Accepted
- **Originator:** project owner (ta.khongsap), 2026-10-01 session
- **Date:** 2026-10-01
- **Risk tier:** R1 — assisted internal workflow; the monitor informs humans and never acts on a model

## Problem

An audit on 2026-10-01 (three read-only passes over backend, frontend/deploy and the
AI engineering playbook) found that the strict-live plumbing is sound but the monitoring on
top of it is not yet trustworthy:

- A `count=0` window is treated as an error and holds the source cursor forever.
- Labels are only joined for the tick being observed; once the cursor advances a tick is
  never revisited, so at the tail `realized_roc_auc` and NBA `acceptance_rate` stay
  Unknown indefinitely.
- No alert exists in the live plane: no transition detection, no delivery, `actions: []`.
- No retry/backoff, no dead-letter path, poller observability is `print`.
- The repository has none of the playbook's required documents and the Replit
  `postMerge` hook runs `drizzle-kit push` with an empty schema against the backend's
  database.

## Proposed outcome

An operator can trust the Green/Amber/Red board for the three live use cases, is told
when a use case goes Red, can see realized performance once labels arrive, and can
unstick a stalled source without a database session. A new agent or engineer can orient
from `README.md` / `CLAUDE.md` and find the change history in `CHANGELOG.md` / `DEVLOG.md`.

## Users and systems affected

- Monitor operators (RAI team) — new alert channel, new operator endpoint, new docs.
- The `ai-use-cases` producer — unchanged; no contract version bump.
- Replit Autoscale deployment — `postMerge` hook changed, new env vars (optional).

## Constraints

- Contract v1.1 stays as is; the monitor becomes a more faithful consumer of it.
- Strict-live invariants hold: observations are immutable once digested; cursors advance
  only after durable writes; no scenario data in production.
- No new paid dependencies. Alert delivery is a plain webhook.
- Work lands as five sequential PRs so each can be reverted alone.

## Open questions

| Question | Blocks | Owner | Needed by |
|---|---|---|---|
| Who owns alert triage once delivery exists (contract §18 Q1)? | Release of PR 3 | project owner | before enabling `LIVE_ALERT_WEBHOOK_URL` in prod |

## Decision

Accepted 2026-10-01 by the project owner: proceed with all five slices in the order
docs → correctness → alerting → resilience → hygiene.
