"""Alert evaluation (spec C.1): diff a use case's graded health against its last snapshot.

Runs inside the lease-held poll cycle after each runner tick (``live_poller``).  The
health it diffs is the *grading view* — ``realized_view.apply_realized`` over the latest
stored observation — so a Red caused by a lagged realized AUC alerts exactly like a Red
in the observation itself.  Transitions come from the pure engine
(``engines.alerts.transitions``); this module only persists them and the new snapshot.
Delivery is separate (``alert_delivery``) and never blocks grading.
"""
from __future__ import annotations

import logging

from . import db
from .engines.alerts import AlertEvent, transitions
from .realized_view import apply_realized

log = logging.getLogger(__name__)


def current_health(uc: str) -> dict | None:
    """``{lane: health, ..., "overall": health}`` plus tick/observation for a use case,
    or None when it has never been observed."""
    from .scenario.live_runner import live_runner
    state = apply_realized(uc, live_runner(uc).state())
    if not state:
        return None
    lanes = {str(lane): str(h) for lane, h in (state.get("lanes") or {}).items()}
    return {"lanes": lanes, "overall": str(state.get("overall") or "Unknown"),
            "tick": state.get("tick"), "observation_id": state.get("observation_id")}


def evaluate(uc: str) -> list[AlertEvent]:
    """Persist open/resolve transitions for one use case and refresh its snapshot."""
    now = current_health(uc)
    if now is None:
        return []
    curr = {**now["lanes"], "overall": now["overall"]}
    snapshot = db.get_health_snapshot(uc)
    prev = {**snapshot["lanes"], "overall": snapshot["overall"]} if snapshot else {}
    events = transitions(prev, curr, db.open_alert_keys(uc))
    for event in events:
        if event.kind == "open":
            db.open_alert(uc, event.lane, event.from_health, event.to_health,
                          now["tick"], now["observation_id"])
            log.info("alert opened: %s %s %s -> %s at tick %s", uc, event.lane,
                     event.from_health, event.to_health, now["tick"])
        else:
            db.resolve_alerts(uc, event.lane, now["tick"])
            log.info("alert resolved: %s %s -> Green at tick %s", uc, event.lane, now["tick"])
    db.put_health_snapshot(uc, now["tick"], now["overall"], now["lanes"])
    return events
