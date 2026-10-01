"""LIVE portfolio shaper — the dashboard's LIVE data source.

Additive plane over the three live runners (scenario/live_runner.py): merges the
static descriptors from live_registry_seed.json (the sole source of header /
metadata fields) with the graded state each runner exposes via .state(), and
keeps a small in-process per-signal history buffer. The baked plane is untouched
— this module never reaches into the scenario player/baker. No heavy work happens
here: tick() (sync HTTP + engine) is driven by POST /api/live/tick-all off the
event loop; this module only reshapes already-graded payloads for the UI.

_cadence and live_runner are imported lazily inside functions to avoid an import
cycle with routes.py (routes imports this module at load).
"""
from __future__ import annotations

import json

from .. import config, db
from ..engines.health import LANES
from ..live_sync import portfolio_sync, source_sync
from ..realized_view import REALIZED_KEYS, apply_realized

LIVE_UCS = ["AICT-L01", "AICT-L02", "AICT-L03"]

_DESCRIPTORS: dict[str, dict] | None = None

def load_descriptors() -> dict[str, dict]:
    """Read live_registry_seed.json once (cached), keyed by registry_id."""
    global _DESCRIPTORS
    if _DESCRIPTORS is None:
        with open(config.SEEDS_DIR / "live_registry_seed.json", encoding="utf-8") as f:
            rows = json.load(f)
        _DESCRIPTORS = {r["registry_id"]: r for r in rows}
    return _DESCRIPTORS


# lane kind per use case, straight from the descriptors ("ml" | "llm")
LANE_KIND = {uc: d.get("lane_kind", "ml") for uc, d in load_descriptors().items()}

# lane-specific extras passed through the detail payload verbatim when present
_EXTRA_KEYS = (
    "drifted_features", "reference_auc", "model_version", "realized_pending_reason",
    "realized_label_coverage", "lime_top", "lime_instance", "offer_mix",
    "baseline_offer_mix", "acceptance_pending_reason", "judge", "judge_sample",
    "lane_reasons", "window_id", "source_instance_id", "opened_at", "closed_at",
    "content_sha256", "first_record_id", "last_record_id", "provenance_counts",
    "record_count", "source_lag_ms", "observation_id", "ack_status", "ack_error",
    "empty_window", "realized_as_of_tick", "acceptance_as_of_tick", "rollup_meta",
)


def _cadence(tier: str) -> str:
    return {"High": "Weekly (near-real-time alerts for critical signals)",
            "Medium": "Monthly with sampled quality checks",
            "Low": "Quarterly (minimum six-month confirmation)",
            "Unknown": "Monthly until classified"}.get(tier, "Monthly until classified")


def _tooltips(state: dict) -> dict:
    """{lane -> {metric: primary signal label for that lane}} from the live signals."""
    out = {lane: {"metric": "—"} for lane in LANES}
    for key, sig in state.get("signals", {}).items():
        lane = sig.get("lane", "Quality")
        if lane in out and out[lane]["metric"] == "—":
            out[lane]["metric"] = sig.get("label", key)
    return out


def _feedback_unknown_reason(state: dict, desc: dict) -> str | None:
    """Why the Feedback (or Quality) lane reads Unknown — the live pending reason
    if one is present, else the descriptor's declared no-loop finding."""
    lanes = state.get("lanes", {})
    if lanes.get("Feedback & action loop") == "Unknown" or lanes.get("Quality") == "Unknown":
        return (state.get("acceptance_pending_reason")
                or state.get("realized_pending_reason")
                or desc.get("feedback_unknown_reason"))
    return desc.get("feedback_unknown_reason")


def _telemetry_status(sync: dict) -> str:
    return {
        "connecting": "Connecting", "catching_up": "Live — catching up",
        "at_tail": "Live", "idle": "Live — idle", "stale": "Stale",
        "error": "Error",
    }.get(sync.get("sync_state"), "Unknown")


def _not_observed_row(uc: str, desc: dict) -> dict:
    """Row for a use case whose runner has never completed a tick — this is WAITING for
    the first window, NOT 'app offline'. stale is reserved for a runner that had a good
    tick then lost telemetry (see portfolio_rows)."""
    sync = source_sync(uc)
    return {
        **desc,
        "current_health": "Unknown",
        "overall": "Unknown",
        "lanes": {lane: "Unknown" for lane in LANES},
        "lane_kind": desc.get("lane_kind", "ml"),
        "tick": None,
        "mode": "live",
        "waiting": True,
        "stale": sync["sync_state"] == "stale",
        "telemetry_status": _telemetry_status(sync),
        "feedback_unknown_reason": desc.get("feedback_unknown_reason"),
        "tooltips": {lane: {"metric": "—"} for lane in LANES},
        "errors": ({"telemetry": sync["last_error"]} if sync.get("last_error") else {}),
        **sync,
    }


def portfolio_rows() -> list[dict]:
    """One dashboard row per live use case (descriptor + live rollup, or stale)."""
    from ..scenario.live_runner import live_runner

    descs = load_descriptors()
    rows = []
    for uc in LIVE_UCS:
        desc = descs[uc]
        s = apply_realized(uc, live_runner(uc).state())
        if s is None:
            rows.append(_not_observed_row(uc, desc))    # waiting for first window, not offline
            continue
        errors = s.get("errors", {})
        sync = source_sync(uc)
        offline = sync["sync_state"] in ("stale", "error")
        observed_overall = s.get("overall", "Unknown")
        observed_lanes = s.get("lanes", {})
        rows.append({
            **desc,
            "current_health": "Unknown" if offline else observed_overall,
            "overall": "Unknown" if offline else observed_overall,
            "lanes": ({lane: "Unknown" for lane in LANES} if offline else observed_lanes),
            "last_observed_overall": observed_overall,
            "last_observed_lanes": observed_lanes,
            "lane_kind": desc.get("lane_kind", "ml"),
            "tick": s.get("tick"),
            "mode": "live",
            "waiting": False,
            "stale": offline,
            "telemetry_status": _telemetry_status(sync),
            "feedback_unknown_reason": _feedback_unknown_reason(s, desc),
            "tooltips": _tooltips(s),
            "errors": errors,
            "realized_as_of_tick": s.get("realized_as_of_tick"),
            "acceptance_as_of_tick": s.get("acceptance_as_of_tick"),
            **sync,
        })
    return rows


