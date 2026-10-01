"""HTTP telemetry client — the monitor's LIVE adapters PULL raw telemetry from the
external model apps (the models never push; the monitor reaches OUT to their URLs).

Uses httpx (already a backend dep). Kept tiny on purpose: this is the ONLY code that
crosses the process boundary to the model services — everything downstream operates on
plain dicts / DataFrames. Contract v1.0: [redacted] auth is sent when the monitor's
LIVE_TELEMETRY_TOKEN is set (the apps require it when THEIR RAI_TELEMETRY_TOKEN is set);
feature order/categoricals are OWNED by the model service (X-Feature-Order /
X-Categorical-Features headers, echoed in GET /telemetry/meta) — never hardcoded here.
"""
from __future__ import annotations

import hashlib
import io
import json

import httpx

from ..http_retry import request_with_retry


class TelemetryIntegrityError(RuntimeError):
    """Producer metadata does not match the exact public records on the wire."""


class WindowEvicted(RuntimeError):
    """The producer no longer serves this window (HTTP 404) — contract §6 'missing'."""


def canonical_records_sha256(records: list[dict]) -> str:
    """Digest the exact decoded primary-record list using the v1.1 canonical JSON form."""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _auth_headers() -> dict:
    """Bearer header when the monitor is configured with a telemetry token.

    config is imported lazily so this module stays import-cycle-free (config loads .env;
    adapters are imported from many places).
    """
    from .. import config
    token = getattr(config, "LIVE_TELEMETRY_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _send_get(method: str, url: str, **kwargs) -> httpx.Response:
    """One GET attempt through this module's ``httpx`` (the seam tests monkeypatch)."""
    return httpx.get(url, **kwargs)


def _send_post(method: str, url: str, **kwargs) -> httpx.Response:
    """One POST attempt through this module's ``httpx`` (the seam tests monkeypatch)."""
    return httpx.post(url, **kwargs)


def _get(url: str, **kwargs) -> httpx.Response:
    """GET with the contract §12 retry policy (429/502/503/504, connection errors)."""
    return request_with_retry("GET", url, send=_send_get, **kwargs)


def _post(url: str, **kwargs) -> httpx.Response:
    """POST with the same retry policy. Acknowledgements are idempotent on the producer
    side (keyed by window id); score write-back passes `attempts=1` because its
    idempotency is not promised by the contract."""
    return request_with_retry("POST", url, send=_send_post, **kwargs)


def _split_header(value: str | None) -> list[str] | None:
    """Comma-split a header into a list; missing/empty -> None (caller falls back)."""
    items = [s.strip() for s in (value or "").split(",") if s.strip()]
    return items or None


def pull(base_url: str, path: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    """GET a telemetry Window envelope: {contract_version, window, from_tick, to_tick,
    count, records, ...}."""
    r = _get(base_url.rstrip("/") + path, params=params, timeout=timeout,
             headers=_auth_headers())
    if r.status_code == 404:
        tick = params.get("tick") if params else None
        raise WindowEvicted(f"{path} tick={'?' if tick is None else tick} not available (404)")
    r.raise_for_status()
    return r.json()


def pull_meta(base_url: str, timeout: float = 10.0, *, strict: bool = False) -> dict:
    """GET /telemetry/meta — {contract_version, model_name, use_case_type, latest_tick,
    feature_order, categorical_features, ...}. Best-effort: {} on any failure (callers
    use it for cursor sync / feature ownership, never as a hard dependency)."""
    try:
        r = _get(base_url.rstrip("/") + "/telemetry/meta", timeout=timeout,
                 headers=_auth_headers())
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001 — callers choose advisory vs required semantics
        if strict:
            raise
        return {}


def pull_build_version(base_url: str, timeout: float = 5.0) -> dict:
    """Read the producer gateway's public build identity for deploy reconciliation."""
    r = _get(base_url.rstrip("/") + "/build/version", timeout=timeout,
             headers=_auth_headers())
    r.raise_for_status()
    return r.json()


def window_metadata(envelope: dict, tick: int) -> dict:
    """Extract the additive v1.1 immutable-window envelope.

    v1.0 producers remain readable: stable fallback IDs/digests are derived from the
    records without changing the existing endpoint wire shape.
    """
    records = envelope.get("records") or []
    nested = envelope.get("window") if isinstance(envelope.get("window"), dict) else {}

    def field(name: str, default=None):
        return envelope.get(name, nested.get(name, default))

    computed_digest = canonical_records_sha256(records)
    advertised_digest = field("content_sha256")
    if advertised_digest and str(advertised_digest) != computed_digest:
        raise TelemetryIntegrityError(
            f"window content digest mismatch: advertised {advertised_digest}, "
            f"computed {computed_digest}")
    digest = advertised_digest or computed_digest
    advertised_count = envelope.get("count", len(records))
    if int(advertised_count) != len(records):
        raise TelemetryIntegrityError(
            f"window record count mismatch: advertised {advertised_count}, "
            f"received {len(records)}")

    def record_id(record: dict) -> str | None:
        for key in ("inference_id", "rec_id", "trace_id", "event_id", "record_id", "id"):
            if record.get(key) is not None:
                return str(record[key])
        return None

    first_id = field("first_record_id") or (record_id(records[0]) if records else None)
    last_id = field("last_record_id") or (record_id(records[-1]) if records else None)
    window_id = field("window_id") or f"legacy-t{tick}-{str(digest)[:16]}"
    provenance = field("provenance_counts") or {}
    if not provenance:
        for record in records:
            value = str(record.get("provenance") or record.get("data_provenance") or "unknown")
            provenance[value] = provenance.get(value, 0) + 1
    return {
        "window_id": str(window_id),
        "source_instance_id": field("source_instance_id"),
        "opened_at": field("opened_at"),
        "closed_at": field("closed_at"),
        "content_sha256": str(digest),
        "first_record_id": first_id,
        "last_record_id": last_id,
        "model_version": field("model_version"),
        "provenance_counts": provenance,
        "record_count": len(records),
        "batch_id": field("batch_id") or (
            records[0].get("batch_id") if records else None),
    }


def pull_model(base_url: str, path: str = "/model/artifact", timeout: float = 60.0):
    """Load the joblib model artifact the app serves. Returns (model, version:int, meta)
    where meta = {"feature_order": [...] | None, "categorical": [...] | None} parsed from
    the X-Feature-Order / X-Categorical-Features headers (the model service OWNS its
    feature list — the monitor never hardcodes a per-use-case one).

    SECURITY: joblib.load is pickle-based (arbitrary code execution on hostile input).
    This is acceptable here because BOTH ends are first-party: the artifact is a fitted
    scikit-learn estimator served by OUR OWN model apps (ai-use-cases), and `base_url`
    is an operator-configured value (localhost in dev, our own deployment in prod) — not
    user-supplied and not an untrusted third party. Deserializing the real fitted model
    is required to run SHAP TreeExplainer / LIME on it (no JSON form works). Production
    hardening path if these ever leave a trusted network: switch both ends to `skops`
    (safe sklearn (de)serialization) or sign the artifact.
    """
    import joblib
    r = _get(base_url.rstrip("/") + path, timeout=timeout, headers=_auth_headers())
    r.raise_for_status()
    model = joblib.load(io.BytesIO(r.content))
    version = int(r.headers.get("X-Model-Version", "1"))
    meta = {"feature_order": _split_header(r.headers.get("X-Feature-Order")),
            "categorical": _split_header(r.headers.get("X-Categorical-Features"))}
    return model, version, meta


def push_scores(base_url: str, scores: list[dict], timeout: float = 10.0) -> dict:
    """POST judge scores back to the chatbot so they appear on its Langfuse traces.

    The monitor calls this after judging each trace window; the chatbot forwards the
    scores to Langfuse using the same trace_id it published in /telemetry/traces.

    scores: [{"trace_id": str, "name": str, "value": float, "comment"?: str}, ...]
    Returns the response JSON on success. Raises on HTTP/network error (caller degrades)."""
    # Single attempt: the contract does not promise that /telemetry/scores dedupes on
    # (trace_id, name), so a retried POST could double-score a trace. The write-back is
    # re-attempted on the next judged window anyway.
    r = _post(
        base_url.rstrip("/") + "/telemetry/scores",
        json={"scores": scores},
        headers={**_auth_headers(), "Content-Type": "application/json"},
        timeout=timeout,
        attempts=1,
    )
    r.raise_for_status()
    payload = r.json()
    accepted = payload.get("accepted")
    if accepted is None or int(accepted) != len(scores):
        raise RuntimeError(
            f"score write-back accepted {accepted!r} of {len(scores)} submitted scores")
    return payload


def acknowledge_observation(base_url: str, *, window_id: str, observation_id: str,
                            content_sha256: str, timeout: float = 10.0) -> dict:
    """Best-effort durable monitor acknowledgement to the producer portfolio gateway."""
    r = _post(
        base_url.rstrip("/") + "/api/sync/observed",
        json={"window_id": window_id, "observation_id": observation_id,
              "content_sha256": content_sha256},
        headers={**_auth_headers(), "Content-Type": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()
