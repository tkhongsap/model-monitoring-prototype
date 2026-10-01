"""GET /api/live/alerts and the alert fields in the live detail/portfolio (spec C.2)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, db, main
from app.api import live_routes

UC = "AICT-L01"
ALERT_COLUMNS = {
    "alert_id", "source_id", "lane", "from_health", "to_health", "tick", "observation_id",
    "opened_at", "resolved_at", "resolved_tick", "open_delivery_status",
    "open_delivery_error", "open_delivered_at", "resolve_delivery_status",
    "resolve_delivery_error", "resolve_delivered_at",
}


def _client(monkeypatch) -> TestClient:
    """The strict router behind the real strict-live middleware.

    `app.main` picks its router when first imported, which another test may have done in
    demo mode, so the strict app is assembled here from the same two pieces production
    uses (`isolated_db` already put the configuration in live mode)."""
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "LIVE_ALERT_WEBHOOK_URL", "https://hooks.example.test/secret")
    assert config.strict_live_mode()
    strict_app = FastAPI()
    strict_app.include_router(live_routes.router)
    strict_app.middleware("http")(main.strict_live_route_isolation)
    return TestClient(strict_app)


def test_dev_router_serves_the_same_alert_listing(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    alert_id = db.open_alert(UC, "Quality", "Green", "Red", 1, "obs-1")
    with TestClient(main.app) as client:  # whichever router main mounted at import
        body = client.get("/api/live/alerts", params={"uc": UC}).json()
        assert [r["alert_id"] for r in body["rows"]] == [alert_id]
        assert body["redacted"] is True


def test_alerts_route_lists_filters_and_redacts(isolated_db, fake_producer, monkeypatch):
    a = db.open_alert(UC, "Quality", "Green", "Amber", 2, "obs-2")
    b = db.open_alert(UC, "Quality", "Amber", "Red", 3, "obs-3")
    c = db.open_alert("AICT-L03", "overall", None, "Red", 1, "obs-x")
    db.resolve_alerts(UC, "Quality", 4)
    db.mark_alert_delivery(c, "open", ok=False, error="HTTP 500")
    with _client(monkeypatch) as client:
        body = client.get("/api/live/alerts").json()
        assert body["contract_version"] == "1.1" and body["redacted"] is True
        assert [r["alert_id"] for r in body["rows"]] == [c, b, a]       # newest first
        assert all(set(r) == ALERT_COLUMNS for r in body["rows"])
        assert "hooks.example.test" not in client.get("/api/live/alerts").text

        rows = client.get("/api/live/alerts", params={"uc": UC}).json()["rows"]
        assert {r["alert_id"] for r in rows} == {a, b}
        assert all(r["resolved_tick"] == 4 and r["resolved_at"] for r in rows)

        rows = client.get("/api/live/alerts", params={"open": "true"}).json()["rows"]
        assert [r["alert_id"] for r in rows] == [c]
        assert rows[0]["open_delivery_status"] == "error"
        assert rows[0]["open_delivery_error"] == "HTTP 500"

        rows = client.get("/api/live/alerts", params={"limit": 1}).json()["rows"]
        assert len(rows) == 1
        assert client.get("/api/live/alerts", params={"uc": "AICT-L99"}).status_code == 404
        assert client.post("/api/live/alerts").status_code == 404      # strict-live: GET only
        assert client.delete(f"/api/live/alerts/{a}").status_code == 404


def test_detail_carries_open_alerts_and_portfolio_counts_them(isolated_db, fake_producer,
                                                              monkeypatch):
    fake_producer.latest = 1
    from app.scenario import live_runner as live_runner_module
    live_runner_module.LiveRunner(churn_url="https://producer.test").tick()
    amber = db.open_alert(UC, "Quality", "Green", "Amber", 0, "obs-0")
    red = db.open_alert(UC, "overall", "Amber", "Red", 0, "obs-0")
    resolved = db.open_alert(UC, "Drift & degradation", "Green", "Red", 0, "obs-0")
    db.resolve_alerts(UC, "Drift & degradation", 0)
    db.open_alert("AICT-L03", "Quality", "Green", "Red", 0, "obs-y")
    with _client(monkeypatch) as client:
        detail = client.get(f"/api/live/use-case/{UC}").json()
        assert "actions" not in detail
        assert {r["alert_id"] for r in detail["alerts"]} == {amber, red}
        assert resolved not in {r["alert_id"] for r in detail["alerts"]}
        assert all(set(r) == ALERT_COLUMNS for r in detail["alerts"])

        never_observed = client.get("/api/live/use-case/AICT-L02").json()
        assert never_observed["waiting"] is True and never_observed["alerts"] == []
        assert "actions" not in never_observed

        portfolio = client.get("/api/live/portfolio").json()
        assert portfolio["open_alerts"] == {"Red": 2, "Amber": 1}
