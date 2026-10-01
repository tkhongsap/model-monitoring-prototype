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


def test_overdue_tick_without_labels_is_final_no_labels(isolated_db, fake_producer):
    """Spec B.2: labels never arrive -> `no_labels` once `available_at_tick` is at or
    before the producer's latest CLOSED tick (`latest_tick - 1`), never its open one."""
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()                       # ticks 0, 1 observed; labels due at 3, 4
    fake_producer.latest = 3
    runner.tick()                       # observes 2; source tick 2: tick 0 due at 3 > 2
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "pending"
    out = runner.tick()                 # waiting at 3: the producer is still INSIDE tick 3
    assert out.get("waiting") is True   # and may publish tick 0's labels before it closes
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "pending" and row["final"] is False, row
    fake_producer.latest = 4
    runner.tick()                       # observes 3; source tick 3: tick 0 due at 3 <= 3
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "no_labels" and row["final"] is True
    assert "due at tick 3" in row["reason"] and "by tick 3" in row["reason"]
    assert row["value"] is None
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "pending"   # due at 4 > 3
    # final: labels published after the due tick are not picked up, and the producer is
    # not asked for that window again
    fake_producer.release_labels(0)
    fake_producer.calls.clear()
    runner.tick()
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "no_labels"
    assert ("/telemetry/inferences", {"tick": 0}) not in fake_producer.calls
    assert db.latest_realized(UC, KEY) is None


def test_pending_ticks_that_left_the_window_are_swept(isolated_db, fake_producer, monkeypatch):
    """A producer outage spanning the whole window must not leave `pending` rows forever."""
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()                       # ticks 0, 1 pending
    real_pull = fake_producer.pull

    def labels_down(base, path, params=None, timeout=30.0):
        if path == "/telemetry/labels":
            raise ConnectionError("labels service down")
        return real_pull(base, path, params, timeout)

    monkeypatch.setattr(live_http, "pull", labels_down)
    fake_producer.latest = 8
    for _ in range(6):
        runner.tick()                   # observes 2..7; every backfill cycle breaks
    assert db.get_live_source(UC)["next_tick"] == 8
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "pending"
    assert db.get_realized_metric(UC, 7, KEY)["status"] == "pending"   # labels pull failed
    monkeypatch.setattr(live_http, "pull", real_pull)
    out = runner.tick()                 # waiting at 8: window [4, 8) plus stragglers 0..3
    assert out.get("waiting") is True
    for t in range(5):                  # due ticks 3..7 have closed by source tick 7
        row = db.get_realized_metric(UC, t, KEY)
        assert row["status"] == "no_labels" and row["final"] is True, (t, row)
    for t in (5, 6, 7):                 # due at 8, 9, 10: tick 8 is still open
        row = db.get_realized_metric(UC, t, KEY)
        assert row["status"] == "pending" and row["final"] is False, (t, row)
    fake_producer.calls.clear()
    runner.tick()
    assert all(params.get("tick") not in range(5)
               for path, params in fake_producer.calls if params)
    fake_producer.latest = 9            # tick 8 closes: tick 5 (due 8) is now overdue
    runner.tick()                       # observes 8
    assert db.get_realized_metric(UC, 5, KEY)["final"] is True
    assert db.get_realized_metric(UC, 6, KEY)["status"] == "pending"


def test_labels_404_before_due_tick_is_pending_and_retried(isolated_db, fake_producer, monkeypatch):
    """Only the inference re-pull proves eviction; a 404 on the labels window is not final."""
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    real_pull = fake_producer.pull

    def labels_404(base, path, params=None, timeout=30.0):
        if path == "/telemetry/labels":
            raise telemetry_http.WindowEvicted(f"{path} tick={params['tick']} not available (404)")
        return real_pull(base, path, params, timeout)

    monkeypatch.setattr(live_http, "pull", labels_404)
    fake_producer.latest = 3
    runner.tick()                       # observes 2; backfills 0, 1 (due 3, 4 > 2)
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "pending" and "404" in row["reason"] and row["final"] is False
    assert db.get_live_source(UC)["state"] != "error"
    monkeypatch.setattr(live_http, "pull", real_pull)
    fake_producer.release_labels(0)
    runner.tick()                       # waiting at 3: retried and realized
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "realized"


