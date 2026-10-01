"""Contract strictness (spec E.2-E.5): contract_version validation on every pulled
window, no silent latency_s default, https-only producers in strict live mode, and the
judge-model docstring matching the configured default."""
from __future__ import annotations

import httpx
import pytest

from app import config, db
from app.adapters import telemetry_http
from app.adapters.base import TickContext
from app.adapters.llm_eval import live_http


def _fake_get(envelope: dict):
    def fake_get(url, **kwargs):
        return httpx.Response(200, json=envelope, request=httpx.Request("GET", url))
    return fake_get


# --- contract_version on pulled windows -------------------------------------------

@pytest.mark.parametrize("version", ["0.9", "2.0", "1"])
def test_pull_rejects_unsupported_contract_version(monkeypatch, version):
    monkeypatch.setattr(telemetry_http.httpx, "get",
                        _fake_get({"contract_version": version, "records": [], "count": 0}))
    with pytest.raises(telemetry_http.ContractVersionError, match="contract"):
        telemetry_http.pull("https://producer.example", "/telemetry/inferences", {"tick": 3})


def test_pull_rejects_missing_contract_version(monkeypatch):
    monkeypatch.setattr(telemetry_http.httpx, "get",
                        _fake_get({"records": [], "count": 0}))
    with pytest.raises(telemetry_http.ContractVersionError, match="None"):
        telemetry_http.pull("https://producer.example", "/telemetry/inferences", {"tick": 3})


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_pull_accepts_supported_contract_versions(monkeypatch, version):
    monkeypatch.setattr(telemetry_http.httpx, "get",
                        _fake_get({"contract_version": version, "records": [], "count": 0}))
    env = telemetry_http.pull("https://producer.example", "/telemetry/inferences", {"tick": 3})
    assert env["contract_version"] == version


def test_unsupported_contract_version_holds_the_cursor(isolated_db, fake_producer, monkeypatch):
    """An unsupported contract_version on the inference window surfaces as a telemetry
    error (prefixed `contract:`) and the source cursor is held — never advanced on a
    window the monitor cannot interpret."""
    from app.adapters.ml_monitor import live_http as ml_live_http
    from app.scenario import live_runner as live_runner_module

    real_pull = ml_live_http.pull

    def pull_with_bad_version(base_url, path, params=None, timeout=30.0):
        if path == "/telemetry/inferences":
            raise telemetry_http.ContractVersionError(
                "contract: unsupported contract_version '0.9' from /telemetry/inferences")
        return real_pull(base_url, path, params, timeout)

    monkeypatch.setattr(ml_live_http, "pull", pull_with_bad_version)
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    out = runner.tick()
    assert out.get("cursor_held") is True
    assert "contract" in out["errors"]["telemetry"]
    assert db.get_live_source("AICT-L01")["next_tick"] == 0
    assert db.get_live_source("AICT-L01")["state"] == "error"


def test_pull_meta_rejects_unsupported_contract_version_when_present(monkeypatch):
    monkeypatch.setattr(telemetry_http.httpx, "get",
                        _fake_get({"contract_version": "0.9", "latest_tick": 4}))
    with pytest.raises(telemetry_http.ContractVersionError):
        telemetry_http.pull_meta("https://producer.example", strict=True)
    assert telemetry_http.pull_meta("https://producer.example") == {}


def test_pull_meta_without_the_field_is_still_advisory(monkeypatch):
    monkeypatch.setattr(telemetry_http.httpx, "get", _fake_get({"latest_tick": 4}))
    assert telemetry_http.pull_meta("https://producer.example", strict=True) == {
        "latest_tick": 4}


# --- latency_s is never defaulted -----------------------------------------------

@pytest.fixture()
def judge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "judge.db")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "configured")
    db.reset_engine()
    yield
    db.reset_engine()


def _traces_with_missing_latency() -> dict:
    records = []
    for index in range(10):
        record = {
            "trace_id": f"trace-{index}", "question": "What is my plan?",
            "answer": "Your plan is Gold.",
            "retrieval_context": [{"title": "plan", "text": "Gold plan"}],
            "tool_calls": [], "refused": False,
        }
        if index % 2 == 0:            # five traces carry a latency, five do not
            record["latency_s"] = 2.0 + index
        records.append(record)
    return {
        "contract_version": "1.1", "window_id": "chat-window-9", "count": len(records),
        "content_sha256": telemetry_http.canonical_records_sha256(records),
        "records": records,
    }


