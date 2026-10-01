"""Alert persistence and evaluation (spec C.1): live_alerts, snapshots, alerting.evaluate."""
from __future__ import annotations

from app import alerting, db

UC = "AICT-L01"
KEY = "realized_roc_auc"
LANES = ("Quality", "Drift & degradation", "Feedback & action loop",
         "Safety & security", "Reliability")
HAND_SET = ("Feedback & action loop", "Safety & security", "Reliability")


def _sig(key, value, health, lane, **extra):
    return {"value": value, "health": health, "label": key, "lane": lane, "unit": "",
            "direction": "lower_is_better", "green_bar": 0.3, "red_bar": 0.5, **extra}


def _observe(tick: int, drift: float, drift_health: str, overall: str) -> str:
    payload = {
        "use_case_id": UC, "tick": tick, "observed_tick": tick, "mode": "live",
        "window_id": f"w{tick}", "record_count": 600,
        "signals": {
            "data_drift_share": _sig("data_drift_share", drift, drift_health, "Drift & degradation"),
            "estimated_roc_auc": _sig("estimated_roc_auc", 0.9, "Green", "Quality"),
            KEY: _sig(KEY, None, "Unknown", "Quality", pending_reason="label lag"),
        },
        "lanes": {"Quality": "Green", "Drift & degradation": drift_health,
                  **{lane: "Unknown" for lane in HAND_SET}},
        "overall": overall,
        "lane_reasons": {lane: "Unknown — not instrumented" for lane in HAND_SET},
        "realized_pending_reason": "label lag",
        "rollup_meta": {"hand_set_lanes": {lane: "Unknown" for lane in HAND_SET},
                        "excluded_lanes": sorted(HAND_SET), "excluded_keys": [KEY]},
        "errors": {},
    }
    observation_id, _ = db.put_live_observation(
        UC, payload, source_tick=tick, next_tick=tick + 1, backlog=0, state="at_tail")
    return observation_id


def test_snapshot_roundtrip(isolated_db):
    assert db.get_health_snapshot(UC) is None
    db.put_health_snapshot(UC, 3, "Amber", {"Quality": "Amber", "Reliability": "Unknown"})
    db.put_health_snapshot(UC, 4, "Green", {"Quality": "Green"})
    snap = db.get_health_snapshot(UC)
    assert snap["tick"] == 4 and snap["overall"] == "Green"
    assert snap["lanes"] == {"Quality": "Green"}


def test_open_resolve_and_list(isolated_db):
    a = db.open_alert(UC, "Quality", "Green", "Amber", 2, "obs-2")
    b = db.open_alert(UC, "Quality", "Amber", "Red", 3, "obs-3")
    c = db.open_alert("AICT-L03", "overall", None, "Red", 1, "obs-x")
    assert db.open_alert_keys(UC) == {("Quality", "Amber"), ("Quality", "Red")}
    assert {r["alert_id"] for r in db.list_alerts(UC, open_only=True)} == {a, b}
    assert {r["alert_id"] for r in db.list_alerts(open_only=True)} == {a, b, c}
    newest = db.list_alerts(UC, limit=1)[0]
    assert newest["alert_id"] == b and newest["open_delivery_status"] == "pending"
    assert newest["resolve_delivery_status"] == "n/a" and newest["resolved_at"] is None

    assert sorted(db.resolve_alerts(UC, "Quality", 5)) == sorted([a, b])
    assert db.resolve_alerts(UC, "Quality", 6) == []          # nothing left to resolve
    assert db.open_alert_keys(UC) == set()
    rows = {r["alert_id"]: r for r in db.list_alerts(UC)}
    assert rows[a]["resolved_tick"] == 5 and rows[a]["resolved_at"] is not None
    assert rows[a]["resolve_delivery_status"] == "pending"
    assert db.list_alerts(UC, open_only=True) == []
    assert [r["alert_id"] for r in db.list_alerts("AICT-L03")] == [c]


def test_delivery_bookkeeping(isolated_db):
    a = db.open_alert(UC, "Quality", "Green", "Red", 2, "obs-2")
    assert [r["alert_id"] for r in db.alerts_pending_delivery()] == [a]
    db.mark_alert_delivery(a, "open", ok=False, error="HTTP 500")
    row = db.list_alerts(UC)[0]
    assert row["open_delivery_status"] == "error" and row["open_delivery_error"] == "HTTP 500"
    assert [r["alert_id"] for r in db.alerts_pending_delivery()] == [a]   # retried next cycle
    db.mark_alert_delivery(a, "open", ok=True, error=None)
    row = db.list_alerts(UC)[0]
    assert row["open_delivery_status"] == "delivered" and row["open_delivered_at"] is not None
    assert row["open_delivery_error"] is None
    assert db.alerts_pending_delivery() == []
    db.resolve_alerts(UC, "Quality", 4)
    assert [r["alert_id"] for r in db.alerts_pending_delivery()] == [a]   # resolve phase
    db.mark_alert_delivery(a, "resolve", ok=True, error=None)
    assert db.alerts_pending_delivery() == []


