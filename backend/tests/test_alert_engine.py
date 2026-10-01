"""Alert transition engine (spec C.1): pure, no database."""
from __future__ import annotations

from app.engines.alerts import AlertEvent, transitions


def _only(events: list[AlertEvent]) -> AlertEvent:
    assert len(events) == 1, events
    return events[0]


def test_green_to_amber_opens():
    ev = _only(transitions({"Quality": "Green"}, {"Quality": "Amber"}, set()))
    assert ev == AlertEvent("open", "Quality", "Green", "Amber")


def test_amber_to_red_opens_red():
    ev = _only(transitions({"Quality": "Amber"}, {"Quality": "Red"}, {("Quality", "Amber")}))
    assert ev.kind == "open" and ev.to_health == "Red" and ev.from_health == "Amber"


def test_first_evaluation_only_red_opens():
    # no snapshot yet: a brand-new Red is worth an alert, a brand-new Amber is not
    assert transitions({}, {"Quality": "Amber", "overall": "Amber"}, set()) == []
    events = transitions({}, {"Quality": "Red", "overall": "Red"}, set())
    assert {(e.lane, e.to_health) for e in events} == {("Quality", "Red"), ("overall", "Red")}
    assert all(e.kind == "open" and e.from_health is None for e in events)


def test_red_to_green_resolves():
    open_keys = {("Quality", "Amber"), ("Quality", "Red")}
    events = transitions({"Quality": "Red"}, {"Quality": "Green"}, open_keys)
    assert [e.kind for e in events] == ["resolve", "resolve"]
    assert {e.to_health for e in events} == {"Green"}
    assert {e.from_health for e in events} == {"Red"}
    assert {e.lane for e in events} == {"Quality"}


def test_green_without_open_alert_is_silent():
    assert transitions({"Quality": "Amber"}, {"Quality": "Green"}, set()) == []
    assert transitions({"Quality": "Green"}, {"Quality": "Green"}, set()) == []


def test_unknown_never_alerts():
    assert transitions({"Quality": "Green"}, {"Quality": "Unknown"}, set()) == []
    assert transitions({"Quality": "Unknown"}, {"Quality": "Amber"}, set()) == []
    assert transitions({"Quality": "Red"}, {"Quality": "Unknown"}, {("Quality", "Red")}) == []
    assert transitions({"Quality": "Amber"}, {}, {("Quality", "Amber")}) == []  # lane vanished


def test_unknown_to_red_still_opens():
    # a lane that was never measured and now reads Red is a real Red (spec: `* -> Red`)
    ev = _only(transitions({"Quality": "Unknown"}, {"Quality": "Red"}, set()))
    assert ev.kind == "open" and ev.from_health == "Unknown" and ev.to_health == "Red"


def test_dedupe_while_open():
    assert transitions({"Quality": "Green"}, {"Quality": "Amber"}, {("Quality", "Amber")}) == []
    assert transitions({"Quality": "Amber"}, {"Quality": "Red"}, {("Quality", "Red")}) == []
    assert transitions({"Quality": "Red"}, {"Quality": "Red"}, set()) == []  # no transition


def test_unknown_gap_does_not_reopen():
    open_keys = {("Quality", "Red")}
    assert transitions({"Quality": "Red"}, {"Quality": "Unknown"}, open_keys) == []
    assert transitions({"Quality": "Unknown"}, {"Quality": "Red"}, open_keys) == []


def test_every_lane_is_considered_independently():
    prev = {"Quality": "Green", "Drift & degradation": "Green", "overall": "Green"}
    curr = {"Quality": "Amber", "Drift & degradation": "Red", "overall": "Red"}
    events = transitions(prev, curr, set())
    assert {(e.lane, e.to_health) for e in events} == {
        ("Quality", "Amber"), ("Drift & degradation", "Red"), ("overall", "Red")}
    assert events == sorted(events, key=lambda e: e.lane)  # deterministic order
