"""Alert transition engine (spec C.1) — pure, testable without a database.

Given the previous and current health per key (lane names plus ``"overall"``) and the
set of alerts currently open, decide which alerts open and which resolve:

- open on ``* -> Red`` (any previous health, including a missing or Unknown one, since
  a Red that was never Red before is worth telling someone about);
- open on ``Green -> Amber`` only — Unknown -> Amber is a lane coming into measurement,
  not a degradation, and a first-ever Amber (no snapshot) is not alerted either;
- resolve every open alert on a key when it reads Green again;
- an alert is deduplicated on ``(key, to_health)`` while one is open, so
  Red -> Unknown -> Red (a stale gap) never opens a second Red;
- a current Unknown never opens and never resolves anything.

The caller persists the events and the snapshot (``app.alerting``).
"""
from __future__ import annotations

from dataclasses import dataclass

Health = str

ALERTING_HEALTHS = ("Amber", "Red")


@dataclass(frozen=True)
class AlertEvent:
    kind: str                  # "open" | "resolve"
    lane: str                  # lane name or "overall"
    from_health: Health | None
    to_health: Health


def transitions(prev: dict[str, Health], curr: dict[str, Health],
                open_keys: set[tuple[str, Health]]) -> list[AlertEvent]:
    """Diff two health maps into open/resolve events, deterministic by key name."""
    events: list[AlertEvent] = []
    for key in sorted(set(prev) | set(curr)):
        before = prev.get(key)
        now = curr.get(key)
        if now is None or now == "Unknown":
            continue  # a vanished or Unknown lane neither opens nor resolves
        if now == "Green":
            for health in ALERTING_HEALTHS:
                if (key, health) in open_keys:
                    events.append(AlertEvent("resolve", key, before or health, "Green"))
            continue
        if now == before or (key, now) in open_keys:
            continue  # no transition, or already alerting on exactly this health
        if now == "Red" or (now == "Amber" and before == "Green"):
            events.append(AlertEvent("open", key, before, now))
    return events
