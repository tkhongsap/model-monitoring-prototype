"""REST API — the one and only contract (PRD Appendix E §E.3).

All registry field names are exactly Appendix B; health values Green|Amber|Red|Unknown.
The browser calls /api/* same-origin (Next.js rewrites proxy to :8000).
"""
from __future__ import annotations

import asyncio
import json

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .. import config, db
from ..engines.health import LANES, SIGNAL_SPECS
from ..scenario import baker
from ..scenario.player import player

router = APIRouter(prefix="/api")

LANE_FIELD = {"Quality": "quality_status", "Safety & security": "safety_status",
              "Reliability": "reliability_status", "Drift & degradation": "degradation_status",
              "Feedback & action loop": "feedback_status"}

APPROVED = {"privacy_status": "Approved", "security_status": "Approved", "rai_status": "Complete"}


def _registry_now() -> list[dict]:
    """Registry rows with the current tick's deep-row overrides applied."""
    rows = db.get_registry_rows()
    p = player().payload()
    overrides = (p or {}).get("registry_overrides", {})
    out = []
    for r in rows:
        o = overrides.get(r["registry_id"])
        out.append({**r, **o} if o else r)
    return out


def _status_counts(rows: list[dict]) -> dict:
    c = {"production": 0, "in_development": 0, "requirements_not_started": 0,
         "paused": 0, "retired_cancelled": 0}
    for r in rows:
        s = r.get("status")
        if s == "production":
            c["production"] += 1
        elif s in ("PoV", "UAT"):
            c["in_development"] += 1
        elif s == "requirements":
            c["requirements_not_started"] += 1
        elif s == "paused":
            c["paused"] += 1
        elif s == "retired":
            c["retired_cancelled"] += 1
    return c


def _registry_num(row: dict) -> int:
    return int(row["registry_id"].split("P")[-1])


def _monitoring_readiness(row: dict) -> str:
    n = _registry_num(row)
    if n <= 2:
        return "deep_simulated"
    if n <= 15:
        return "pilot_register_only"
    return "not_instrumented"


@router.get("/summary")
def summary():
    rows = _registry_now()
    p = player().payload() or {}
    pilot = [r for r in rows if _registry_num(r) <= 15]
    overall = {"Green": 0, "Amber": 0, "Red": 0, "Unknown": 0}
    for r in pilot:
        overall[r.get("current_health", "Unknown")] = overall.get(r.get("current_health", "Unknown"), 0) + 1
    hi_missing = sum(1 for r in rows if r.get("risk_tier") == "High" and r.get("status") == "production"
                     and any(r.get(k) != v for k, v in APPROVED.items()))
    portfolio_map = [
        {
            "registry_id": r["registry_id"],
            "use_case_name": r["use_case_name"],
            "current_health": r.get("current_health", "Unknown"),
            "status": r.get("status", "Unknown"),
            "risk_tier": r.get("risk_tier", "Unknown"),
            "monitoring_readiness": _monitoring_readiness(r),
        }
        for r in sorted(rows, key=_registry_num)
    ]
    readiness_counts = {"deep_simulated": 0, "pilot_register_only": 0, "not_instrumented": 0}
    for r in portfolio_map:
        readiness_counts[r["monitoring_readiness"]] += 1
    return {
        "as_of_tick": player().tick, "date": p.get("date"),
        "use_case_count": len(rows),
        "status_counts": _status_counts(rows),
        "overall_counts": overall,
        "high_risk_missing_approval": hi_missing,
        "missing_risk_assessment": sum(1 for r in rows if r.get("risk_tier") == "Unknown"),
        "pilot_count": len(pilot),
        "pilot": [{"registry_id": r["registry_id"], "use_case_name": r["use_case_name"],
                   "current_health": r.get("current_health", "Unknown")} for r in pilot],
        "portfolio_map": portfolio_map,
        "readiness_counts": readiness_counts,
        "open_actions": p.get("summary", {}).get("open_actions", 0),
        "critical_actions": p.get("summary", {}).get("critical_actions", 0),
        "sla_breaches": p.get("summary", {}).get("sla_breaches", 0),
    }


