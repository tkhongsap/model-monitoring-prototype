"""Strict production router: persisted, redacted live evidence only.

The demo/scenario router is never registered when CONTROL_TOWER_MODE=live, so baked
controls are absent from the production OpenAPI/routing table as well as middleware-
isolated.  One authenticated worker endpoint gives an external Autoscale wake request
an actual lease-protected poll-cycle completion signal.
"""
from __future__ import annotations

import hmac

import anyio
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import config, db
from ..live_poller import poller
from ..live_sync import portfolio_sync, source_sync
from . import live_portfolio

router = APIRouter(prefix="/api")


def _worker_token_matches(supplied: str, expected: str) -> bool:
    """Compare bearer tokens without letting malformed header text raise a 500.

    HTTP bearer credentials are deliberately restricted to ASCII by the strict-live
    configuration checks.  ``hmac.compare_digest`` raises ``TypeError`` when either
    string contains non-ASCII text, so encode only validated wire-safe values and fail
    closed for malformed input.
    """
    try:
        supplied_bytes = supplied.encode("ascii")
        expected_bytes = expected.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(supplied_bytes, expected_bytes)


def _require_worker_token(authorization: str | None) -> None:
    """401 unless the request carries the configured worker bearer token."""
    expected = config.LIVE_WORKER_TOKEN
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not expected or not supplied or not _worker_token_matches(supplied, expected):
        raise HTTPException(401, detail="invalid worker token")


def _public_payload(payload: dict) -> dict:
    """Defense-in-depth redaction for public observation/detail responses."""
    out = dict(payload)
    out.pop("lime_top", None)
    out.pop("lime_instance", None)
    artifacts = dict(out.get("artifacts") or {})
    artifacts.pop("lime_html", None)
    out["artifacts"] = artifacts
    samples = []
    for sample in out.get("judge_sample") or []:
        samples.append({key: sample.get(key) for key in (
            "trace_id", "groundedness", "relevance", "hallucination", "pii",
            "latency_s", "judge") if key in sample})
    if "judge_sample" in out:
        out["judge_sample"] = samples
    return out


@router.get("/live/portfolio")
def portfolio():
    return {**live_portfolio.portfolio_summary(), "rows": live_portfolio.portfolio_rows()}


@router.get("/live/use-case/{uc}")
def use_case(uc: str):
    detail = live_portfolio.detail(uc)
    if detail is None:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    return _public_payload(detail)


@router.get("/live/sync")
def sync_all():
    return portfolio_sync(live_portfolio.LIVE_UCS)


@router.get("/live/sync/{uc}")
def sync_one(uc: str):
    if uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    return source_sync(uc)


@router.get("/live/observations")
def observations(uc: str | None = None, limit: int = 100):
    if uc is not None and uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    rows = db.list_live_observations(uc, limit)
    for row in rows:
        row["payload"] = _public_payload(row.get("payload") or {})
    return {"contract_version": "1.1", "redacted": True, "rows": rows}


@router.get("/live/alerts")
def alerts(uc: str | None = None, open: bool = False, limit: int = 50):
    """Health-transition alerts (spec C.2): derived columns only, newest first.

    Read-only, like every browser-facing live route; the worker owns opening and
    resolving. The webhook URL is never part of a row.
    """
    if uc is not None and uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    rows = db.list_alerts(uc, open_only=open, limit=max(1, min(limit, 500)))
    return {"contract_version": "1.1", "redacted": True, "rows": rows}


@router.get("/live/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    meta = db.get_artifact(artifact_id)
    if (not meta or not str(meta.get("scenario_id", "")).startswith("LIVE-")
            or meta.get("kind") == "lime_html"):
        raise HTTPException(404, detail="unknown live artifact id")
    blob = db.get_live_artifact_blob(artifact_id)
    if not blob:
        raise HTTPException(404, detail="live artifact not persisted")
    return Response(
        content=blob["content"], media_type=blob["content_type"],
        headers={"Cache-Control": "private, no-store",
                 "X-Content-SHA256": blob["content_sha256"]})


@router.post("/live/poll")
async def run_poll_cycle(authorization: str | None = Header(default=None)):
    _require_worker_token(authorization)
    completed = await anyio.to_thread.run_sync(poller().run_once)
    return {
        "completed": completed,
        "lease": "acquired" if completed else "held_by_another_worker",
        "sync": portfolio_sync(live_portfolio.LIVE_UCS),
    }


# --- operator unstick (spec D.3): worker token, mutating, audited ---------------------

class SkipRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/live/sources/{uc}/skip")
def skip_source_tick(uc: str, body: SkipRequest,
                     authorization: str | None = Header(default=None)):
    """Skip the tick a source's cursor is held on (state `error`), with a reason.

    In one transaction: writes a stub observation (`record_count=0`, `skipped=true`,
    never acknowledged to the producer) so the window stays auditable, marks its realized
    rows final so the backfill never re-pulls the poisoned window and advances the
    cursor; then drops the in-memory runner so it reloads the cursor. 409 when the
    cursor is not held on an error.
    """
    _require_worker_token(authorization)
    if uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(422, detail="reason must not be blank")
    from ..scenario import live_runner as live_runner_module
    try:
        # one transaction: stub observation, cursor advance and final realized rows
        result = db.skip_live_tick(uc, reason,
                                   realized_keys=live_runner_module.realized_keys_for(uc))
    except db.NothingToSkip as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    live_runner_module.reset_live_runner(uc)
    return {**result, "sync": source_sync(uc)}


@router.post("/live/sources/{uc}/reset-ack")
def reset_source_acks(uc: str, authorization: str | None = Header(default=None)):
    """Abandon a source's pending/errored producer acknowledgements so a poisoned ack is
    no longer retried every cycle. Observations and the cursor are untouched."""
    _require_worker_token(authorization)
    if uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    return {"use_case_id": uc, "abandoned": db.abandon_live_acks(uc)}
