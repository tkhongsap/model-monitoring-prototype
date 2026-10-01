# ADR 0001: The RAI team owns alert triage through the monitor's webhook channel

- **Status:** Accepted
- **Date:** 2026-10-01
- **Decision owners:** project owner (ta.khongsap) for this repository; the RAI team as
  the operating role
- **Review trigger:** the AI Council resolves contract §18 open question 1 ("who owns
  alerting when a use case goes Red"), a second consumer of the alert channel appears,
  or a producer team asks to be paged directly

## Context

Slice C of the monitoring gap closure adds alerting: every committed observation is
graded, Red and Green→Amber transitions are persisted in `live_alerts`, and each opened
or resolved alert is POSTed to one operator-configured webhook (`LIVE_ALERT_WEBHOOK_URL`).
Something has to be true about *who reads that channel and acts*, or the alert is noise.

The telemetry contract (`docs/MONITORING-CONTRACT.md` §16 and §18 question 1) leaves
alert ownership open at the portfolio level: the contract is shared with producer teams
and the Amity integration, and the AI Council has not ruled. This repository cannot
wait for that ruling to ship alerting, but it must not pretend to answer the contract
question for everyone.

Constraints: the monitor is an R1 system that advises humans and never acts on a model
(intent). Producers are separate teams on separate deployments; the monitor holds no
producer-side contact details and has no paging integration. The pilot has one webhook
and no on-call rota. Alerts are advisory: the Flagged-concern table in the spec and the
README "Known limitations" both say so.

## Decision criteria

- Clarity of accountability: one role answers "who saw the Red and what did they do".
- Least new surface: no new tokens, directories, or integrations beyond one webhook URL.
- Respect for team boundaries: producers are told about their model's health by a
  human after triage, not paged by a monitor they do not operate.
- Reversibility: the contract-level answer may differ; this decision must be cheap to
  supersede.

## Options considered

### Option A — The RAI team owns triage via the webhook channel (chosen)

The webhook posts to a channel the RAI team reads. The RAI team triages every open
alert, decides whether it is a monitor problem (stale source, label coverage, bad band)
or a model problem, and contacts the producer team through the normal engineering
channel when it is the latter. Benefits: matches the intent's user ("the RAI team
operating the monitor"), one accountable role, zero producer-side change. Drawbacks: adds
a human hop between a Red and the team that can fix the model; relies on the RAI team
actually watching the channel (no SLA timer in this slice).

### Option B — Page producer teams directly per use case

Each use case carries a producer contact (channel or pager) in the registry seed and the
monitor notifies it on Red. Benefits: shortest path from signal to the team that can act.
Drawbacks: the monitor becomes a paging system for teams that did not opt in; false
positives (a stale source, an undersized window, a label-coverage dip) would page the
wrong people and erode trust; requires per-use-case contact data and a routing layer the
contract has not agreed. Rejected for the pilot; it is the shape the contract-level
decision might eventually take, which is why the webhook payload already carries
`use_case_id`.

### Option C — No delivery; dashboard only

Persist alerts and show them in the UI, deliver nothing. Benefits: nothing to own.
Drawbacks: fails the spec's user contract ("operators … are notified on Red/Amber
transitions") and the DEVLOG known gap "operators must watch the board". Rejected.

## Decision

Option A. For this repository, alert triage belongs to the RAI team, exercised through
the single webhook channel configured by `LIVE_ALERT_WEBHOOK_URL`. Producers are not
paged by the monitor. The contract-level question (§18 Q1) stays open with the contract
owners; this ADR answers it only for the monitor's own pilot, and records that a
different contract-level ruling supersedes it.

Operationally: an open alert is the RAI team's to triage; a resolved alert needs no
action; the use-case page and `GET /api/live/alerts` are the record of what was open and
when. There is no acknowledge workflow, SLA timer or escalation in this slice (spec C.6
non-goals).

## Consequences

### Positive

- One accountable role for every Red, consistent with the intent's user and the R1 tier.
- No producer-side obligations are added; contract v1.1 is unchanged.
- The channel is Slack-incoming-webhook compatible, so moving to a bot or a second
  receiver later does not change the payload.

### Negative and risk

- A human hop sits between a Red and the producer team. Mitigation: the webhook text
  names the use case, lane and tick, and links the dashboard URL so triage is quick;
  the RAI team contacts the producer through the usual channel. Owner: RAI team.
- Nothing enforces that the channel is watched. Mitigation: `GET /api/live/alerts?open=true`
  and the home-page strip make unattended alerts visible; an SLA timer is listed in the
  DEVLOG as out of scope and would arrive with escalation.
- Delivery is at-least-once with one attempt per poll cycle until slice D adds retry
  with backoff; a long webhook outage delays notification but never loses the alert
  (it stays open in the database and the UI).

## Validation and rollback

Validated by `backend/tests/test_alert_engine.py`, `test_alerting.py`,
`test_alert_delivery.py` and `test_alert_routes.py`: transitions open and resolve as
specified, a failed webhook POST is recorded on the row and retried next cycle, the
payload carries no features or trace text and the logs carry only the webhook host, and
the strict router serves the listing read-only.

Invalidated by: the AI Council assigning alert ownership elsewhere, a producer team
needing direct notification, or the RAI team being unable to staff the channel. To
reverse: unset `LIVE_ALERT_WEBHOOK_URL` (alerts keep being recorded and shown; nothing
is delivered) and write ADR 0002 superseding this one. The `live_alerts` table and the
API stay valid under any ownership answer.

## Sources

- `changes/2026-10-01-monitoring-gap-closure/intent.md` (user: the RAI team; R1 tier)
- `changes/2026-10-01-monitoring-gap-closure/spec.md` §C and "Open questions carried forward"
- `docs/MONITORING-CONTRACT.md` §16 (alerting ownership = open question) and §18 question 1
- `backend/app/alerting.py`, `backend/app/alert_delivery.py`, `backend/app/engines/alerts.py`
