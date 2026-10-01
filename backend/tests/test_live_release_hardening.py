from __future__ import annotations

import threading

import pytest

from app import config, db
from app.adapters.base import TickContext
from app.adapters.ml_monitor.live_http import LiveHttpMLAdapter
from app.adapters import telemetry_http
from app.api.live_routes import _public_payload, artifact as public_artifact
from app.scenario.baker import _artifact_writer_factory
from app.scenario import live_runner


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hardening.db")
    monkeypatch.setattr(config, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", True)
    db.reset_engine()
    yield tmp_path
    db.reset_engine()


def test_canonical_digest_is_verified_against_exact_records():
    records = [{"record_id": "a", "value": 1}, {"record_id": "b", "value": 2}]
    digest = telemetry_http.canonical_records_sha256(records)
    meta = telemetry_http.window_metadata({
        "records": records, "count": 2, "content_sha256": digest,
        "window_id": "window-a",
    }, 0)
    assert meta["content_sha256"] == digest
    with pytest.raises(telemetry_http.TelemetryIntegrityError, match="digest mismatch"):
        telemetry_http.window_metadata({
            "records": records, "count": 2, "content_sha256": "0" * 64,
            "window_id": "window-a",
        }, 0)
    with pytest.raises(telemetry_http.TelemetryIntegrityError, match="record count mismatch"):
        telemetry_http.window_metadata({"records": records, "count": 3}, 0)


def test_score_writeback_rejects_partial_acceptance(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"received": 2, "accepted": 1}

    monkeypatch.setattr(telemetry_http.httpx, "post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="accepted 1 of 2"):
        telemetry_http.push_scores("https://producer", [
            {"trace_id": "a", "name": "x", "value": 1},
            {"trace_id": "b", "name": "x", "value": 1},
        ])


def test_live_artifact_blob_survives_restart_and_lime_is_not_public(isolated_db):
    writer = _artifact_writer_factory("LIVE-AICT-L01")
    shap_id = writer("shap_png", 0, b"safe-global-shap", "png")
    lime_id = writer("lime_html", 0, "raw value: tenure=17", "html")
    (config.ARTIFACTS_DIR / "live-aict-l01-t00-shap_png.png").unlink()
    db.reset_engine()
    response = public_artifact(shap_id)
    assert response.body == b"safe-global-shap"
    assert response.headers["x-content-sha256"]
    with pytest.raises(Exception) as exc:
        public_artifact(lime_id)
    assert getattr(exc.value, "status_code", None) == 404


def test_public_payload_redacts_raw_judge_and_instance_explanation():
    payload = _public_payload({
        "judge_sample": [{"trace_id": "t-1", "question": "secret question",
                          "answer": "secret answer", "groundedness": 0.9}],
        "lime_top": [["tenure <= 17", 0.5]],
        "lime_instance": {"index": 4},
        "artifacts": {"lime_html": "unsafe", "shap_png": "safe"},
    })
    assert "secret" not in str(payload)
    assert "lime_top" not in payload and "lime_instance" not in payload
    assert payload["artifacts"] == {"shap_png": "safe"}


def test_ml_window_below_500_records_is_unknown_not_telemetry_failure(monkeypatch):
    records = [{"inference_id": "i-1", "features": {"x": 1.0},
                "churn_proba": 0.4}]
    envelope = {
        "records": records, "count": 1, "window_id": "w-1",
        "content_sha256": telemetry_http.canonical_records_sha256(records),
    }
    adapter = LiveHttpMLAdapter("https://producer", lambda *args: "artifact", chunk_size=500)
    monkeypatch.setattr(adapter, "_ensure_baseline", lambda: None)
    monkeypatch.setattr("app.adapters.ml_monitor.live_http.pull",
                        lambda *args, **kwargs: envelope)
    result = adapter.monitor("AICT-L01", TickContext(tick=0, seed=42))
    assert "telemetry" not in result.errors
    assert result.errors["insufficient_sample"] == "requires 500 records; observed 1"
    assert all(value is None for value in result.signals.values())


def test_failed_ack_is_retried_after_cursor_advance(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "https://producer")
    payload = {
        "tick": 0, "window_id": "w-ack", "content_sha256": "a" * 64,
        "record_count": 500, "signals": {}, "overall": "Unknown",
    }
    observation_id, _ = db.put_live_observation(
        "AICT-L01", payload, source_tick=0, next_tick=1, backlog=0, state="at_tail")
    calls = []
    monkeypatch.setattr(live_runner, "acknowledge_observation",
                        lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})
    result = live_runner.retry_pending_acknowledgements()
    assert result == {"attempted": 1, "acknowledged": 1, "failed": 0}
    assert calls[0]["window_id"] == "w-ack"
    row = db.get_latest_live_observation("AICT-L01")
    assert row["ack_status"] == "acknowledged"
    assert db.get_live_source("AICT-L01")["next_tick"] == 1


def test_versioned_migrations_are_idempotent_under_thread_contention(isolated_db):
    db.reset_engine()
    errors: list[Exception] = []

    def open_database():
        try:
            db.engine()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=open_database) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    bind = db.engine()
    assert db.migrate_engine(bind) == []
    with bind.begin() as cx:
        versions = list(cx.execute(db.select(db.schema_migrations.c.version)).scalars())
    assert versions == [1, 2, 3, 4, 5, 6, 7]