@router.get("/registry")
def registry(status: str | None = None, risk_tier: str | None = None, health: str | None = None,
             use_case_group: str | None = None, business_unit: str | None = None,
             q: str | None = None, group: str | None = None):
    rows = _registry_now()
    if group == "pilot":
        rows = [r for r in rows if int(r["registry_id"].split("P")[-1]) <= 15]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if risk_tier:
        rows = [r for r in rows if r.get("risk_tier") == risk_tier]
    if health:
        rows = [r for r in rows if r.get("current_health") == health]
    if use_case_group:
        rows = [r for r in rows if r.get("use_case_group") == use_case_group]
    if business_unit:
        rows = [r for r in rows if r.get("business_unit") == business_unit]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r.get("use_case_name", "").lower()
                or ql in r["registry_id"].lower()]
    # join most-severe open action per registry_id (View 3 needs Next action / Due)
    p = player().payload() or {}
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    open_by_uc: dict[str, dict] = {}
    for a in p.get("actions", []):
        if a["status"] in ("Open", "In progress"):
            cur = open_by_uc.get(a["registry_id"])
            if cur is None or sev_rank[a["severity"]] < sev_rank[cur["severity"]]:
                open_by_uc[a["registry_id"]] = a
    out = []
    for r in rows:
        a = open_by_uc.get(r["registry_id"])
        out.append({
            "registry_id": r["registry_id"], "use_case_name": r["use_case_name"],
            "use_case_group": r.get("use_case_group", "Unknown"),
            "business_unit": r.get("business_unit", "Unknown"),
            "platform_or_app": r.get("platform_or_app", "Unknown"),
            "status": r.get("status", "Unknown"), "risk_tier": r.get("risk_tier", "Unknown"),
            "current_health": r.get("current_health", "Unknown"),
            "telemetry_status": r.get("telemetry_status", "Unknown"),
            "monitoring_owner": r.get("monitoring_owner", "Unknown"),
            "privacy_status": r.get("privacy_status", "Unknown"),
            "security_status": r.get("security_status", "Unknown"),
            "rai_status": r.get("rai_status", "Unknown"),
            "ai_readiness_status": r.get("ai_readiness_status", "Unknown"),
            "last_reviewed": r.get("last_reviewed", "Unknown"),
            "next_review": r.get("next_review", "Unknown"),
            "open_actions": r.get("open_actions", 0),
            "evidence_note": r.get("evidence_note"),
            "next_action": {"action_id": a["action_id"], "recommended_action": a["recommended_action"],
                            "due_date": a["due_date"], "due_tick": a["due_tick"],
                            "severity": a["severity"], "escalated": a["escalated"]} if a else None,
        })
    return {"rows": out}


@router.get("/heatmap")
def heatmap():
    rows = _registry_now()
    pilot = [r for r in rows if int(r["registry_id"].split("P")[-1]) <= 15]
    p = player().payload() or {}
    uc_payload = p.get("use_cases", {})
    out = []
    for r in pilot:
        lanes = {}
        deep = uc_payload.get(r["registry_id"])
        for lane in LANES:
            lanes[lane] = (deep["lanes"].get(lane) if deep and lane in deep.get("lanes", {})
                           else r.get(LANE_FIELD[lane], "Unknown"))
        # tooltips: metric + threshold + driving signal
        tooltips = {}
        for lane in LANES:
            field = LANE_FIELD[lane].replace("_status", "")
            metric_field = {"quality": "quality_metric", "safety": "safety_controls",
                            "reliability": "reliability_metric", "degradation": "degradation_signal",
                            "feedback": "feedback_source"}.get(field, "")
            tooltips[lane] = {"metric": r.get(metric_field, "—"),
                              "threshold": r.get("quality_threshold", "") if field == "quality" else ""}
        out.append({"registry_id": r["registry_id"], "use_case_name": r["use_case_name"],
                    "risk_tier": r.get("risk_tier", "Unknown"),
                    "overall": r.get("current_health", "Unknown"),
                    "lanes": lanes, "tooltips": tooltips,
                    "feedback_unknown_reason": r.get("feedback_unknown_reason")})
    return {"rows": out}


