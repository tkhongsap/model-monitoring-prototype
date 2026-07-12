from __future__ import annotations

import time
import threading

import pytest

from app import config, db
from app import live_sync
from app import live_poller
from app.api import live_portfolio
from app.scenario import live_runner as live_runner_module


@pytest.fixture()
def isolated_live_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live.db")
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "")
    db.reset_engine()
    yield tmp_path / "live.db"
    db.reset_engine()


def _payload(digest: str = "a" * 64) -> dict:
    return {
        "use_case_id": "AICT-L01", "tick": 0, "observed_tick": 0,
        "window_id": "churn-window-0", "source_instance_id": "producer-a",
        "batch_id": "poc-20260712-1200",
        "opened_at": "2026-07-12T00:00:00Z", "closed_at": "2026-07-12T00:05:00Z",
        "content_sha256": digest, "first_record_id": "inf-1", "last_record_id": "inf-500",
        "model_version": 1, "provenance_counts": {"synthetic_demo": 500},
        "record_count": 500, "overall": "Green", "lanes": {"Quality": "Green"},
        "signals": {"estimated_roc_auc": {"value": 0.88, "health": "Green"}},
        "errors": {},
    }


def test_observation_cursor_history_survive_restart(isolated_live_db):
    observation_id, inserted = db.put_live_observation(
        "AICT-L01", _payload(), source_tick=2, next_tick=1,
        backlog=2, state="catching_up")
    assert inserted is True
    assert db.get_live_source("AICT-L01")["next_tick"] == 1
    assert db.get_live_signal_history("AICT-L01", "estimated_roc_auc", 10) == [
        {"tick": 0, "value": 0.88, "health": "Green"}]

    db.reset_engine()  # process restart against the same SQLite file
    latest = db.get_latest_live_observation("AICT-L01")
    assert latest["observation_id"] == observation_id
    assert latest["window_id"] == "churn-window-0"
    assert latest["batch_id"] == "poc-20260712-1200"
    assert latest["content_sha256"] == "a" * 64
    assert latest["provenance_counts"] == {"synthetic_demo": 500}
    assert db.get_live_source("AICT-L01")["observed_tick"] == 0
    assert live_sync.source_sync("AICT-L01")["batch_id"] == "poc-20260712-1200"


def test_window_dedup_and_digest_immutability(isolated_live_db):
    first, inserted = db.put_live_observation(
        "AICT-L01", _payload(), source_tick=0, next_tick=1, backlog=0, state="at_tail")
    second, inserted_again = db.put_live_observation(
        "AICT-L01", _payload(), source_tick=0, next_tick=1, backlog=0, state="at_tail")
    assert second == first
    assert inserted is True and inserted_again is False
    assert len(db.list_live_observations("AICT-L01")) == 1
    with pytest.raises(db.WindowDigestMismatch):
        db.put_live_observation(
            "AICT-L01", _payload("b" * 64), source_tick=0, next_tick=1,
            backlog=0, state="at_tail")


def test_renewable_lease_has_one_owner(isolated_live_db):
    assert db.acquire_live_lease("poller", "worker-a", 30) is True
    assert db.acquire_live_lease("poller", "worker-a", 30) is True  # renewal
    assert db.acquire_live_lease("poller", "worker-b", 30) is False
    db.release_live_lease("poller", "worker-a")
    assert db.acquire_live_lease("poller", "worker-b", 30) is True


def test_poller_heartbeat_keeps_lease_during_slow_tick(isolated_live_db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_POLL_LEASE_SECONDS", 0.3)
    monkeypatch.setattr(live_portfolio, "LIVE_UCS", ["AICT-L01"])
    started, finish = threading.Event(), threading.Event()

    class SlowRunner:
        def tick(self):
            started.set()
            assert finish.wait(3)
            return {"waiting": True}

    monkeypatch.setattr(live_runner_module, "live_runner", lambda uc: SlowRunner())
    worker = live_poller.LivePoller()
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(worker.run_once()))
    thread.start()
    try:
        assert started.wait(1)
        first_expiry = db.get_live_lease(live_poller.LEASE_NAME)["expires_at"]
        time.sleep(0.6)
        renewed_expiry = db.get_live_lease(live_poller.LEASE_NAME)["expires_at"]
        assert renewed_expiry > first_expiry
        time.sleep(0.8)  # total exceeds the store's one-second minimum lease duration
        assert db.acquire_live_lease(
            live_poller.LEASE_NAME, "competing-worker", 30) is False
    finally:
        finish.set()
        thread.join(timeout=3)
        db.release_live_lease(live_poller.LEASE_NAME, worker.owner_id)
    assert result == [True]


def test_sync_state_reports_tail_idle_stale_and_error(isolated_live_db, monkeypatch):
    real_time = time.time
    db.put_live_observation(
        "AICT-L01", _payload(), source_tick=0, next_tick=1, backlog=0, state="at_tail")
    row = db.get_live_source("AICT-L01")
    base = max(row["last_checked_at"], row["last_success_at"])
    monkeypatch.setattr(config, "LIVE_IDLE_SECONDS", 10)
    monkeypatch.setattr(config, "LIVE_STALE_SECONDS", 30)

    monkeypatch.setattr(live_sync.time, "time", lambda: base + 1)
    assert live_sync.source_sync("AICT-L01")["sync_state"] == "at_tail"
    monkeypatch.setattr(live_sync.time, "time", lambda: base + 20)
    assert live_sync.source_sync("AICT-L01")["sync_state"] == "idle"
    monkeypatch.setattr(live_sync.time, "time", lambda: base + 40)
    assert live_sync.source_sync("AICT-L01")["sync_state"] == "stale"

    # Restore real time before recording the error; source_sync gives explicit errors
    # precedence over age-derived states.
    monkeypatch.setattr(live_sync.time, "time", real_time)
    db.mark_live_error("AICT-L01", "producer unavailable")
    assert live_sync.source_sync("AICT-L01")["sync_state"] == "error"
