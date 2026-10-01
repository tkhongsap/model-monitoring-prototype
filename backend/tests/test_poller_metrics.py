"""Structured logging and poll-cycle metrics (spec D.2)."""
from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from app import config, db, live_poller, logging_setup, main
from app.api import live_portfolio
from app.scenario import live_runner as live_runner_module

UC = "AICT-L01"


class OkRunner:
    def tick(self):
        return {"use_case_id": UC, "tick": 4, "observed_tick": 4, "backlog": 2,
                "state": "catching_up", "signals": {}, "lanes": {}, "overall": "Unknown"}

    def state(self):
        return {"use_case_id": UC, "tick": 4, "observation_id": "obs-4", "signals": {},
                "lanes": {"Quality": "Green"}, "overall": "Green"}


class WaitingRunner(OkRunner):
    def tick(self):
        return {"use_case_id": UC, "tick": None, "waiting": True, "backlog": 0}


class HeldRunner(OkRunner):
    def tick(self):
        return {"use_case_id": UC, "tick": 4, "cursor_held": True, "state": "error",
                "errors": {"telemetry": "ConnectError: refused"}}


class BrokenRunner(OkRunner):
    def tick(self):
        raise RuntimeError("engine exploded")


def _cycle(monkeypatch, runner) -> dict:
    monkeypatch.setattr(live_portfolio, "LIVE_UCS", [UC])
    monkeypatch.setattr(live_runner_module, "live_runner", lambda uc: runner)
    worker = live_poller.LivePoller()
    try:
        assert worker.run_once() is True
    finally:
        db.release_live_lease(live_poller.LEASE_NAME, worker.owner_id)
    assert worker.last_cycle is not None
    return worker.last_cycle


def test_run_once_records_per_source_metrics(isolated_db, monkeypatch):
    cycle = _cycle(monkeypatch, OkRunner())
    source = cycle["sources"][UC]
    assert source["outcome"] == "ok"
    assert source["tick"] == 4 and source["backlog"] == 2
    assert source["duration_ms"] >= 0 and source["error"] is None
    assert cycle["outcome"] == "ok" and cycle["duration_ms"] >= 0
    assert cycle["cycle_id"] and cycle["finished_at"] >= cycle["started_at"]
    assert cycle["backlog"] == 2


def test_run_once_records_waiting_held_and_error_outcomes(isolated_db, monkeypatch):
    assert _cycle(monkeypatch, WaitingRunner())["sources"][UC]["outcome"] == "waiting"
    held = _cycle(monkeypatch, HeldRunner())["sources"][UC]
    assert held["outcome"] == "held" and "refused" in held["error"]
    broken = _cycle(monkeypatch, BrokenRunner())
    assert broken["sources"][UC]["outcome"] == "error"
    assert "engine exploded" in broken["sources"][UC]["error"]
    assert broken["outcome"] == "error"
    assert "engine exploded" in db.get_live_source(UC)["last_error"]


def test_cycle_is_logged_with_structured_fields(isolated_db, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="app.live_poller"):
        _cycle(monkeypatch, OkRunner())
    records = [r for r in caplog.records if getattr(r, "source_id", None) == UC]
    assert records, "one source log line per cycle"
    record = records[-1]
    assert record.cycle_id and record.tick == 4 and record.outcome == "ok"
    assert record.backlog == 2 and record.duration_ms >= 0


def test_json_formatter_emits_one_object_per_line():
    formatter = logging_setup.JsonFormatter()
    record = logging.LogRecord("app.live_poller", logging.INFO, __file__, 1, "cycle",
                               None, None)
    record.cycle_id, record.source_id, record.tick = "c1", UC, 4
    record.duration_ms, record.outcome, record.backlog = 12, "ok", 0
    line = formatter.format(record)
    body = json.loads(line)
    assert "\n" not in line
    assert body["message"] == "cycle" and body["level"] == "INFO"
    assert body["logger"] == "app.live_poller" and body["ts"]
    assert {k: body[k] for k in ("cycle_id", "source_id", "tick", "duration_ms",
                                 "outcome", "backlog")} == {
        "cycle_id": "c1", "source_id": UC, "tick": 4, "duration_ms": 12,
        "outcome": "ok", "backlog": 0}


def test_configure_is_idempotent_and_switches_format():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        logging_setup.configure("json")
        logging_setup.configure("json")
        ours = [h for h in root.handlers if getattr(h, "_control_tower", False)]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, logging_setup.JsonFormatter)
        logging_setup.configure("plain")
        ours = [h for h in root.handlers if getattr(h, "_control_tower", False)]
        assert len(ours) == 1
        assert not isinstance(ours[0].formatter, logging_setup.JsonFormatter)
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_control_tower", False):
                root.removeHandler(handler)
        for handler in before:
            if handler not in root.handlers:
                root.addHandler(handler)


def test_readiness_exposes_last_cycle(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    cycle = {"cycle_id": "c9", "started_at": 1.0, "finished_at": 1.5, "duration_ms": 500,
             "outcome": "error", "backlog": 0,
             "sources": {UC: {"tick": 3, "duration_ms": 400, "outcome": "error",
                              "backlog": 0, "error": "RuntimeError: boom"}}}
    monkeypatch.setattr(main.poller(), "last_cycle", cycle)
    with TestClient(main.app) as client:
        body = client.get("/api/readiness").json()
    assert body["poller"]["last_cycle"] == cycle
    assert body["poller"]["last_cycle"]["sources"][UC]["error"] == "RuntimeError: boom"