@router.get("/use-cases/{registry_id}")
def use_case(registry_id: str):
    rows = _registry_now()
    row = next((r for r in rows if r["registry_id"] == registry_id), None)
    if row is None:
        raise HTTPException(404, detail=f"unknown registry_id {registry_id}")
    p = player().payload() or {}
    deep = p.get("use_cases", {}).get(registry_id)
    signals = []
    artifacts: dict = {}
    extras: dict = {}
    if deep:
        cur_t = p.get("local_tick", p.get("tick", 0))
        hist = deep.get("history", {})
        for key, s in deep.get("signals", {}).items():
            spec = SIGNAL_SPECS.get(key)
            signals.append({
                "key": key, "label": spec.label if spec else key,
                "lane": spec.lane if spec else "Quality",
                "value": s.get("value"), "health": s.get("health"),
                "pending_reason": s.get("pending_reason"),
                "green_bar": spec.green_bar if spec else None,
                "red_bar": spec.red_bar if spec else None,
                "unit": spec.unit if spec else "", "direction": spec.direction if spec else "",
                "provenance": spec.provenance if spec else "",
                "history": [h for h in hist.get(key, []) if h["tick"] <= p.get("tick", 0)],
            })
        artifacts = dict(deep.get("artifacts", {}))
        if registry_id == "AICT-P01":
            artifacts["traces"] = db.get_traces(baker.MASTER, p.get("tick", 0), registry_id)
            extras = {"judge_sample": deep.get("judge_sample", []),
                      "corpus": _corpus_payload()}
        else:
            extras = {"drifted_features": deep.get("drifted_features", []),
                      "lime_top": deep.get("lime_top", []),
                      "lime_instance": deep.get("lime_instance", {}),
                      "reference_auc": deep.get("reference_auc"),
                      "model_version": deep.get("model_version", 1),
                      "realized_pending_reason": deep.get("realized_pending_reason")}
        extras["errors"] = deep.get("errors", {})
        extras["lanes"] = deep.get("lanes", {})
    actions = [a for a in (p.get("actions", []) if p else []) if a["registry_id"] == registry_id]
    return {**row, "cadence": _cadence(row.get("risk_tier", "Unknown")),
            "signals": signals, "artifacts": artifacts, "actions": actions, **extras}


def _corpus_payload() -> dict:
    from ..datagen import hr_corpus
    return {"topics": hr_corpus.CORPUS,
            "qa": [{"question": x["q"], "reference": x["ref"], "answerable": x["answerable"],
                    "topic": x["topic"]} for x in hr_corpus.QA]}


def _cadence(tier: str) -> str:
    return {"High": "Weekly (near-real-time alerts for critical signals)",
            "Medium": "Monthly with sampled quality checks",
            "Low": "Quarterly (minimum six-month confirmation)",
            "Unknown": "Monthly until classified"}.get(tier, "Monthly until classified")


@router.get("/actions")
def actions(status: str | None = None, severity: str | None = None, registry_id: str | None = None):
    p = player().payload() or {}
    rows = p.get("actions", [])
    if status:
        rows = [a for a in rows if a["status"] == status]
    if severity:
        rows = [a for a in rows if a["severity"] == severity]
    if registry_id:
        rows = [a for a in rows if a["registry_id"] == registry_id]
    reg = {r["registry_id"]: r for r in db.get_registry_rows()}
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    rows = sorted(rows, key=lambda a: (sev_rank[a["severity"]], a["opened_at_tick"]))
    cur_tick = (player().payload() or {}).get("tick", 0)
    out = []
    for a in rows:
        r = reg.get(a["registry_id"], {})
        out.append({**a, "use_case_name": r.get("use_case_name", a["registry_id"]),
                    "risk_tier": r.get("risk_tier", "Unknown"),
                    "t_minus": a["due_tick"] - cur_tick})
    return {"actions": out, "as_of_tick": player().tick}


