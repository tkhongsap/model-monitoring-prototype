"""Retry with exponential backoff for the monitor's outbound HTTP (spec D.1, contract §12).

One policy for producer pulls, producer acknowledgements, score write-back and the alert
webhook: up to ``attempts`` tries; a retry on ``httpx.TransportError`` (connection,
read, write, pool errors) and on HTTP 429/502/503/504; exponential backoff
``base_delay * 2**n`` capped at ``max_delay`` with ±25 % jitter; an integer or float
``Retry-After`` header replaces the computed delay (still capped). Every other status is
returned at once — a 404 is the producer's "window evicted" answer and a 4xx is the
caller's bug, neither improves by waiting.

When the retryable statuses are exhausted the LAST RESPONSE is returned so callers keep
their own status handling (``raise_for_status``, the webhook's ``status_code >= 400``);
when transport errors are exhausted the last exception is raised.

The function never imports application config: ``sleep`` and ``rng`` are injectable so
tests run without waiting, and ``send`` / ``client`` decide how the request leaves the
process (``httpx.request`` by default).
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 4.0
JITTER = 0.25


def _retry_after_seconds(response) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:  # an HTTP-date Retry-After: fall back to the computed backoff
        return None
    return max(0.0, seconds)


def backoff_delay(attempt: int, *, base_delay: float, max_delay: float,
                  rng: Callable[[], float], retry_after: float | None = None) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based)."""
    if retry_after is not None:
        return min(max_delay, retry_after)
    delay = min(max_delay, base_delay * (2 ** attempt))
    return delay * (1.0 + JITTER * (2.0 * rng() - 1.0))


def request_with_retry(method: str, url: str, *, attempts: int = DEFAULT_ATTEMPTS,
                       base_delay: float = DEFAULT_BASE_DELAY,
                       max_delay: float = DEFAULT_MAX_DELAY,
                       sleep: Callable[[float], None] | None = None,
                       rng: Callable[[], float] | None = None,
                       send: Callable[..., httpx.Response] | None = None,
                       client: httpx.Client | None = None,
                       **kwargs) -> httpx.Response:
    """Send ``method url`` with the retry policy described in the module docstring.

    ``send(method, url, **kwargs)`` performs one attempt; it defaults to
    ``client.request`` when a client is given, else ``httpx.request``. Adapters pass a
    ``send`` that calls ``httpx.get`` / ``httpx.post`` so their module-level ``httpx``
    stays the seam tests monkeypatch.
    """
    if send is None:
        send = client.request if client is not None else httpx.request
    # resolved per call so a test can patch ``http_retry.time.sleep`` / ``random.random``
    sleep = time.sleep if sleep is None else sleep
    rng = random.random if rng is None else rng
    attempts = max(1, int(attempts))
    last_error: Exception | None = None
    response = None
    for attempt in range(attempts):
        retry_after = None
        try:
            response = send(method, url, **kwargs)
            last_error = None
        except httpx.TransportError as exc:
            last_error = exc
            response = None
        if response is not None:
            status = getattr(response, "status_code", None)
            if status not in RETRY_STATUSES:
                return response
            retry_after = _retry_after_seconds(response)
        if attempt == attempts - 1:
            break
        delay = backoff_delay(attempt, base_delay=base_delay, max_delay=max_delay, rng=rng,
                              retry_after=retry_after)
        reason = (f"HTTP {response.status_code}" if response is not None
                  else type(last_error).__name__)
        log.info("retrying %s %s after %s (attempt %d of %d, sleeping %.2fs)",
                 method, _redact(url), reason, attempt + 1, attempts, delay)
        sleep(delay)
    if response is not None:
        return response
    assert last_error is not None
    raise last_error


def _redact(url: str) -> str:
    """Log scheme and host only: a webhook URL's path is the credential."""
    try:
        parsed = httpx.URL(url)
        return f"{parsed.scheme}://{parsed.host}"
    except Exception:  # noqa: BLE001 — never let logging raise
        return "<url>"
