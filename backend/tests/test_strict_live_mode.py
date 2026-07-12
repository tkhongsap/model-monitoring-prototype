from __future__ import annotations

from fastapi.testclient import TestClient

from app import config, db
from app.main import app


def test_strict_live_hides_baked_routes_and_poison_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "strict.db")
    monkeypatch.setattr(config, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", True)
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 0)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    db.reset_engine()
    poison = "BAKED-POISON-8afcc48d"
    db.put_baked_tick("DEMO-FULL", 0, {"sentinel": poison})
    artifact_file = config.ARTIFACTS_DIR / "poison.html"
    artifact_file.parent.mkdir(parents=True, exist_ok=True)
    artifact_file.write_text(poison, encoding="utf-8")
    db.put_artifact("poison-artifact", "DEMO-FULL", 0, "evidently_html",
                    artifact_file.name, "text/html")

    with TestClient(app) as client:
        for path in ("/api/summary", "/api/registry", "/api/scenario/state",
                     "/api/export/demo-snapshots", "/api/artifacts/poison-artifact"):
            assert client.get(path).status_code == 404
        assert client.post("/api/live/tick-all").status_code == 404
        observations = client.get("/api/live/observations")
        assert observations.status_code == 200
        assert poison not in observations.text
        assert client.get("/api/live/artifacts/poison-artifact").status_code == 404
        version = client.get("/api/version").json()
        assert version["contract_version"] == "1.1"
        assert version["mode"] == "live"

    db.reset_engine()


def test_strict_live_readiness_rejects_insecure_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fallback.db")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", False)
    monkeypatch.setattr(config, "LIVE_CHURN_URL", "http://127.0.0.1:8083")
    monkeypatch.setattr(config, "LIVE_CHATBOT_URL", "http://127.0.0.1:8082")
    monkeypatch.setattr(config, "LIVE_NBA_URL", "http://127.0.0.1:8084")
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "")
    monkeypatch.setattr(config, "LIVE_TELEMETRY_TOKEN", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "")
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 5)
    db.reset_engine()

    with TestClient(app) as client:
        response = client.get("/api/readiness")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        errors = " ".join(body["configuration"]["errors"])
        assert "DATABASE_URL" in errors
        assert "LIVE_TELEMETRY_TOKEN" in errors
        assert "ANTHROPIC_API_KEY" in errors
        assert "external deployment URL" in errors
