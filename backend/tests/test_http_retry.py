"""HTTP retry with backoff for producer pulls, acks and the alert webhook (spec D.1)."""
from __future__ import annotations

import httpx
import pytest

from app import alert_delivery, http_retry
from app.adapters import telemetry_http


class Script:
    """A MockTransport handler that answers one scripted outcome per request."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            status, headers = outcome
            return httpx.Response(status, headers=headers, json={"ok": status})
        return httpx.Response(outcome, json={"ok": outcome})


def _run(script: Script, **kwargs) -> tuple[httpx.Response, list[float]]:
    sleeps: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(script)) as client:
        response = http_retry.request_with_retry(
            "GET", "https://producer.example/telemetry/meta", client=client,
            sleep=sleeps.append, rng=lambda: 0.5, **kwargs)
    return response, sleeps


def test_429_honours_retry_after():
    script = Script((429, {"Retry-After": "1"}), 200)
    response, sleeps = _run(script)
    assert response.status_code == 200
    assert len(script.requests) == 2
    assert sleeps == [1.0]


def test_retry_after_is_capped_at_max_delay():
    script = Script((503, {"Retry-After": "120"}), 200)
    _, sleeps = _run(script)
    assert sleeps == [4.0]


def test_503_twice_then_200_backs_off_exponentially():
    script = Script(503, 503, 200)
    response, sleeps = _run(script)
    assert response.status_code == 200
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]          # rng=0.5 is the jitter midpoint: no offset


def test_jitter_stays_within_a_quarter_of_the_base_delay():
    script = Script(503, 200)
    sleeps: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(script)) as client:
        http_retry.request_with_retry("GET", "https://producer.example/x", client=client,
                                      sleep=sleeps.append, rng=lambda: 1.0)
    assert sleeps == [pytest.approx(0.625)]


def test_connection_error_is_retried_then_raised():
    script = Script(httpx.ConnectError("refused"), httpx.ConnectError("refused"),
                    httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        _run(script)
    assert len(script.requests) == 3


def test_404_is_not_retried():
    script = Script(404, 200)
    response, sleeps = _run(script)
    assert response.status_code == 404
    assert len(script.requests) == 1 and sleeps == []


def test_retryable_status_exhausted_returns_last_response():
    script = Script(502, 502, 502)
    response, sleeps = _run(script)
    assert response.status_code == 502
    assert len(script.requests) == 3 and len(sleeps) == 2


def test_telemetry_pull_retries_through_httpx_get(monkeypatch):
    """`pull` goes through the retry policy while `telemetry_http.httpx.get` stays the
    seam the existing tests monkeypatch."""
    calls: list[str] = []
    outcomes = [503, 200]

    def fake_get(url, **kwargs):
        calls.append(url)
        status = outcomes.pop(0)
        return httpx.Response(status, json={"contract_version": "1.1", "records": []},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(telemetry_http.httpx, "get", fake_get)
    monkeypatch.setattr(http_retry.time, "sleep", lambda s: None)
    envelope = telemetry_http.pull("https://producer.example", "/telemetry/inferences",
                                   {"tick": 3})
    assert envelope["contract_version"] == "1.1"
    assert calls == ["https://producer.example/telemetry/inferences"] * 2


def test_telemetry_pull_404_is_evicted_without_retry(monkeypatch):
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(telemetry_http.httpx, "get", fake_get)
    with pytest.raises(telemetry_http.WindowEvicted):
        telemetry_http.pull("https://producer.example", "/telemetry/inferences", {"tick": 3})
    assert len(calls) == 1


def test_acknowledge_retries_through_httpx_post(monkeypatch):
    outcomes = [httpx.ConnectError("refused"), 200]
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(telemetry_http.httpx, "post", fake_post)
    monkeypatch.setattr(http_retry.time, "sleep", lambda s: None)
    body = telemetry_http.acknowledge_observation(
        "https://producer.example", window_id="w1", observation_id="o1",
        content_sha256="a" * 64)
    assert body == {"ok": True} and len(calls) == 2
    assert calls[0]["json"]["window_id"] == "w1"


def test_webhook_default_post_retries_then_returns_last_response(monkeypatch):
    outcomes = [503, 503, 503]
    calls: list[str] = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return httpx.Response(outcomes.pop(0), request=httpx.Request("POST", url))

    monkeypatch.setattr(alert_delivery.httpx, "post", fake_post)
    sleeps: list[float] = []
    monkeypatch.setattr(http_retry.time, "sleep", sleeps.append)
    response = alert_delivery._default_post("https://hooks.example.test/s", json={"a": 1},
                                            timeout=5)
    assert response.status_code == 503
    assert len(calls) == 3 and len(sleeps) == 2