class ActionPatch(BaseModel):
    status: str | None = None
    owner: str | None = None
    due_date: str | None = None
    evidence_link: str | None = None


@router.patch("/actions/{action_id}")
def patch_action(action_id: str, patch: ActionPatch):
    if patch.status == "Closed" and not (patch.evidence_link or "").strip():
        # evidence-before-closure (B.3) — unless the baked timeline already closed it
        p = player().payload() or {}
        baked = next((a for a in p.get("actions", []) if a["action_id"] == action_id), None)
        if not (baked and baked.get("evidence_link")):
            raise HTTPException(422, detail="Closing an action requires a non-empty evidence_link (B.3)")
    updated = player().patch_action(action_id, patch.model_dump(exclude_none=True))
    if updated is None:
        raise HTTPException(404, detail=f"unknown action {action_id}")
    player().broker.publish({"event": "action_updated", "data": {"action_id": action_id}})
    return updated


@router.get("/alerts")
def alerts(since_tick: int | None = None):
    p = player().payload() or {}
    rows = p.get("alerts", [])
    if since_tick is not None:
        rows = [a for a in rows if a["tick"] > since_tick]
    return {"alerts": rows, "as_of_tick": player().tick}


@router.get("/board-narrative")
def board_narrative():
    rows = _registry_now()
    p = player().payload() or {}
    actions_all = p.get("actions", [])
    closed = [a for a in actions_all if a["status"] == "Closed"]
    closed_on_time = [a for a in closed if _close_tick(a) is not None and _close_tick(a) <= a["due_tick"]]
    open_actions = [a for a in actions_all if a["status"] in ("Open", "In progress")]
    tiers: dict[str, int] = {}
    for r in rows:
        tiers[r.get("risk_tier", "Unknown")] = tiers.get(r.get("risk_tier", "Unknown"), 0) + 1
    telem: dict[str, int] = {}
    for r in rows:
        telem[r.get("telemetry_status", "Unknown")] = telem.get(r.get("telemetry_status", "Unknown"), 0) + 1
    pilot = [r for r in rows if int(r["registry_id"].split("P")[-1]) <= 15]
    owners_named = sum(1 for r in pilot if all(
        r.get(k, "Unknown") != "Unknown" for k in ("business_owner", "technical_owner", "monitoring_owner")))
    statements = [
        {"text": "One registry links AI Reporting Tool / VRO / TPM records to assurance evidence.",
         "stat": {"total_rows": len(rows),
                  "pct_with_source_record": round(100 * sum(
                      1 for r in rows if r.get("source_record_id", "Unknown") != "Unknown") / len(rows), 1)}},
        {"text": "Risk-based monitoring separates high, medium, low, and unknown cases.",
         "stat": {"by_risk_tier": tiers,
                  "cadence": {"High": "weekly", "Medium": "monthly", "Low": "quarterly",
                              "Unknown": "monthly until classified"}}},
        {"text": "Named owners make validation and post-production action accountable.",
         "stat": {"pilot_rows_all_owners_named_pct": round(100 * owners_named / max(1, len(pilot)), 1)}},
        {"text": "Red/amber/unknown items become action queues, not hidden spreadsheet gaps.",
         "stat": {"open_actions": len(open_actions),
                  "closed_within_sla_pct": round(100 * len(closed_on_time) / len(closed), 1) if closed else None,
                  "escalations": sum(1 for a in actions_all if a.get("escalated"))}},
        {"text": "The intelligence layer can become the operating backbone as platform telemetry matures.",
         "stat": {"telemetry_status_distribution": telem}},
    ]
    proof_stats = {
        "use_cases_monitored": len(pilot),
        "red_open": sum(1 for r in pilot if r.get("current_health") == "Red"),
        "critical_open": sum(1 for a in open_actions if a["severity"] == "Critical"),
        "actions_closed_with_evidence": sum(1 for a in closed if a.get("evidence_link")),
        "mean_ticks_to_action": 0,
        "amber_watchlisted": sum(1 for r in pilot if r.get("current_health") == "Amber"),
    }
    return {"as_of_tick": player().tick, "date": p.get("date"),
            "statements": statements, "proof_stats": proof_stats}