def test_labels_404_after_due_tick_is_final_no_labels(isolated_db, fake_producer, monkeypatch):
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    real_pull = fake_producer.pull

    def labels_404(base, path, params=None, timeout=30.0):
        if path == "/telemetry/labels":
            raise telemetry_http.WindowEvicted(f"{path} tick={params['tick']} not available (404)")
        return real_pull(base, path, params, timeout)

    monkeypatch.setattr(live_http, "pull", labels_404)
    fake_producer.latest = 3
    runner.tick()                       # observes 2; source tick 2: tick 0 due at 3 > 2
    runner.tick()                       # waiting at 3: tick 3 still open -> still pending
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "pending" and row["final"] is False, row
    fake_producer.latest = 4
    runner.tick()                       # observes 3; source tick 3: tick 0 due at 3 <= 3
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "no_labels" and row["final"] is True and "404" in row["reason"]
    assert "none by tick 3" in row["reason"]
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "pending"   # due at 4 > 3


def test_labels_window_without_available_at_uses_monitor_due_tick(isolated_db, fake_producer):
    """A producer that omits `available_at_tick` (v1.0) must not leave a non-final
    `no_labels` row: the tick is `pending` until the monitor's own due tick (`t + L`)
    closes, then final `no_labels`, the same as a 404 on the labels window."""
    fake_producer.omit_available_at = True
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()
    runner.tick()                       # ticks 0, 1 observed; monitor due ticks 3, 4
    fake_producer.latest = 3
    runner.tick()                       # observes 2; backfills 0, 1 with source tick 2
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "pending" and row["reason"] == "label lag" and row["final"] is False
    runner.tick()                       # waiting at 3: still pending (tick 3 is open)
    assert db.get_realized_metric(UC, 0, KEY)["status"] == "pending"
    fake_producer.latest = 4
    runner.tick()                       # observes 3; source tick 3 >= due tick 3
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "no_labels" and row["final"] is True
    assert "due at tick 3" in row["reason"] and "available_at_tick" in row["reason"]
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "pending"
    fake_producer.release_labels(1)
    runner.tick()                       # waiting at 4: labels still land before the due tick
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "realized"


def test_empty_and_undersized_windows_are_final_and_not_repulled(isolated_db, fake_producer):
    """No label can ever realize a `count=0` or undersized window, so the backfill marks
    it final at once instead of re-pulling its inferences for the whole window."""
    fake_producer.empty.add(0)
    runner = _runner()
    fake_producer.latest = 2
    runner.tick()                       # observes 0 (empty)
    runner.tick()                       # observes 1; backfills 0
    row = db.get_realized_metric(UC, 0, KEY)
    assert row["status"] == "no_labels" and row["reason"] == "empty window"
    assert row["final"] is True and row["value"] is None
    fake_producer.calls.clear()
    runner.tick()                       # waiting at 2
    assert ("/telemetry/inferences", {"tick": 0}) not in fake_producer.calls
    assert ("/telemetry/labels", {"tick": 0}) not in fake_producer.calls

    # undersized: tick 1 is pending (600 records >= 500); raise the window size the
    # adapter demands so the same window is now too small to realize
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "pending"
    runner.ml.chunk_size = 10_000
    runner.tick()                       # waiting at 2: backfills 1 as undersized
    row = db.get_realized_metric(UC, 1, KEY)
    assert row["status"] == "no_labels" and row["reason"].startswith("insufficient sample")
    assert row["final"] is True
    fake_producer.calls.clear()
    runner.tick()
    assert ("/telemetry/inferences", {"tick": 1}) not in fake_producer.calls


def test_nba_overdue_tick_is_final_for_both_keys(isolated_db, fake_producer):
    runner = live_runner_module.LiveNBARunner(nba_url="https://producer.test")
    fake_producer.latest = 2
    runner.tick()
    runner.tick()
    fake_producer.latest = 5
    runner.tick()
    runner.tick()
    for key in (KEY, "acceptance_rate"):
        row = db.get_realized_metric("AICT-L03", 0, key)
        assert row["status"] == "no_labels" and row["final"] is True, (key, row)
