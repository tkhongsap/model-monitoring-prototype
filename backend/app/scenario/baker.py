"""Scenario baker — precompute every tick of DEMO-FULL through the REAL engines
(§A.1.2, ADR-4). Order of operations per tick is normative (§A.1.1):
generate window → run engines → grade → roll up → fire/dedupe actions →
SLA-breach check → emit events. Persists complete per-tick state to SQLite.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .. import config, db
from ..adapters.base import TickContext
from ..adapters.explain.lime_shap import make_explain
from ..adapters.llm_eval.judge import make_llm_eval
from ..adapters.ml_monitor.evidently_nannyml import make_ml_monitor
from ..engines import actions as act
from ..engines import health
from .source import YamlScenarioSource

MASTER = "DEMO-FULL"

P01, P02 = "AICT-P01", "AICT-P02"
OWNER = "RAI COE Coordinator (role)"


def _artifact_writer_factory(scenario_id: str):
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    def write(kind: str, tick: int, content, ext: str) -> str:
        artifact_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{scenario_id}/{tick}/{kind}").hex
        filename = f"{scenario_id.lower()}-t{tick:02d}-{kind}.{ext}"
        path = config.ARTIFACTS_DIR / filename
        payload = content if isinstance(content, bytes) else content.encode("utf-8")
        path.write_bytes(payload)
        ctype = "text/html" if ext == "html" else "image/png"
        db.put_artifact(artifact_id, scenario_id, tick, kind, filename, ctype)
        if scenario_id.startswith("LIVE-"):
            db.put_live_artifact_blob(artifact_id, ctype, payload)
        return artifact_id

    return write


def bake(scenario_id: str = MASTER, seed: int | None = None, log=print) -> dict:
    """Bake the DEMO-FULL master (all standalone scenarios are views into it)."""
    src = YamlScenarioSource()
    script = src.load(MASTER)
    seed = seed if seed is not None else int(src.sim.get("seed", config.DEMO_SEED))
    weekly = int(src.sim.get("weekly_review_every", 7))
    label_lag = int(src.churn.get("label_lag_ticks", src.sim.get("label_lag_ticks", 3)))

    db.clear_bake(MASTER)
    writer = _artifact_writer_factory(MASTER)

    log(f"[bake] {MASTER} seed={seed} ticks={script.ticks} — initializing ML world…")
    # factories with the seeded impl PINNED by name → golden bake byte-identical
    # regardless of env (only the live runner reads config.*_ADAPTER).
    world = make_ml_monitor(seed, src.churn, writer, impl="evidently_nannyml")
    explainer = make_explain(seed, world, writer, impl="lime_shap")
    llm = make_llm_eval(seed, MASTER, P01)
    engine = act.ActionEngine()

    events_by_tick: dict[int, list[dict]] = {}
    for ev in script.events:
        events_by_tick.setdefault(int(ev["tick"]), []).append(ev)

    history: dict[str, dict[str, list]] = {P01: {}, P02: {}}
    weekly_amber: dict[tuple[str, str], int] = {}   # consecutive-Amber count at reviews
    last_review_tick = 0
    sla_breaches_total = 0

    for t in range(script.ticks):
        events: list[dict] = []
        scripted = events_by_tick.get(t, [])

        # scripted world-mutating events happen at tick START (S5 semantics, §A.5.5)
        for ev in scripted:
            if ev["type"] == "RETRAIN_REBASELINE":
                world.retrain_rebaseline(t)
                events.append({"type": "RETRAIN_REBASELINE", "lane": ev.get("lane")})
            elif ev["type"] == "KB_UPDATE":
                events.append({"type": "KB_UPDATE", "lane": ev.get("lane")})

        # --- run engines ---
        alpha = script.churn_alpha[t] if t < len(script.churn_alpha) else script.churn_alpha[-1]
        n_h = script.llm_halluc[t] if t < len(script.llm_halluc) else script.llm_halluc[-1]
        tick_ml = TickContext(t, seed, MASTER, {"churn_alpha": alpha})
        tick_llm = TickContext(t, seed, MASTER, {"n_halluc": n_h})

        ml = world.monitor(P02, tick_ml)
        ex = explainer.explain(P02, tick_ml)
        lm = llm.evaluate(P01, tick_llm)

        # --- grade ---
        p01_health = {k: health.evaluate(k, v) for k, v in lm.signals.items()}
        p02_health = {k: health.evaluate(k, v) for k, v in ml.signals.items()}
        realized_pending = ml.records.get("realized_pending_reason") if isinstance(ml.records, dict) else None
        excluded = {"realized_roc_auc"} if realized_pending else set()
        if realized_pending:
            p02_health["realized_roc_auc"] = "Unknown"  # Pending, reasoned → excluded from rollup

        p01_lanes, p01_overall = health.rollup(
            p01_health, hand_set_lanes={"Feedback & action loop": "Green",
                                        "Drift & degradation": "Green"})
        p02_lanes, p02_overall = health.rollup(
            p02_health, excluded_keys=excluded,
            hand_set_lanes={"Feedback & action loop": "Unknown",
                            "Safety & security": "Green", "Reliability": "Green"},
            excluded_lanes={"Feedback & action loop"})  # declared-reason Unknown (§A.1.4)

        # --- actions: fire/dedupe on Red ---
        # ML lane observed first: fixes scripted action-ID assignment when both lanes
        # go Red in the same tick (A.5: t13 -> ACT-002 realized_roc_auc, ACT-003 hallucination)
        for uc, healths, signals, tier in ((P02, p02_health, ml.signals, "High"),
                                           (P01, p01_health, lm.signals, "High")):
            for key, h in healths.items():
                if key in excluded and uc == P02:
                    continue
                engine.observe(t, uc, key, signals.get(key), h, tier, OWNER, events)

        # --- scripted CLOSE_ACTION (after grading — post-remediation ticks are Green) ---
        for ev in scripted:
            if ev["type"] == "CLOSE_ACTION":
                engine.close(t, list(ev.get("targets", [])), ev.get("evidence_link", ""),
                             events, suppress_ticks=label_lag)

        # --- weekly review (Amber persistence + review stamps) ---
        weekly_review = (t > 0 and t % weekly == 0)
        if weekly_review:
            last_review_tick = t
            for uc, healths, tier in ((P01, p01_health, "High"), (P02, p02_health, "High")):
                for key, h in healths.items():
                    k = (uc, key)
                    if h == "Amber":
                        weekly_amber[k] = weekly_amber.get(k, 0) + 1
                        if weekly_amber[k] >= 2:
                            engine.amber_persistence(t, uc, key, tier, OWNER, events)
                    else:
                        weekly_amber[k] = 0
            events.append({"type": "WEEKLY_REVIEW", "tick": t})

        # --- SLA breach check (per tick, after grading) ---
        before = sum(1 for a in engine.actions if a["escalated"])
        engine.sla_breach_check(t, events)
        sla_breaches_total += sum(1 for a in engine.actions if a["escalated"]) - before

        # --- histories ---
        for uc, signals, healths in ((P01, lm.signals, p01_health), (P02, ml.signals, p02_health)):
            for key, v in signals.items():
                history[uc].setdefault(key, []).append(
                    {"tick": t, "value": None if v is None else round(float(v), 4),
                     "health": healths[key],
                     **({"pending_reason": realized_pending}
                        if (uc == P02 and key == "realized_roc_auc" and realized_pending) else {})})

        actions_snap, alerts_snap = engine.snapshot()
        open_actions = [a for a in actions_snap if a["status"] in ("Open", "In progress")]

        ml_records = ml.records if isinstance(ml.records, dict) else {}
        payload = {
            "tick": t, "date": act.tick_date(t), "scenario_id": MASTER, "seed": seed,
            "use_cases": {
                P01: {
                    "signals": {k: {"value": _r(v), "health": p01_health[k]} for k, v in lm.signals.items()},
                    "history": history[P01], "lanes": p01_lanes, "overall": p01_overall,
                    "judge_sample": lm.records, "artifacts": lm.artifacts, "errors": lm.errors,
                },
                P02: {
                    "signals": {k: {"value": _r(v), "health": p02_health[k],
                                    **({"pending_reason": realized_pending}
                                       if (key_is_realized := k == "realized_roc_auc") and realized_pending else {})}
                                for k, v in ml.signals.items()},
                    "history": history[P02], "lanes": p02_lanes, "overall": p02_overall,
                    "drifted_features": ml_records.get("drifted_features", []),
                    "reference_auc": _r(ml_records.get("reference_auc")),
                    "model_version": ml_records.get("model_version", 1),
                    "realized_pending_reason": realized_pending,
                    "lime_top": ex.lime_top, "lime_instance": ex.instance,
                    "artifacts": {**ml.artifacts, **ex.artifacts},
                    "errors": {**ml.errors, **ex.errors},
                },
            },
            "actions": actions_snap, "alerts": alerts_snap, "events": events,
            "registry_overrides": {
                P01: _overrides(p01_lanes, p01_overall, open_actions, P01, last_review_tick, weekly),
                P02: _overrides(p02_lanes, p02_overall, open_actions, P02, last_review_tick, weekly),
            },
            "summary": {
                "open_actions": len(open_actions),
                "critical_actions": sum(1 for a in open_actions if a["severity"] == "Critical"),
                "sla_breaches": sum(1 for a in actions_snap if a["escalated"]),
            },
        }
        db.put_baked_tick(MASTER, t, payload)
        log(f"[bake] t{t:02d} drift={_fmt(ml.signals.get('data_drift_share'))} "
            f"est={_fmt(ml.signals.get('estimated_roc_auc'))} real={_fmt(ml.signals.get('realized_roc_auc'))} "
            f"hall={_fmt(lm.signals.get('hallucination_rate'))} "
            f"P02={p02_overall} P01={p01_overall} open={len(open_actions)}")

    manifest = _manifest(seed, script)
    db.put_manifest(MASTER, manifest)
    db.put_state(scenario_id=MASTER, tick=0, playing=0, speed=1, mode="baked", seed=seed)
    log(f"[bake] done — manifest {manifest['config_hash'][:12]}")
    return {"scenario_id": scenario_id, "total_ticks": script.ticks, "baked": True,
            "manifest": manifest}


def _overrides(lanes: dict, overall: str, open_actions: list, uc: str,
               last_review_tick: int, weekly: int) -> dict:
    mine = [a for a in open_actions if a["registry_id"] == uc]
    lane_field = {"Quality": "quality_status", "Safety & security": "safety_status",
                  "Reliability": "reliability_status", "Drift & degradation": "degradation_status",
                  "Feedback & action loop": "feedback_status"}
    out = {lane_field[l]: h for l, h in lanes.items() if l in lane_field}
    out.update({
        "current_health": overall, "open_actions": len(mine),
        "last_reviewed": act.tick_date(last_review_tick),
        "next_review": act.tick_date(last_review_tick + weekly),
    })
    return out


def _manifest(seed: int, script) -> dict:
    import importlib.metadata as md
    versions = {}
    for pkg in ("evidently", "nannyml", "lime", "shap", "scikit-learn", "numpy", "pandas"):
        try:
            versions[pkg] = md.version(pkg)
        except Exception:  # noqa: BLE001
            versions[pkg] = "missing"
    cfg_hash = hashlib.sha256(json.dumps(
        {"seed": seed, "alpha": script.churn_alpha, "halluc": script.llm_halluc,
         "events": script.events}, sort_keys=True).encode()).hexdigest()
    return {"seed": seed, "config_hash": cfg_hash, "package_versions": versions}


def _r(v):
    return None if v is None else round(float(v), 4)


def _fmt(v):
    return "—" if v is None else f"{v:.3f}"