def _close_tick(a: dict) -> int | None:
    for h in reversed(a.get("history", [])):
        if "closed" in h.get("event", ""):
            return h["tick"]
    return None


@router.get("/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    meta = db.get_artifact(artifact_id)
    if not meta:
        raise HTTPException(404, detail="unknown artifact id")
    path = config.ARTIFACTS_DIR / meta["filename"]
    if not path.exists():
        raise HTTPException(404, detail="artifact file missing")
    return FileResponse(path, media_type=meta["content_type"])


# ------------------------------------------------------------------ scenario control

@router.get("/scenario/state")
def scenario_state():
    return player().state()


class LoadBody(BaseModel):
    scenario_id: str


@router.post("/scenario/load")
def scenario_load(body: LoadBody):
    try:
        return player().load(body.scenario_id)
    except KeyError:
        raise HTTPException(404, detail=f"unknown scenario {body.scenario_id}")


@router.post("/scenario/play")
async def scenario_play():
    # async so play() can create its asyncio task on the server's main event loop
    return player().play()


@router.post("/scenario/pause")
async def scenario_pause():
    return player().pause()


@router.post("/scenario/step")
def scenario_step():
    return player().step()


class SpeedBody(BaseModel):
    multiplier: int


@router.post("/scenario/speed")
def scenario_speed(body: SpeedBody):
    try:
        return player().set_speed(body.multiplier)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))


class JumpBody(BaseModel):
    tick: int


@router.post("/scenario/jump")
def scenario_jump(body: JumpBody):
    return player().jump(body.tick)


@router.post("/scenario/reset")
def scenario_reset():
    return player().reset()


