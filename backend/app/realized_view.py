"""Grade a live use case with its latest realized metric (spec B.4, contract §7).

The stored observation for tick *t* can only say "labels pending".  Once the backfill
realizes an older tick, the dashboard should grade the Performance (and NBA Feedback)
lane on that value rather than keep it reasoned-Unknown.  `apply_realized` does this as
a read-time view: it returns a *copy* of the runner state with the realized signals
filled in and the lane/overall rollup recomputed, and never writes anything.  Without a
realized row the state is returned unchanged.
"""
from __future__ import annotations

import copy

from . import db
from .engines import health

REALIZED_KEYS = ("realized_roc_auc", "acceptance_rate")
_PENDING_FIELD = {"realized_roc_auc": "realized_pending_reason",
                  "acceptance_rate": "acceptance_pending_reason"}
_FEEDBACK_LANE = "Feedback & action loop"


def _rollup_meta(state: dict) -> dict:
    """The rollup inputs the runner used; reconstructed for observations stored before
    `rollup_meta` existed (hand-set lanes are exactly the `lane_reasons` lanes)."""
    meta = state.get("rollup_meta")
    if isinstance(meta, dict):
        return {"hand_set_lanes": dict(meta.get("hand_set_lanes") or {}),
                "excluded_lanes": set(meta.get("excluded_lanes") or []),
                "excluded_keys": set(meta.get("excluded_keys") or [])}
    lanes = state.get("lanes") or {}
    signals = state.get("signals") or {}
    hand_set = {lane: lanes.get(lane, "Unknown") for lane in (state.get("lane_reasons") or {})}
    excluded_keys = {k for k, s in signals.items() if (s or {}).get("pending_reason")}
    if (signals.get("acceptance_rate") or {}).get("pending_reason"):
        hand_set.setdefault(_FEEDBACK_LANE, "Unknown")
    return {"hand_set_lanes": hand_set, "excluded_lanes": set(hand_set),
            "excluded_keys": excluded_keys}


def apply_realized(uc: str, state: dict | None) -> dict | None:
    if not state or not isinstance(state.get("signals"), dict):
        return state
    rows = {key: db.latest_realized(uc, key) for key in REALIZED_KEYS if key in state["signals"]}
    rows = {key: row for key, row in rows.items() if row and row.get("value") is not None}
    if not rows:
        return state

    out = copy.deepcopy(state)
    meta = _rollup_meta(out)
    for key, row in rows.items():
        sig = out["signals"][key]
        sig["value"] = round(float(row["value"]), 4)
        sig["health"] = health.evaluate(key, row["value"])
        sig["as_of_tick"] = int(row["tick"])
        sig["coverage"] = row.get("coverage")
        sig.pop("pending_reason", None)
        meta["excluded_keys"].discard(key)
        out[_PENDING_FIELD[key]] = None
        if key == "acceptance_rate":          # the Feedback lane is measured again
            meta["hand_set_lanes"].pop(_FEEDBACK_LANE, None)
            meta["excluded_lanes"].discard(_FEEDBACK_LANE)
    s_health = {key: sig.get("health", "Unknown") for key, sig in out["signals"].items()}
    lanes, overall = health.rollup(
        s_health, excluded_keys=meta["excluded_keys"],
        hand_set_lanes=meta["hand_set_lanes"], excluded_lanes=meta["excluded_lanes"])
    out["lanes"], out["overall"] = lanes, overall
    out["rollup_meta"] = {"hand_set_lanes": meta["hand_set_lanes"],
                          "excluded_lanes": sorted(meta["excluded_lanes"]),
                          "excluded_keys": sorted(meta["excluded_keys"])}
    if "realized_roc_auc" in rows:
        out["realized_as_of_tick"] = int(rows["realized_roc_auc"]["tick"])
        out["realized_label_coverage"] = rows["realized_roc_auc"].get("coverage")
    if "acceptance_rate" in rows:
        out["acceptance_as_of_tick"] = int(rows["acceptance_rate"]["tick"])
    return out
