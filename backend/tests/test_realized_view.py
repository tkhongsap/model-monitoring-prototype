"""Grading uses the latest realized value (spec B.4): apply_realized and the live routes."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import config, db
from app.realized_view import apply_realized
from app.scenario import live_runner as live_runner_module

UC = "AICT-L01"
KEY = "realized_roc_auc"


def _sig(key, value, health, lane, **extra):
    return {"value": value, "health": health, "label": key, "lane": lane, "unit": "",
            "direction": "higher_is_better", "green_bar": 0.8, "red_bar": 0.72, **extra}


def _churn_state(tick=4):
    return {
        "use_case_id": UC, "tick": tick, "mode": "live",
        "signals": {
            "data_drift_share": _sig("data_drift_share", 0.1, "Green", "Drift & degradation"),
            "estimated_roc_auc": _sig("estimated_roc_auc", 0.9, "Green", "Quality"),
            KEY: _sig(KEY, None, "Unknown", "Quality", pending_reason="label lag"),
        },
        "lanes": {"Quality": "Green", "Drift & degradation": "Green",
                  "Feedback & action loop": "Unknown", "Safety & security": "Unknown",
                  "Reliability": "Unknown"},
        "overall": "Green",
        "lane_reasons": {lane: "Unknown — not instrumented" for lane in
                         ("Feedback & action loop", "Safety & security", "Reliability")},
        "realized_pending_reason": "label lag",
        "rollup_meta": {
            "hand_set_lanes": {"Feedback & action loop": "Unknown",
                               "Safety & security": "Unknown", "Reliability": "Unknown"},
            "excluded_lanes": ["Feedback & action loop", "Reliability", "Safety & security"],
            "excluded_keys": [KEY],
        },
        "errors": {"realized_pending": "label lag"},
    }


def test_unchanged_without_realized_rows(isolated_db):
    state = _churn_state()
    assert apply_realized(UC, state) is state
    assert apply_realized(UC, None) is None


def test_latest_realized_flips_overall_and_marks_as_of_tick(isolated_db):
    db.put_realized_metric(UC, 1, KEY, value=0.9, coverage=1.0, status="realized")
    db.put_realized_metric(UC, 2, KEY, value=0.6, coverage=0.8, status="realized")
    db.put_realized_metric(UC, 3, KEY, value=None, coverage=0.0, status="pending",
                           reason="label lag")
    state = _churn_state()
    out = apply_realized(UC, state)
    assert out is not state and state["overall"] == "Green"        # input untouched
    assert state["signals"][KEY]["pending_reason"] == "label lag"
    sig = out["signals"][KEY]
    assert sig["value"] == 0.6 and sig["health"] == "Red" and sig["as_of_tick"] == 2
    assert sig["coverage"] == 0.8 and "pending_reason" not in sig
    assert out["lanes"]["Quality"] == "Red" and out["overall"] == "Red"
    assert out["lanes"]["Feedback & action loop"] == "Unknown"      # hand-set lanes kept
    assert out["realized_pending_reason"] is None
    assert out["realized_as_of_tick"] == 2
    assert out["rollup_meta"]["excluded_keys"] == []


def test_realized_green_keeps_overall_green(isolated_db):
    db.put_realized_metric(UC, 2, KEY, value=0.85, coverage=1.0, status="realized")
    out = apply_realized(UC, _churn_state())
    assert out["signals"][KEY]["health"] == "Green" and out["overall"] == "Green"


def test_works_without_rollup_meta_from_older_observations(isolated_db):
    db.put_realized_metric(UC, 2, KEY, value=0.6, coverage=1.0, status="realized")
    state = _churn_state()
    del state["rollup_meta"]
    out = apply_realized(UC, state)
    assert out["overall"] == "Red" and out["lanes"]["Quality"] == "Red"
    assert out["lanes"]["Safety & security"] == "Unknown"


def test_nba_acceptance_rate_realized_counts_the_feedback_lane(isolated_db):
    uc = "AICT-L03"
    state = {
        "use_case_id": uc, "tick": 3, "mode": "live",
        "signals": {
            "estimated_roc_auc": _sig("estimated_roc_auc", 0.9, "Green", "Quality"),
            KEY: _sig(KEY, None, "Unknown", "Quality", pending_reason="label lag"),
            "acceptance_rate": _sig("acceptance_rate", None, "Unknown",
                                    "Feedback & action loop", pending_reason="label lag"),
            "recommendation_drift": _sig("recommendation_drift", 0.1, "Green",
                                         "Drift & degradation"),
        },
        "lanes": {"Quality": "Green", "Drift & degradation": "Green",
                  "Feedback & action loop": "Unknown", "Safety & security": "Unknown",
                  "Reliability": "Unknown"},
        "overall": "Green",
        "acceptance_pending_reason": "label lag", "realized_pending_reason": "label lag",
        "rollup_meta": {
            "hand_set_lanes": {"Safety & security": "Unknown", "Reliability": "Unknown",
                               "Feedback & action loop": "Unknown"},
            "excluded_lanes": ["Feedback & action loop", "Reliability", "Safety & security"],
            "excluded_keys": [KEY, "acceptance_rate"],
        },
    }
    db.put_realized_metric(uc, 1, "acceptance_rate", value=0.03, coverage=1.0,
                           status="realized")
    out = apply_realized(uc, state)
    assert out["signals"]["acceptance_rate"]["health"] == "Red"
    assert out["signals"]["acceptance_rate"]["as_of_tick"] == 1
    assert out["lanes"]["Feedback & action loop"] == "Red" and out["overall"] == "Red"
    assert out["acceptance_pending_reason"] is None
    assert out["signals"][KEY]["pending_reason"] == "label lag"      # still pending
    assert out["lanes"]["Quality"] == "Green"


def test_runner_payload_carries_rollup_meta(isolated_db, fake_producer):
    fake_producer.latest = 1
    out = live_runner_module.LiveRunner(churn_url="https://producer.test").tick()
    assert out["rollup_meta"]["excluded_keys"] == [KEY]
    assert "Feedback & action loop" in out["rollup_meta"]["hand_set_lanes"]
    nba = live_runner_module.LiveNBARunner(nba_url="https://producer.test").tick()
    assert set(nba["rollup_meta"]["excluded_keys"]) >= {KEY, "acceptance_rate"}
    assert "Feedback & action loop" in nba["rollup_meta"]["excluded_lanes"]


def _live_client(monkeypatch):
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    from app.main import app
    return TestClient(app)


def test_use_case_route_shows_as_of_tick_and_realized_history(isolated_db, fake_producer,
                                                              monkeypatch):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.release_labels(0)
    fake_producer.latest = 4
    runner.tick()
    with _live_client(monkeypatch) as client:
        body = client.get(f"/api/live/use-case/{UC}").json()
        sig = next(s for s in body["signals"] if s["key"] == KEY)
        assert sig["as_of_tick"] == 0 and sig["value"] == 1.0 and sig["health"] == "Green"
        assert sig["pending_reason"] is None
        assert sig["history"] == [{"tick": 0, "value": 1.0, "health": "Green"}]
        assert body["realized_as_of_tick"] == 0 and body["realized_pending_reason"] is None
        # the realized AUC is Green, but estimated_roc_auc is unmeasured here (no model
        # artifact) and NOT a reasoned exclusion, so the Quality lane stays Unknown
        estimated = next(s for s in body["signals"] if s["key"] == "estimated_roc_auc")
        assert estimated["health"] == "Unknown" and estimated["pending_reason"] is None
        assert body["lanes"]["Quality"] == "Unknown"
        other = next(s for s in body["signals"] if s["key"] == "data_drift_share")
        assert other["as_of_tick"] is None and len(other["history"]) == 3
        portfolio = client.get("/api/live/portfolio").json()
        row = next(r for r in portfolio["rows"] if r["registry_id"] == UC)
        assert row["realized_as_of_tick"] == 0
