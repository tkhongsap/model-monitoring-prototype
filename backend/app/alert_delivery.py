"""Webhook delivery for live alerts (spec C.3).

Every opened and every resolved alert is POSTed once to ``LIVE_ALERT_WEBHOOK_URL`` as a
JSON body ``{"text", "blocks", "alert"}`` — Slack incoming-webhook compatible and plain
enough for any generic receiver.  Delivery is at-least-once: a failed attempt is recorded
on the alert row (``*_delivery_status = "error"``) and retried on the next poll cycle;
until slice D lands the retry policy is one attempt per cycle.  Delivery never blocks
grading: ``deliver_pending`` swallows every error and the poller calls it last.

Redaction: the payload carries use case id, lane, health, tick and the dashboard URL —
no features, no trace text (spec Data contract).  Logs name the webhook *host* only,
never the URL (its path is the secret) and never the body.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from . import config, db

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
NOT_CONFIGURED = "LIVE_ALERT_WEBHOOK_URL not configured"

_PHASES = (("open", "open_delivery_status"), ("resolve", "resolve_delivery_status"))


def _default_post(url: str, *, json: dict, timeout: float):
    return httpx.post(url, json=json, timeout=timeout)


def _dashboard_url(source_id: str) -> str | None:
    base = config.LIVE_DASHBOARD_URL
    return f"{base}/use-case/{source_id}" if base else None


def build_payload(alert: dict, phase: str) -> dict:
    """Slack-compatible notification for one alert phase; derived fields only."""
    source_id = str(alert["source_id"])
    lane = str(alert["lane"])
    if phase == "resolve":
        tick = alert.get("resolved_tick")
        text = (f"[RESOLVED] {source_id} {lane}: {alert['to_health']} → Green "
                f"at tick {tick if tick is not None else '—'}")
    else:
        tick = alert.get("tick")
        before = alert.get("from_health") or "—"
        text = (f"[{str(alert['to_health']).upper()}] {source_id} {lane}: "
                f"{before} → {alert['to_health']} at tick {tick if tick is not None else '—'}")
    dashboard = _dashboard_url(source_id)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if dashboard:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{dashboard}|Open the use case in the monitor>"}]})
    return {
        "text": text,
        "blocks": blocks,
        "alert": {
            "alert_id": alert["alert_id"],
            "use_case_id": source_id,
            "lane": lane,
            "from_health": alert.get("from_health"),
            "to_health": alert["to_health"],
            "tick": alert.get("tick"),
            "observation_id": alert.get("observation_id"),
            "phase": phase,
            "opened_at": alert.get("opened_at"),
            "resolved_at": alert.get("resolved_at"),
            "resolved_tick": alert.get("resolved_tick"),
            "dashboard_url": dashboard,
        },
    }


def deliver_pending(post=None, limit: int = 100) -> int:
    """POST every pending/errored open or resolve notification; return the delivered count.

    `post(url, json=..., timeout=...)` is injectable for tests; the default is httpx.
    """
    post = post or _default_post
    rows = db.alerts_pending_delivery(limit)
    if not rows:
        return 0
    url = config.LIVE_ALERT_WEBHOOK_URL
    if not url:
        db.mark_alerts_delivery_skipped([r["alert_id"] for r in rows], NOT_CONFIGURED)
        return 0
    host = urlsplit(url).hostname or "?"
    delivered = 0
    for row in rows:
        for phase, status_column in _PHASES:
            if row.get(status_column) not in db.ALERT_DELIVERY_RETRY_STATUSES:
                continue
            try:
                response = post(url, json=build_payload(row, phase), timeout=TIMEOUT_SECONDS)
                status_code = int(getattr(response, "status_code", 0))
                if status_code >= 400 or status_code == 0:
                    raise _WebhookStatus(status_code)
            except _WebhookStatus as exc:
                db.mark_alert_delivery(row["alert_id"], phase, ok=False, error=str(exc))
                log.warning("alert webhook %s: %s delivery to host %s failed: %s",
                            row["alert_id"], phase, host, exc)
                continue
            except Exception as exc:  # noqa: BLE001 — recorded, retried next cycle
                # the stored error reaches the public alerts API: never let an exception
                # message carry the webhook URL (the secret) into it
                error = f"{type(exc).__name__}: {exc}".replace(url, "<webhook>")
                db.mark_alert_delivery(row["alert_id"], phase, ok=False, error=error)
                log.warning("alert webhook %s: %s delivery to host %s failed: %s",
                            row["alert_id"], phase, host, type(exc).__name__)
                continue
            db.mark_alert_delivery(row["alert_id"], phase, ok=True, error=None)
            delivered += 1
            log.info("alert webhook %s: %s delivered to host %s", row["alert_id"], phase, host)
    return delivered


class _WebhookStatus(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
