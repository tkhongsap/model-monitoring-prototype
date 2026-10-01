"""Contract §7: realized metrics for recently observed ticks once lagged labels land."""
from __future__ import annotations

import pytest

from app import db, label_backfill
from app.adapters import telemetry_http
from app.adapters.ml_monitor import live_http
from app.scenario import live_runner as live_runner_module

UC = "AICT-L01"
KEY = "realized_roc_auc"


def _runner():
    return live_runner_module.LiveRunner(churn_url="https://producer.test")


def test_lag_from_meta():
    assert label_backfill.lag_from_meta({"label_lag_ticks": 5}, kind="ml") == 5
    assert label_backfill.lag_from_meta({"reward_lag_ticks": 2, "label_lag_ticks": 7}, kind="nba") == 2
    assert label_backfill.lag_from_meta({}, kind="ml") == 3
    assert label_backfill.lag_from_meta({"label_lag_ticks": "x"}, kind="ml") == 3
    assert label_backfill.lag_from_meta({"label_lag_ticks": -4}, kind="ml") == 0


def test_pull_raises_window_evicted_on_404(monkeypatch):
    class Response:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("raise_for_status must not be reached for a 404")

    monkeypatch.setattr(telemetry_http.httpx, "get", lambda *a, **k: Response())
    with pytest.raises(telemetry_http.WindowEvicted, match="tick=7"):
        telemetry_http.pull("https://producer.test", "/telemetry/inferences", {"tick": 7})


def test_current_tick_row_is_written_pending_at_commit(isolated_db, fake_producer):
    fake_producer.latest = 1
    _runner().tick()
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "pending" and row["reason"] == "label lag" and row["value"] is None


def test_backfill_realizes_older_tick_when_labels_land(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2            # ticks 0,1 closed; labels for 0 not yet available
    runner.tick()
    runner.tick()
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "pending"
    fake_producer.latest = 4
    fake_producer.release_labels(0)
    out = runner.tick()                 # observes tick 2, then backfills [t-L-1, t)
    assert out["tick"] == 2 and out.get("cursor_held") is not True
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "realized" and 0.0 <= row["value"] <= 1.0
    assert row["coverage"] == 1.0
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "pending"
    assert db.latest_realized(UC, KEY)["tick"] == 0


def test_backfill_runs_while_waiting_at_tail(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.release_labels(0)
    out = runner.tick()                 # waiting: read_tick == latest
    assert out.get("waiting") is True
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "realized"
    assert db.get_live_source(UC)["next_tick"] == 2


def test_insufficient_coverage_is_recorded_and_retried(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.release_labels(0, coverage=0.4)
    runner.tick()
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "insufficient_coverage" and row["coverage"] == 0.4
    assert row["value"] is None and db.latest_realized(UC, KEY) is None
    fake_producer.release_labels(0, coverage=0.9)
    runner.tick()                       # not final: picked up again next cycle
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "realized" and row["coverage"] == 0.9


def test_realized_row_is_final(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.release_labels(0)
    runner.tick()
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "realized"
    fake_producer.evict(0)
    runner.tick()
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "realized" and row["value"] is not None


def test_evicted_tick_never_holds_cursor(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.evict(0)
    fake_producer.latest = 4
    out = runner.tick()
    assert out.get("cursor_held") is not True
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "evicted" and "404" in row["reason"]
    assert db.get_live_source(UC)["next_tick"] == 3
    assert db.get_live_source(UC)["state"] != "error"
    # evicted is final: the producer is not asked for that window again
    fake_producer.calls.clear()
    runner.tick()
    assert ("/telemetry/inferences", {"tick": 0}) not in fake_producer.calls


def test_digest_change_on_backfill_is_an_error_not_a_value(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    original = fake_producer.records
    fake_producer.records = staticmethod(
        lambda tick, kind="ml": original(tick, kind)[::-1])   # same ids, other order
    fake_producer.release_labels(0)
    runner.tick()
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "error" and "digest changed" in row["reason"]
    assert row["value"] is None and db.latest_realized(UC, KEY) is None
    assert db.get_live_source(UC)["state"] != "error"


def test_transient_backfill_failure_never_fails_the_tick(isolated_db, fake_producer, monkeypatch):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    real_pull = fake_producer.pull

    def flaky(base, path, params=None, timeout=30.0):
        if path == "/telemetry/labels":
            raise ConnectionError("producer blipped")
        return real_pull(base, path, params, timeout)

    monkeypatch.setattr(live_http, "pull", flaky)
    out = runner.tick()
    assert out.get("waiting") is True and "telemetry" not in out.get("errors", {})
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "pending"
    assert db.get_live_source(UC)["state"] != "error"


def test_observation_payload_unchanged_by_backfill(isolated_db, fake_producer):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    before = db.list_live_observations(UC)
    history_before = db.get_live_signal_history(UC, KEY, 50)
    fake_producer.release_labels(0)
    fake_producer.latest = 4
    runner.tick()
    after = {o["observation_id"]: o for o in db.list_live_observations(UC)}
    for o in before:
        assert after[o["observation_id"]]["payload"] == o["payload"]
        assert after[o["observation_id"]]["content_sha256"] == o["content_sha256"]
    # the signal-history rows written with the original observations are untouched
    assert db.get_live_signal_history(UC, KEY, 50)[:len(history_before)] == history_before


def test_nba_backfill_realizes_acceptance_rate(isolated_db, fake_producer):
    runner = live_runner_module.LiveNBARunner(nba_url="https://producer.test")
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    assert db.get_realized_metric("AICT-L03", 0, "acceptance_rate")["status"] == "pending"
    fake_producer.release_labels(0)
    runner.tick()
    auc = db.get_realized_metric("AICT-L03", 0, KEY)
    acc = db.get_realized_metric("AICT-L03", 0, "acceptance_rate")
    assert auc["status"] == "realized" and auc["value"] == 1.0
    assert acc["status"] == "realized" and acc["value"] == 0.5 and acc["coverage"] == 1.0
