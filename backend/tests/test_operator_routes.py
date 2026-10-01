"""Operator unstick endpoints (spec D.3): POST /api/live/sources/{uc}/skip and /reset-ack.

Both sit behind the worker token and the strict-live middleware; every other POST under
/api/live/ stays unreachable.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, db, main
from app.api import live_routes
from app.scenario import live_runner as live_runner_module

UC = "AICT-L01"
TOKEN = "worker-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(isolated_db, monkeypatch) -> TestClient:
    """The strict router behind the real strict-live middleware (see test_alert_routes)."""
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LIVE_WORKER_TOKEN", TOKEN)
    assert config.strict_live_mode()
    strict_app = FastAPI()
    strict_app.include_router(live_routes.router)
    strict_app.middleware("http")(main.strict_live_route_isolation)
    with TestClient(strict_app) as c:
        yield c


def _hold_cursor(next_tick: int = 5, error: str = "TelemetryIntegrityError: digest") -> dict:
    """A source whose cursor is held at `next_tick` after an error, as the runner leaves it."""
    db.ensure_live_source(UC)
    db.put_live_observation(UC, {"tick": next_tick - 1, "window_id": f"w{next_tick - 1}",
                                 "content_sha256": "a" * 64, "record_count": 500,
                                 "signals": {}, "overall": "Green"},
                            source_tick=next_tick + 2, next_tick=next_tick, backlog=3,
                            state="catching_up")
    return db.mark_live_error(UC, error)


def test_skip_requires_worker_token(client):
    _hold_cursor()
    assert client.post(f"/api/live/sources/{UC}/skip", json={"reason": "x"}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post(f"/api/live/sources/{UC}/skip", json={"reason": "x"},
                       headers=wrong).status_code == 401
    assert client.post(f"/api/live/sources/{UC}/reset-ack", headers=wrong).status_code == 401
    assert db.get_live_source(UC)["next_tick"] == 5          # nothing moved


def test_skip_requires_held_cursor(client):
    """Review focus 5: a healthy source is never skipped; the cursor never advances."""
    db.ensure_live_source(UC)
    db.put_live_observation(UC, {"tick": 4, "window_id": "w4", "content_sha256": "b" * 64,
                                 "record_count": 500, "signals": {}, "overall": "Green"},
                            source_tick=4, next_tick=5, backlog=0, state="at_tail")
    response = client.post(f"/api/live/sources/{UC}/skip", json={"reason": "operator"},
                           headers=AUTH)
    assert response.status_code == 409
    assert "at_tail" in response.json()["detail"]
    cursor = db.get_live_source(UC)
    assert cursor["next_tick"] == 5 and cursor["state"] == "at_tail"
    assert db.get_live_observation_by_tick(UC, 5) is None


def test_skip_advances_and_audits(client, monkeypatch):
    _hold_cursor(next_tick=5)
    sentinel = object()
    monkeypatch.setattr(live_runner_module, "_ml_runner", sentinel)
    response = client.post(f"/api/live/sources/{UC}/skip",
                           json={"reason": "producer rewrote window w5"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped_tick"] == 5 and body["next_tick"] == 6
    assert body["reason"] == "producer rewrote window w5"

    cursor = db.get_live_source(UC)
    assert cursor["next_tick"] == 6 and cursor["state"] != "error"
    assert cursor["last_error"] is None

    stub = db.get_live_observation_by_tick(UC, 5)
    assert stub is not None and stub["observation_id"] == body["observation_id"]
    assert stub["record_count"] == 0
    payload = stub["payload"]
    assert payload["skipped"] is True and payload["record_count"] == 0
    assert payload["skip_reason"] == "producer rewrote window w5"
    assert payload["errors"] == {"skipped": "producer rewrote window w5"}
    assert payload["overall"] == "Unknown" and set(payload["lanes"].values()) == {"Unknown"}
    assert payload["window_id"] == f"{UC}:skipped-t5"
    # the backfill must never re-pull the poisoned window: realized rows are final
    realized = db.get_realized_metric(UC, 5, "realized_roc_auc")
    assert realized["status"] == "no_labels" and realized["final"] is True
    assert db.ticks_needing_realization(UC, "realized_roc_auc", 5, 6) == []
    # the in-memory runner reloads its cursor
    assert live_runner_module._ml_runner is None
    # the stub is the newest observation: the views and alerting must render it
    from app import alerting
    from app.api import live_portfolio
    detail = client.get(f"/api/live/use-case/{UC}")
    assert detail.status_code == 200
    assert detail.json()["skipped"] is True and detail.json()["overall"] == "Unknown"
    assert live_portfolio.portfolio_summary()["open_alerts"] == {"Red": 0, "Amber": 0}
    alerting.evaluate(UC)
    assert db.list_alerts(UC) == []                     # Unknown never opens an alert


def test_skip_stub_is_never_acknowledged_to_the_producer(client, monkeypatch):
    """The stub's window was never served by the producer: with a producer configured
    it must not be stored `pending` and the ack retry loop must never POST it."""
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "https://producer.example")
    _hold_cursor(next_tick=5)
    real, _ = db.put_live_observation(
        UC, {"tick": 3, "window_id": "w3", "content_sha256": "b" * 64, "record_count": 500,
             "signals": {}, "overall": "Green"},
        source_tick=7, next_tick=5, backlog=3, state="catching_up")
    db.mark_live_error(UC, "TelemetryIntegrityError: digest")
    response = client.post(f"/api/live/sources/{UC}/skip",
                           json={"reason": "poisoned window"}, headers=AUTH)
    assert response.status_code == 200, response.text
    stub = db.get_live_observation_by_tick(UC, 5)
    assert stub["ack_status"] == "skipped" and stub["ack_error"] is None
    retriable = {r["observation_id"] for r in db.list_live_acks_to_retry()}
    assert stub["observation_id"] not in retriable
    assert real in retriable                                 # genuine windows still retry

    posted: list[str] = []

    def fake_ack(base_url, *, window_id, observation_id, content_sha256):
        posted.append(window_id)

    monkeypatch.setattr(live_runner_module, "acknowledge_observation", fake_ack)
    summary = live_runner_module.retry_pending_acknowledgements()
    assert f"{UC}:skipped-t5" not in posted
    assert summary["attempted"] == summary["acknowledged"] == len(posted) > 0
    assert db.get_live_observation_by_tick(UC, 5)["ack_status"] == "skipped"
    # reset-ack leaves the stub alone too (nothing poisoned to abandon)
    client.post(f"/api/live/sources/{UC}/reset-ack", headers=AUTH)
    assert db.get_live_observation_by_tick(UC, 5)["ack_status"] == "skipped"


def test_skip_is_one_transaction(client, monkeypatch):
    """If finalising the realized rows fails, the cursor must still be held so the
    operator can retry the skip; a half-applied skip would let the backfill re-pull the
    poisoned window."""
    _hold_cursor(next_tick=5)

    def boom(cx, *args, **kwargs):
        raise RuntimeError("realized write failed")

    # scope the failure injection so the fixture patches (live mode, isolated DB,
    # worker token) stay in force for the successful retry below
    with pytest.MonkeyPatch.context() as failing:
        failing.setattr(db, "_put_realized_metric", boom)
        with pytest.raises(RuntimeError, match="realized write failed"):
            client.post(f"/api/live/sources/{UC}/skip", json={"reason": "x"}, headers=AUTH)
    cursor = db.get_live_source(UC)
    assert cursor["state"] == "error" and cursor["next_tick"] == 5
    assert db.get_live_observation_by_tick(UC, 5) is None
    assert db.get_realized_metric(UC, 5, "realized_roc_auc") is None

    response = client.post(f"/api/live/sources/{UC}/skip", json={"reason": "x"}, headers=AUTH)
    assert response.status_code == 200, response.text   # the retry succeeds
    assert db.get_live_source(UC)["next_tick"] == 6
    assert db.get_realized_metric(UC, 5, "realized_roc_auc")["final"] is True


def test_skip_rejects_blank_reason_and_unknown_use_case(client):
    _hold_cursor()
    assert client.post(f"/api/live/sources/{UC}/skip", json={"reason": "  "},
                       headers=AUTH).status_code == 422
    assert client.post(f"/api/live/sources/{UC}/skip", json={}, headers=AUTH).status_code == 422
    assert client.post("/api/live/sources/AICT-L99/skip", json={"reason": "x"},
                       headers=AUTH).status_code == 404
    assert client.post("/api/live/sources/AICT-L99/reset-ack", headers=AUTH).status_code == 404
    assert db.get_live_source(UC)["next_tick"] == 5


def test_reset_ack(client, monkeypatch):
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "https://producer.example")
    observation_id, _ = db.put_live_observation(
        UC, {"tick": 0, "window_id": "w0", "content_sha256": "c" * 64, "record_count": 500,
             "signals": {}, "overall": "Green"},
        source_tick=0, next_tick=1, backlog=0, state="at_tail")
    db.set_live_ack(observation_id, ok=False, error="HTTP 500")
    other, _ = db.put_live_observation(
        "AICT-L03", {"tick": 0, "window_id": "w0", "content_sha256": "d" * 64,
                     "record_count": 500, "signals": {}, "overall": "Green"},
        source_tick=0, next_tick=1, backlog=0, state="at_tail")
    assert {r["observation_id"] for r in db.list_live_acks_to_retry()} == {observation_id, other}

    response = client.post(f"/api/live/sources/{UC}/reset-ack", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"use_case_id": UC, "abandoned": 1}
    assert [r["observation_id"] for r in db.list_live_acks_to_retry()] == [other]
    assert db.get_latest_live_observation(UC)["ack_status"] == "abandoned"
    assert db.get_live_source(UC)["next_tick"] == 1          # reset-ack never moves a cursor


def test_other_live_posts_are_still_unreachable(client):
    for path in ("/api/live/alerts", "/api/live/observations", "/api/live/tick-all",
                 "/api/live/reset", "/api/live/sync", f"/api/live/use-case/{UC}",
                 "/api/live/sources", f"/api/live/sources/{UC}/anything",
                 f"/api/live/sources/{UC}/skip/extra"):
        response = client.post(path, headers=AUTH)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "not available in strict live mode"}, path
    assert client.get("/api/live/alerts").status_code == 200