def test_evaluate_opens_once_and_resolves_on_green(isolated_db, fake_producer):
    assert alerting.evaluate(UC) == []                        # never observed: nothing
    obs0 = _observe(0, 0.1, "Green", "Green")
    assert alerting.evaluate(UC) == []                        # first snapshot, all Green
    snap = db.get_health_snapshot(UC)
    assert snap["tick"] == 0 and snap["overall"] == "Green"

    obs1 = _observe(1, 0.6, "Red", "Red")
    events = alerting.evaluate(UC)
    assert {(e.kind, e.lane, e.to_health) for e in events} == {
        ("open", "Drift & degradation", "Red"), ("open", "overall", "Red")}
    assert alerting.evaluate(UC) == []                        # same state: dedupe
    open_rows = db.list_alerts(UC, open_only=True)
    assert len(open_rows) == 2
    assert all(r["to_health"] == "Red" and r["tick"] == 1 and r["observation_id"] == obs1
               for r in open_rows)
    assert obs0 != obs1

    _observe(2, 0.05, "Green", "Green")
    events = alerting.evaluate(UC)
    assert {(e.kind, e.lane) for e in events} == {
        ("resolve", "Drift & degradation"), ("resolve", "overall")}
    assert db.list_alerts(UC, open_only=True) == []
    rows = db.list_alerts(UC)
    assert len(rows) == 2 and all(r["resolved_tick"] == 2 and r["resolved_at"] for r in rows)


def test_evaluate_uses_realized_grading(isolated_db, fake_producer):
    """A Red caused only by a lagged realized AUC must alert too (grading view, not the
    stored payload)."""
    _observe(0, 0.1, "Green", "Green")
    assert alerting.evaluate(UC) == []
    db.put_realized_metric(UC, 0, KEY, value=0.6, coverage=1.0, status="realized")
    events = alerting.evaluate(UC)
    assert {(e.kind, e.lane, e.to_health) for e in events} == {
        ("open", "Quality", "Red"), ("open", "overall", "Red")}
    assert db.get_health_snapshot(UC)["lanes"]["Quality"] == "Red"


def test_clear_live_state_drops_alerts(isolated_db):
    db.open_alert(UC, "Quality", "Green", "Red", 2, "obs-2")
    db.put_health_snapshot(UC, 2, "Red", {"Quality": "Red"})
    db.clear_live_state(UC)
    assert db.list_alerts(UC) == [] and db.get_health_snapshot(UC) is None


def test_poll_cycle_evaluates_and_survives_webhook_outage(isolated_db, monkeypatch):
    from app import alert_delivery, config, live_poller
    from app.api import live_portfolio
    from app.scenario import live_runner as live_runner_module

    monkeypatch.setattr(live_portfolio, "LIVE_UCS", [UC])
    monkeypatch.setattr(config, "LIVE_ALERT_WEBHOOK_URL", "https://hooks.example.test/x")

    class RedRunner:
        def tick(self):
            return {"waiting": True}

        def state(self):
            return {"use_case_id": UC, "tick": 7, "observation_id": "obs-7",
                    "signals": {}, "lanes": {"Quality": "Red"}, "overall": "Red"}

    monkeypatch.setattr(live_runner_module, "live_runner", lambda uc: RedRunner())

    def failing_post(url, *, json, timeout):
        raise ConnectionError("webhook down")

    monkeypatch.setattr(alert_delivery, "_default_post", failing_post)
    worker = live_poller.LivePoller()
    try:
        assert worker.run_once() is True
    finally:
        db.release_live_lease(live_poller.LEASE_NAME, worker.owner_id)
    rows = db.list_alerts(UC, open_only=True)
    assert {r["lane"] for r in rows} == {"Quality", "overall"}
    assert all(r["open_delivery_status"] == "error" for r in rows)
    assert db.get_live_source(UC)["state"] != "error"      # delivery never holds a source
    assert db.get_health_snapshot(UC)["tick"] == 7