def portfolio_summary() -> dict:
    """Roll the 3 live use cases into header counts (overall + per lane)."""
    from ..scenario.live_runner import live_runner

    as_of: dict[str, int | None] = {}
    overall_counts = {"Green": 0, "Amber": 0, "Red": 0, "Unknown": 0}
    lane_counts = {lane: {"Green": 0, "Amber": 0, "Red": 0, "Unknown": 0} for lane in LANES}
    for uc in LIVE_UCS:
        s = apply_realized(uc, live_runner(uc).state())
        if s is None:
            as_of[uc] = None
            overall_counts["Unknown"] += 1
            for lane in LANES:
                lane_counts[lane]["Unknown"] += 1
            continue
        as_of[uc] = s.get("tick")
        sync = source_sync(uc)
        overall = ("Unknown" if sync["sync_state"] in ("stale", "error")
                   else s.get("overall", "Unknown"))
        overall_counts[overall] = overall_counts.get(overall, 0) + 1
        lanes = ({lane: "Unknown" for lane in LANES}
                 if sync["sync_state"] in ("stale", "error") else s.get("lanes", {}))
        for lane in LANES:
            h = lanes.get(lane, "Unknown")
            lane_counts[lane][h] = lane_counts[lane].get(h, 0) + 1
    sync = portfolio_sync(LIVE_UCS)
    return {"use_case_count": 3, "as_of": as_of,
            "overall_counts": overall_counts, "lane_counts": lane_counts,
            "sync_state": sync["sync_state"], "state": sync["state"],
            "backlog": sync["backlog"], "sync_sources": sync["sources"]}


def record(uc: str, state: dict) -> None:
    """Compatibility shim: live_runner now persists observations/history atomically."""


def detail(uc: str) -> dict | None:
    """404-safe deep detail for one live use case (descriptor + reshaped state)."""
    if uc not in LIVE_UCS:
        return None
    from ..scenario.live_runner import live_runner
    desc = load_descriptors()[uc]
    cadence = _cadence(desc.get("risk_tier", "Unknown"))
    s = apply_realized(uc, live_runner(uc).state())
    if s is None:  # never observed — waiting for the first window (header-only, never raises)
        sync = source_sync(uc)
        return {
            **desc,
            "cadence": cadence,
            "lane_kind": desc.get("lane_kind", "ml"),
            "current_health": "Unknown",
            "overall": "Unknown",
            "signals": [],
            "lanes": {lane: "Unknown" for lane in LANES},
            "artifacts": {},
            "errors": ({"telemetry": sync["last_error"]} if sync.get("last_error") else {}),
            "actions": [],
            "mode": "live",
            "tick": None,
            "waiting": True,
            "stale": sync["sync_state"] == "stale",
            "telemetry_status": _telemetry_status(sync),
            **sync,
        }
    sync = source_sync(uc)
    offline = sync["sync_state"] in ("stale", "error")
    signals = []
    for key, sig in s.get("signals", {}).items():
        signals.append({
            "key": key,
            "label": sig.get("label", key),
            "lane": sig.get("lane", "Quality"),
            "value": sig.get("value"),
            "health": "Unknown" if offline else sig.get("health"),
            "last_observed_health": sig.get("health"),
            "pending_reason": sig.get("pending_reason"),  # lifted from the nested signal
            "as_of_tick": sig.get("as_of_tick"),          # realized metrics: label tick
            "coverage": sig.get("coverage"),
            "green_bar": sig.get("green_bar"),
            "red_bar": sig.get("red_bar"),
            "unit": sig.get("unit", ""),
            "direction": sig.get("direction", ""),
            "provenance": "live",
            # realized metrics are graded on lagged labels, so their sparkline comes from
            # live_realized_metrics; the per-observation history rows stay as stored
            "history": (db.realized_history(uc, key, config.LIVE_HIST_MAX)
                        if key in REALIZED_KEYS
                        else db.get_live_signal_history(uc, key, config.LIVE_HIST_MAX)),
        })
    out = {
        **desc,
        "cadence": cadence,
        "lane_kind": desc.get("lane_kind", "ml"),
        "current_health": "Unknown" if offline else s.get("overall", "Unknown"),
        "overall": "Unknown" if offline else s.get("overall", "Unknown"),
        "last_observed_overall": s.get("overall", "Unknown"),
        "signals": signals,
        "lanes": ({lane: "Unknown" for lane in LANES} if offline else s.get("lanes", {})),
        "last_observed_lanes": s.get("lanes", {}),
        "artifacts": s.get("artifacts", {}),
        "errors": s.get("errors", {}),
        "actions": [],
        "mode": "live",
        "tick": s.get("tick"),
        "waiting": False,
        "stale": sync["sync_state"] == "stale",
        "cursor_held": bool(s.get("cursor_held")),
        "telemetry_status": _telemetry_status(sync),
        **sync,
    }
    for k in _EXTRA_KEYS:  # pass ML/LLM/NBA extras through verbatim when present
        if k in s:
            out[k] = s[k]
    return out
