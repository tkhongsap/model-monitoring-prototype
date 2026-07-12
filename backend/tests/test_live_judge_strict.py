from __future__ import annotations

import pytest

from app import config, db
from app.adapters.base import TickContext
from app.adapters.llm_eval import live_http
from app.adapters import telemetry_http


@pytest.fixture()
def isolated_judge_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "judge.db")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    db.reset_engine()
    yield
    db.reset_engine()


def _trace_envelope() -> dict:
    records = [{
        "trace_id": f"trace-{index}", "question": "What is my plan?",
        "answer": "Your plan is Gold.",
        "retrieval_context": [{"title": "plan", "text": "Gold plan"}],
        "tool_calls": [], "latency_s": 0.5, "refused": False,
    } for index in range(8)]
    return {
        "contract_version": "1.1", "window_id": "chat-window-0",
        "source_instance_id": "chat-a", "opened_at": "2026-07-12T00:00:00Z",
        "closed_at": "2026-07-12T00:05:00Z",
        "content_sha256": telemetry_http.canonical_records_sha256(records),
        "first_record_id": "trace-0", "last_record_id": "trace-7",
        "model_version": "claude-live", "provenance_counts": {"user_input": 8},
        "count": len(records), "records": records,
    }


def test_live_mode_never_uses_heuristic_without_real_judge(
        isolated_judge_db, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(live_http, "pull", lambda *args, **kwargs: _trace_envelope())
    adapter = live_http.LiveHttpLLMAdapter("https://producer", "LIVE", "AICT-L02")
    result = adapter.evaluate("AICT-L02", TickContext(tick=0, seed=42))
    assert result.metadata["window_id"] == "chat-window-0"
    assert result.errors["llm_eval"].endswith(
        "real Anthropic judge is required when CONTROL_TOWER_MODE=live")
    assert result.signals and all(value is None for value in result.signals.values())


def test_live_writeback_failure_makes_scores_unknown(
        isolated_judge_db, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "configured")
    monkeypatch.setattr(live_http, "pull", lambda *args, **kwargs: _trace_envelope())
    monkeypatch.setattr(live_http, "_judge_claude", lambda traces, *args, **kwargs: [{
        "groundedness": 0.95, "relevance": 0.95, "hallucination": False, "pii": False}
        for _ in traces])

    def fail_writeback(*args, **kwargs):
        raise RuntimeError("Langfuse write-back unavailable")

    monkeypatch.setattr(telemetry_http, "push_scores", fail_writeback)
    adapter = live_http.LiveHttpLLMAdapter("https://producer", "LIVE", "AICT-L02")
    result = adapter.evaluate("AICT-L02", TickContext(tick=0, seed=42))
    assert "Langfuse write-back unavailable" in result.errors["score_writeback"]
    assert result.signals and all(value is None for value in result.signals.values())


def test_undersized_closed_chat_window_is_unknown_without_judging(
        isolated_judge_db, monkeypatch):
    envelope = _trace_envelope()
    envelope["records"] = envelope["records"][:1]
    envelope["count"] = 1
    envelope["content_sha256"] = telemetry_http.canonical_records_sha256(envelope["records"])
    monkeypatch.setattr(live_http, "pull", lambda *args, **kwargs: envelope)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "configured")
    monkeypatch.setattr(live_http, "_judge_claude",
                        lambda *args, **kwargs: pytest.fail("judge must not run"))
    adapter = live_http.LiveHttpLLMAdapter("https://producer", "LIVE", "AICT-L02")
    result = adapter.evaluate("AICT-L02", TickContext(tick=0, seed=42))
    assert result.errors["insufficient_sample"] == "requires 8 traces; observed 1"
    assert all(value is None for value in result.signals.values())
