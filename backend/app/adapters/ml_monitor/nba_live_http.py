"""LiveHttpNBAAdapter — LIVE monitoring of the Next-Best-Action recommender.

A thin subclass of LiveHttpMLAdapter: same engines (Evidently drift, CBPE estimate,
realized AUC on the partial matched subset), different telemetry shape — the app emits
/telemetry/recommendations (accept_proba, rec_id, offer_mix) and /telemetry/rewards
(rec_id, accepted), where Reward.accepted is the binary label. Adds two NBA signals:

  - acceptance_rate: mean(accepted) over the matched rewards subset. Same pending
    semantics as realized AUC — while rewards lag (or coverage < 50%) the signal is
    None with errors["acceptance_pending"], and the runner excludes it from rollup.
  - recommendation_drift: total-variation distance (0.5 * L1) between the CURRENT
    window's offer mix and a BASELINE mix. Baseline capture rule: the baseline is the
    offer_mix of the first window observed after each (re)baseline WHOSE served_version
    matches the pulled model version — a lagging monitor replaying pre-retrain windows
    after a version change must not anchor the new policy's baseline to the OLD
    policy's mix (review finding). Until a version-matched window is seen the signal is
    None with errors["recommendation_drift_pending"], excluded from rollup with reason.
    The captured mix is persisted in `live_baselines` per (source, model version) and
    read back on a cold start, so an Autoscale restart does not reset the signal to
    pending (spec D.4). The database is the only state the adapter shares; a failure
    there degrades to the in-memory baseline and never fails the tick.

No churn fallbacks here: feature order/categoricals come from the artifact meta or
sorted record keys only (the NBA service owns its feature list, contract v1.0).
"""
from __future__ import annotations

import logging

import numpy as np

from ... import db
from ..base import LaneResult
from .live_http import LiveHttpMLAdapter
from .realized import MIN_COVERAGE, RealizedResult


def _tv_distance(p: dict, q: dict) -> float:
    """Total-variation distance between two offer-mix distributions (0.5 * L1)."""
    keys = set(p) | set(q)
    return 0.5 * sum(abs(float(p.get(k, 0.0)) - float(q.get(k, 0.0))) for k in keys)


log = logging.getLogger(__name__)

BASELINE_KIND = "offer_mix"


class LiveHttpNBAAdapter(LiveHttpMLAdapter):
    name = "live_http_nba"

    _fallback_features = None       # no churn heritage — meta / record keys only
    _fallback_categorical = None
    _label_field = "accepted"       # Reward.accepted is the label
    _signal_keys = ("data_drift_share", "estimated_roc_auc", "realized_roc_auc",
                    "acceptance_rate", "recommendation_drift")

    def __init__(self, base_url: str, artifact_writer, chunk_size: int = 500,
                 model_name: str = "nba-recommender", source_id: str = "AICT-L03") -> None:
        super().__init__(base_url, artifact_writer, chunk_size=chunk_size,
                         model_name=model_name,
                         inferences_path="/telemetry/recommendations",
                         labels_path="/telemetry/rewards",
                         proba_field="accept_proba", id_field="rec_id")
        self.source_id = source_id
        self._baseline_mix: dict | None = None
        self._baseline_loaded_for: int | None = None   # version whose row was looked up

    def _on_rebaseline(self) -> None:
        # the new model's baseline: the stored one if this version was seen before a
        # restart, otherwise re-captured from its first window
        self._baseline_mix = None
        self._baseline_loaded_for = None
        self._load_baseline()

    def _load_baseline(self) -> None:
        """Cold-start read of the persisted mix for the current model version (once)."""
        if self._version is None or self._baseline_loaded_for == self._version:
            return
        self._baseline_loaded_for = self._version
        try:
            row = db.get_baseline(self.source_id, BASELINE_KIND, self._version)
        except Exception as exc:  # noqa: BLE001 — degrade to in-memory capture
            log.warning("%s: baseline read failed: %s: %s", self.source_id,
                        type(exc).__name__, exc)
            return
        if row and isinstance(row.get("payload"), dict):
            self._baseline_mix = dict(row["payload"])

    def _store_baseline(self, mix: dict) -> None:
        if self._version is None:
            # contract 1.0 producer (no /model/artifact): the version is unknown, so the
            # capture stays in memory rather than persisting a row keyed "None"
            return
        try:
            db.put_baseline(self.source_id, BASELINE_KIND, self._version, mix)
        except Exception as exc:  # noqa: BLE001 — the tick still grades on the in-memory mix
            log.warning("%s: baseline write failed: %s: %s", self.source_id,
                        type(exc).__name__, exc)

    def _on_empty_window(self, res: LaneResult, t: int) -> None:
        # No recommendations were served: the Feedback lane and the mix drift are
        # reasoned-Unknown for this window (the runner excludes them from the rollup).
        res.errors["acceptance_pending"] = "empty window"
        res.errors["recommendation_drift_pending"] = "empty window"
        res.records["offer_mix"] = None
        res.records["baseline_offer_mix"] = self._baseline_mix

    def _realized_signals(self, joined: RealizedResult) -> dict[str, RealizedResult]:
        # acceptance_rate needs matched rewards with enough coverage, not both classes:
        # a window where every offer was accepted (single_class for AUC) still has a
        # perfectly good acceptance rate.
        covered = bool(joined.matched) and (joined.coverage or 0.0) >= MIN_COVERAGE
        acceptance = RealizedResult(
            float(np.mean(joined.matched)) if covered else None, joined.coverage,
            "realized" if covered else joined.status, list(joined.matched),
            None if covered else joined.reason,
            final=False if covered else joined.final)   # overdue no_labels is final for both
        return {"realized_roc_auc": joined, "acceptance_rate": acceptance}

    def _extend(self, res: LaneResult, inf: list, matched: list,
                coverage: float | None, pending: str | None, t: int) -> None:
        # --- acceptance_rate over the matched rewards subset (pending like realized) ---
        if matched and coverage is not None and coverage >= 0.5:
            res.signals["acceptance_rate"] = float(np.mean(matched))
        else:
            res.signals["acceptance_rate"] = None
            res.errors["acceptance_pending"] = pending or "no matched rewards"

        # --- recommendation_drift: current offer mix vs the captured baseline mix ---
        mix = inf[0].get("offer_mix") if inf else None
        window_version = inf[0].get("served_version") if inf else None
        if mix:
            if self._baseline_mix is None:
                self._load_baseline()        # a restart may have lost only the memory
            if self._baseline_mix is None:
                # capture ONLY from a window served by the CURRENT model version — a
                # lagging monitor replaying pre-retrain windows must not anchor the new
                # policy's baseline to the old policy's mix (review finding)
                if window_version == self._version:
                    self._baseline_mix = dict(mix)
                    self._store_baseline(self._baseline_mix)
            if self._baseline_mix is not None:
                res.signals["recommendation_drift"] = _tv_distance(mix, self._baseline_mix)
            else:
                res.signals["recommendation_drift"] = None
                res.errors["recommendation_drift_pending"] = (
                    f"awaiting a window served by model v{self._version} "
                    f"(this window is v{window_version})")
        else:
            res.signals["recommendation_drift"] = None
            res.errors["recommendation_drift"] = "no offer_mix on recommendations window"
        res.records["offer_mix"] = mix
        res.records["baseline_offer_mix"] = self._baseline_mix
