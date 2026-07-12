"""Canonical v1.1 sync-state shaping for APIs and the background poller."""
from __future__ import annotations

from datetime import datetime, timezone
import time

from . import config, db


def iso_utc(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def source_sync(source_id: str) -> dict:
    row = db.get_live_source(source_id)
    now = time.time()
    state = row.get("state") or "connecting"
    last_checked = row.get("last_checked_at")
    last_success = row.get("last_success_at")
    backlog = max(0, int(row.get("backlog") or 0))

    if state == "error" and row.get("last_error"):
        state = "error"
    elif last_checked is not None and now - last_checked > config.LIVE_STALE_SECONDS:
        state = "stale"
    elif backlog > 0:
        state = "catching_up"
    elif row.get("observed_tick") is None:
        state = "idle" if row.get("source_tick") is not None else "connecting"
    elif last_success is not None and now - last_success > config.LIVE_IDLE_SECONDS:
        state = "idle"
    else:
        state = "at_tail"

    latest = db.get_latest_live_observation(source_id)
    payload = latest["payload"] if latest else {}
    source_tick = row.get("source_tick")
    observed_tick = row.get("observed_tick")
    out = {
        "source_id": source_id,
        "sync_state": state,
        "state": state,
        "source_tick": source_tick,
        "producer_tick": source_tick,  # compatibility alias for the dashboard
        "observed_tick": observed_tick,
        "backlog": backlog,
        "last_checked_at": iso_utc(last_checked),
        "last_success_at": iso_utc(last_success),
        "last_error_at": iso_utc(row.get("last_error_at")),
        "last_error": row.get("last_error"),
        "observation_id": latest.get("observation_id") if latest else None,
        "window_id": latest.get("window_id") if latest else None,
        "batch_id": latest.get("batch_id") if latest else None,
        "content_sha256": latest.get("content_sha256") if latest else None,
        "window_digest": latest.get("content_sha256") if latest else None,
        "source_instance_id": latest.get("source_instance_id") if latest else None,
        "model_version": payload.get("model_version") if latest else None,
        "provenance_counts": latest.get("provenance_counts", {}) if latest else {},
        "record_count": latest.get("record_count", 0) if latest else 0,
        "source_lag_ms": latest.get("source_lag_ms") if latest else None,
        "ack_status": latest.get("ack_status") if latest else None,
        "ack_error": latest.get("ack_error") if latest else None,
    }
    return out


def portfolio_sync(source_ids: list[str]) -> dict:
    sources = [source_sync(source_id) for source_id in source_ids]
    priority = {"error": 5, "stale": 4, "catching_up": 3,
                "connecting": 2, "idle": 1, "at_tail": 0}
    state = max((s["sync_state"] for s in sources),
                key=lambda value: priority.get(value, 99), default="connecting")
    return {
        "state": state,
        "sync_state": state,
        "backlog": sum(s["backlog"] for s in sources),
        "sources": sources,
    }
