"""Webhook delivery for live alerts (spec C.3): payload shape, at-least-once, redaction."""
from __future__ import annotations

import json
import logging

import pytest

from app import alert_delivery, config, db

UC = "AICT-L01"
WEBHOOK = "https://hooks.example.test/services/T000/B000/SECRETPATH"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakePost:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def __call__(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture()
def webhook(monkeypatch):
    monkeypatch.setattr(config, "LIVE_ALERT_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(config, "LIVE_DASHBOARD_URL", "https://monitor.example.test")
    return WEBHOOK


def _alert(**overrides) -> dict:
    row = {
        "alert_id": "a1", "source_id": UC, "lane": "Quality", "from_health": "Amber",
        "to_health": "Red", "tick": 12, "observation_id": "obs-12", "opened_at": 1.0,
        "resolved_at": None, "resolved_tick": None, "open_delivery_status": "pending",
        "open_delivery_error": None, "open_delivered_at": None,
        "resolve_delivery_status": "n/a", "resolve_delivery_error": None,
        "resolve_delivered_at": None,
        # never part of a real row, but the payload must not pass through anything extra
        "features": {"tenure": 3}, "question": "what is my balance",
    }
    row.update(overrides)
    return row


def test_open_payload_shape(webhook):
    payload = alert_delivery.build_payload(_alert(), "open")
    assert payload["text"] == "[RED] AICT-L01 Quality: Amber → Red at tick 12"
    assert isinstance(payload["blocks"], list) and payload["blocks"]
    alert = payload["alert"]
    assert alert == {
        "alert_id": "a1", "use_case_id": UC, "lane": "Quality", "from_health": "Amber",
        "to_health": "Red", "tick": 12, "observation_id": "obs-12", "phase": "open",
        "opened_at": 1.0, "resolved_at": None, "resolved_tick": None,
        "dashboard_url": "https://monitor.example.test/use-case/AICT-L01",
    }
    dumped = json.dumps(payload)
    assert "features" not in dumped and "question" not in dumped and "balance" not in dumped
    assert "http" not in payload["text"]
    assert "SECRETPATH" not in dumped and "hooks.example.test" not in dumped


def test_resolve_payload_and_first_open_without_previous_health(webhook, monkeypatch):
    payload = alert_delivery.build_payload(
        _alert(resolved_at=2.0, resolved_tick=15), "resolve")
    assert payload["text"] == "[RESOLVED] AICT-L01 Quality: Red → Green at tick 15"
    assert payload["alert"]["phase"] == "resolve" and payload["alert"]["resolved_tick"] == 15
    monkeypatch.setattr(config, "LIVE_DASHBOARD_URL", "")
    payload = alert_delivery.build_payload(_alert(from_health=None, lane="overall"), "open")
    assert payload["text"] == "[RED] AICT-L01 overall: — → Red at tick 12"
    assert payload["alert"]["dashboard_url"] is None


def test_delivery_retries_then_marks_once(isolated_db, webhook, caplog):
    alert_id = db.open_alert(UC, "Quality", "Amber", "Red", 12, "obs-12")
    post = FakePost(500, ConnectionError(f"boom connecting to {WEBHOOK}"), 200)
    with caplog.at_level(logging.INFO, logger="app.alert_delivery"):
        assert alert_delivery.deliver_pending(post=post) == 0
        row = db.list_alerts(UC)[0]
        assert row["open_delivery_status"] == "error"
        assert row["open_delivery_error"] == "HTTP 500"
        assert alert_delivery.deliver_pending(post=post) == 0
        stored_error = db.list_alerts(UC)[0]["open_delivery_error"]
        assert stored_error.startswith("ConnectionError")
        assert "SECRETPATH" not in stored_error and "<webhook>" in stored_error
        assert alert_delivery.deliver_pending(post=post) == 1
        assert alert_delivery.deliver_pending(post=post) == 0      # nothing left: no 4th post
    row = db.list_alerts(UC)[0]
    assert row["alert_id"] == alert_id
    assert row["open_delivery_status"] == "delivered" and row["open_delivered_at"] is not None
    assert row["open_delivery_error"] is None
    assert len(post.calls) == 3
    assert all(c["url"] == WEBHOOK and c["timeout"] == 10 for c in post.calls)
    assert post.calls[-1]["json"]["alert"]["alert_id"] == alert_id
    # logs name the host only: never the URL path, never the body
    assert caplog.records, "delivery must be logged"
    for record in caplog.records:
        message = record.getMessage()
        assert "hooks.example.test" in message
        assert "SECRETPATH" not in message and "/services/" not in message
        assert "Quality" not in message and "obs-12" not in message


def test_resolve_is_delivered_as_its_own_notification(isolated_db, webhook):
    alert_id = db.open_alert(UC, "Quality", "Green", "Amber", 3, "obs-3")
    post = FakePost()
    assert alert_delivery.deliver_pending(post=post) == 1
    db.resolve_alerts(UC, "Quality", 5)
    assert alert_delivery.deliver_pending(post=post) == 1
    assert alert_delivery.deliver_pending(post=post) == 0
    assert [c["json"]["alert"]["phase"] for c in post.calls] == ["open", "resolve"]
    assert post.calls[1]["json"]["text"].startswith("[RESOLVED]")
    row = db.list_alerts(UC)[0]
    assert row["alert_id"] == alert_id
    assert row["resolve_delivery_status"] == "delivered"


def test_no_webhook_configured_marks_skipped(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_ALERT_WEBHOOK_URL", "")
    db.open_alert(UC, "Quality", "Green", "Red", 1, "obs-1")
    post = FakePost()
    assert alert_delivery.deliver_pending(post=post) == 0
    assert post.calls == []
    row = db.list_alerts(UC)[0]
    assert row["open_delivery_status"] == "skipped"
    assert "LIVE_ALERT_WEBHOOK_URL" in row["open_delivery_error"]
    assert db.alerts_pending_delivery() == []


def test_delivery_failure_never_raises(isolated_db, webhook):
    db.open_alert(UC, "Quality", "Green", "Red", 1, "obs-1")
    assert alert_delivery.deliver_pending(post=FakePost(RuntimeError("down"))) == 0
    assert db.list_alerts(UC)[0]["open_delivery_status"] == "error"