def test_missing_latency_is_excluded_and_counted(judge_db, monkeypatch):
    monkeypatch.setattr(live_http, "pull", lambda *a, **k: _traces_with_missing_latency())
    monkeypatch.setattr(live_http, "_judge_claude", lambda traces, *a, **k: [{
        "groundedness": 0.9, "relevance": 0.9, "hallucination": False, "pii": False}
        for _ in traces])
    monkeypatch.setattr(telemetry_http, "push_scores", lambda *a, **k: {"accepted": 0})
    adapter = live_http.LiveHttpLLMAdapter("https://producer", "LIVE", "AICT-L02")
    result = adapter.evaluate("AICT-L02", TickContext(tick=0, seed=42))

    assert "llm_eval" not in result.errors
    # p95 of {2, 4, 6, 8, 10}; a 0.0 default would have dragged it to 7.0 or lower
    assert result.signals["p95_latency_s"] == pytest.approx(9.6)
    assert result.metadata["latency_missing"] == 5
    by_id = {r["trace_id"]: r for r in result.records}
    assert by_id["trace-1"]["latency_s"] is None
    assert by_id["trace-2"]["latency_s"] == 4.0


def test_all_latency_missing_leaves_p95_unknown(judge_db, monkeypatch):
    env = _traces_with_missing_latency()
    for record in env["records"]:
        record.pop("latency_s", None)
    env["content_sha256"] = telemetry_http.canonical_records_sha256(env["records"])
    monkeypatch.setattr(live_http, "pull", lambda *a, **k: env)
    monkeypatch.setattr(live_http, "_judge_claude", lambda traces, *a, **k: [{
        "groundedness": 0.9, "relevance": 0.9, "hallucination": False, "pii": False}
        for _ in traces])
    monkeypatch.setattr(telemetry_http, "push_scores", lambda *a, **k: {"accepted": 0})
    adapter = live_http.LiveHttpLLMAdapter("https://producer", "LIVE", "AICT-L02")
    result = adapter.evaluate("AICT-L02", TickContext(tick=0, seed=42))
    assert result.signals["p95_latency_s"] is None
    assert result.signals["groundedness"] == pytest.approx(0.9)
    assert result.metadata["latency_missing"] == 10


def test_judge_docstring_names_the_configured_default_model():
    assert "claude-haiku-4-5" in live_http.__doc__
    assert "claude-opus-4-8" not in live_http.__doc__


# --- https-only producers in strict live mode ------------------------------------

def _strict_config(monkeypatch, scheme: str) -> None:
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", False)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://monitor@db.example/monitor")
    monkeypatch.setattr(config, "LIVE_CHURN_URL", f"{scheme}://producer.example/churn")
    monkeypatch.setattr(config, "LIVE_CHATBOT_URL", f"{scheme}://producer.example/chatbot")
    monkeypatch.setattr(config, "LIVE_NBA_URL", f"{scheme}://producer.example/nba")
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", f"{scheme}://producer.example")
    monkeypatch.setattr(config, "LIVE_TELEMETRY_TOKEN", "t")
    monkeypatch.setattr(config, "LIVE_WORKER_TOKEN", "w")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(config, "LIVE_POLL_SECONDS", 5)


def test_http_producer_urls_are_configuration_errors(monkeypatch):
    _strict_config(monkeypatch, "http")
    errors = config.live_configuration_errors()
    for name in ("LIVE_CHURN_URL", "LIVE_CHATBOT_URL", "LIVE_NBA_URL", "LIVE_PRODUCER_URL"):
        assert f"{name} must use https in strict live mode" in errors
    assert len(errors) == 4


def test_https_producer_urls_pass(monkeypatch):
    _strict_config(monkeypatch, "https")
    assert config.live_configuration_errors() == []


def test_insecure_testing_switch_still_bypasses_https(monkeypatch):
    _strict_config(monkeypatch, "http")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", True)
    assert config.live_configuration_errors() == []