@router.get("/events")
async def events(request: Request):
    q = player().broker.subscribe()

    async def gen():
        try:
            # initial state so late subscribers sync immediately
            yield {"event": "scenario_state", "data": json.dumps(player().state())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": ev.get("event", "message"), "data": json.dumps(ev.get("data", {}))}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            player().broker.unsubscribe(q)

    return EventSourceResponse(gen())


# ------------------------------------------------------------------ live monitoring
# Separate from the baked scenario player: the monitor PULLS telemetry from external
# model apps, runs its engines, and grades a LIVE use case. The baked demo is untouched.

from ..scenario.live_runner import live_runner, reset_live_runner  # noqa: E402
from ..live_sync import portfolio_sync, source_sync  # noqa: E402

from . import live_portfolio  # noqa: E402


@router.post("/live/tick")
async def live_tick(uc: str = "AICT-L01"):
    """Observe the next live telemetry window and grade it. `uc` selects the use case:
    AICT-L01 = churn (Evidently/CBPE/LIME/SHAP), AICT-L02 = chatbot (LLM-as-judge),
    AICT-L03 = NBA recommender (+ acceptance_rate / recommendation_drift). The
    adapters do sync HTTP + heavy engine work, so run it off the event loop."""
    return await anyio.to_thread.run_sync(lambda: live_runner(uc).tick())


@router.get("/live/state")
def live_state(uc: str = "AICT-L01"):
    p = live_runner(uc).state()
    return p or {"use_case_id": uc, "mode": "live", "tick": None,
                 "message": "waiting for the backend poller to observe a closed window",
                 **source_sync(uc)}


@router.post("/live/reset")
def live_reset(uc: str | None = None):
    db.clear_live_state(uc)
    reset_live_runner(uc)
    return {"reset": True, "uc": uc or "all"}


@router.get("/live/portfolio")
def live_portfolio_view():
    """LIVE plane for the control tower: header counts + one row per live use case."""
    return {**live_portfolio.portfolio_summary(), "rows": live_portfolio.portfolio_rows()}


@router.get("/live/use-case/{uc}")
def live_use_case(uc: str):
    """Deep detail for one live use case (signals, lanes, artifacts, lane extras)."""
    d = live_portfolio.detail(uc)
    if d is None:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    return d


@router.get("/live/sync")
def live_sync_all():
    """Durable producer-to-monitor cursor state for every live use case."""
    return portfolio_sync(live_portfolio.LIVE_UCS)


@router.get("/live/sync/{uc}")
def live_sync_one(uc: str):
    if uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    return source_sync(uc)


@router.get("/live/observations")
def live_observations(uc: str | None = None, limit: int = 100):
    if uc is not None and uc not in live_portfolio.LIVE_UCS:
        raise HTTPException(404, detail=f"unknown live use case {uc}")
    from .live_routes import _public_payload
    rows = db.list_live_observations(uc, limit)
    for row in rows:
        row["payload"] = _public_payload(row.get("payload") or {})
    # The live namespace never reads baked ticks or scenario snapshots.
    return {"contract_version": "1.1", "rows": rows}


@router.get("/live/alerts")
def live_alerts(uc: str | None = None, open: bool = False, limit: int = 50):
    """Same read-only alert listing as the strict router (the dev SPA fetches it)."""
    from .live_routes import alerts
    return alerts(uc, open, limit)


@router.get("/live/artifacts/{artifact_id}")
def live_artifact(artifact_id: str):
    meta = db.get_artifact(artifact_id)
    if not meta or not str(meta.get("scenario_id", "")).startswith("LIVE-"):
        raise HTTPException(404, detail="unknown live artifact id")
    if meta.get("kind") == "lime_html":
        raise HTTPException(404, detail="per-instance artifact is redacted")
    blob = db.get_live_artifact_blob(artifact_id)
    if not blob:
        raise HTTPException(404, detail="live artifact not persisted")
    return Response(content=blob["content"], media_type=blob["content_type"],
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-SHA256": blob["content_sha256"]})


@router.post("/live/tick-all")
async def live_tick_all():
    """Advance all three live runners one window and record history. Runs off the
    event loop (each tick() does sync HTTP + heavy engine work)."""
    def _run() -> dict:
        as_of: dict[str, int | None] = {}
        states = []
        for uc in live_portfolio.LIVE_UCS:
            p = live_runner(uc).tick()
            if not p.get("waiting"):
                live_portfolio.record(uc, p)
            states.append(p)
            s = live_runner(uc).state()
            as_of[uc] = s.get("tick") if s else None
        return {"as_of": as_of, "states": states}

    return await anyio.to_thread.run_sync(_run)


# ------------------------------------------------------------------ dev-only / export

class BakeBody(BaseModel):
    scenario_id: str = "DEMO-FULL"


@router.post("/sim/bake")
async def sim_bake(body: BakeBody):
    result = await anyio.to_thread.run_sync(lambda: baker.bake(body.scenario_id))
    from ..scenario.player import reset_player
    reset_player()
    return {"scenario_id": body.scenario_id, "total_ticks": result["total_ticks"], "baked": True}


@router.get("/export/demo-snapshots")
def demo_snapshots(scenario_id: str = "DEMO-FULL"):
    pl = player()
    lo, hi = pl.src.load(scenario_id).range_in_master
    ticks = []
    for mt in range(lo, hi + 1):
        p = db.get_baked_tick(baker.MASTER, mt)
        if p:
            ticks.append({"tick": mt - lo, "date": p["date"], "summary": p["summary"],
                          "overall": {uc: v["overall"] for uc, v in p["use_cases"].items()},
                          "actions_open": [a["action_id"] for a in p["actions"]
                                           if a["status"] in ("Open", "In progress")],
                          "artifacts": {uc: v.get("artifacts", {}) for uc, v in p["use_cases"].items()}})
    return {"scenario_id": scenario_id, "mark": "CPG Confidential · Simulated data",
            "seed": pl.seed, "ticks": ticks}
