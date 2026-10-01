"""Contract §6: a closed window with count=0 is a real observation, not an error."""
from __future__ import annotations

from app import db
from app.adapters.base import TickContext
from app.adapters.ml_monitor.nba_live_http import LiveHttpNBAAdapter
from app.scenario import live_runner as live_runner_module


def test_empty_window_is_observed_and_cursor_advances(isolated_db, fake_producer):
    fake_producer.empty.add(0)
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    out = runner.tick()
    assert out.get("cursor_held") is not True
    assert out["tick"] == 0 and out["record_count"] == 0
    assert out["errors"].get("empty_window") == "count=0"
    assert "telemetry" not in out["errors"]
    assert db.get_live_source("AICT-L01")["next_tick"] == 1
    assert db.get_live_source("AICT-L01")["state"] != "error"
    assert out["signals"]["realized_roc_auc"]["pending_reason"] == "empty window"
    assert all(sig["value"] is None for sig in out["signals"].values())
    assert out["lanes"]["Quality"] == "Unknown"
    assert out["lanes"]["Drift & degradation"] == "Unknown"
    stored = db.get_latest_live_observation("AICT-L01")
    assert stored["record_count"] == 0 and stored["payload"]["empty_window"] is True


def test_empty_window_then_normal_window(isolated_db, fake_producer):
    fake_producer.empty.add(0)
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    runner.tick()
    second = runner.tick()
    assert second["tick"] == 1 and second["record_count"] == 600
    assert "empty_window" not in second["errors"]
    assert second.get("empty_window") is not True
    assert second["signals"]["realized_roc_auc"]["pending_reason"] == "label lag"
    assert second["signals"]["data_drift_share"]["value"] == 0.0
    assert db.get_live_source("AICT-L01")["next_tick"] == 2


def test_nba_empty_window_marks_feedback_and_mix_pending(isolated_db, fake_producer):
    fake_producer.empty.add(0)
    adapter = LiveHttpNBAAdapter("https://producer.test", lambda *args: "artifact")
    res = adapter.monitor("AICT-L03", TickContext(tick=0, seed=42))
    assert res.errors["empty_window"] == "count=0"
    assert "telemetry" not in res.errors
    assert res.errors["acceptance_pending"] == "empty window"
    assert res.errors["recommendation_drift_pending"] == "empty window"
    assert set(res.signals) == set(adapter._signal_keys)
    assert all(value is None for value in res.signals.values())
    assert res.records["realized_pending_reason"] == "empty window"
