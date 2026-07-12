"""LiveHttpMLAdapter — the LIVE counterpart of `EvidentlyNannyMLAdapter`.

Instead of generating synthetic data internally, it PULLS raw telemetry from an
external model app over HTTP and runs the monitor's OWN engines on it:
  - reference + current FEATURE windows  -> Evidently drift  -> data_drift_share
  - reference scored by the pulled model -> NannyML CBPE fit  -> estimated_roc_auc
  - current proba + lagged labels         -> roc_auc_score    -> realized_roc_auc

Contract v1.0 posture:
  - feature order/categoricals are OWNED by the model service (artifact headers);
    fallbacks: sorted record keys, then the churn lists as an absolute last resort.
  - drift does NOT depend on the OPTIONAL /model/artifact: reference is pulled and
    Evidently runs even when the artifact fails; that failure degrades only the
    label-free CBPE estimate.
  - labels are partial-by-design: realized AUC is computed on the matched subset when
    coverage >= 50% and both classes are present (coverage is reported in records).

The class is parameterized (paths/field names, churn defaults) so LiveHttpNBAAdapter
subclasses it. ALL monitoring intelligence stays in the monitor; the model app only
emits raw data + its serialized model. Every engine call degrades to None (Unknown)
on failure, never crashing the tick — matching the seeded adapter's contract.
"""
from __future__ import annotations

import pandas as pd
from sklearn.metrics import roc_auc_score

from ...datagen import churn
from ..base import LaneResult, TickContext
from ..telemetry_http import pull, pull_model, window_metadata
from . import engines


class LiveHttpMLAdapter:
    name = "live_http"

    # last-resort feature lists (subclasses without a churn heritage clear these)
    _fallback_features: list[str] | None = churn.FEATURES
    _fallback_categorical: list[str] | None = churn.CATEGORICAL
    _label_field = "label"                  # the labels-window truth field
    _signal_keys = ("data_drift_share", "estimated_roc_auc", "realized_roc_auc")

    def __init__(self, base_url: str, artifact_writer, chunk_size: int = 500,
                 model_name: str = "telco-churn",
                 inferences_path: str = "/telemetry/inferences",
                 labels_path: str = "/telemetry/labels",
                 proba_field: str = "churn_proba",
                 id_field: str = "inference_id") -> None:
        self.base_url = base_url.rstrip("/")
        self.write_artifact = artifact_writer
        self.chunk_size = chunk_size
        self.model_name = model_name
        self.inferences_path = inferences_path
        self.labels_path = labels_path
        self.proba_field = proba_field
        self.id_field = id_field
        self._version: int | None = None
        self._cbpe = None                       # fitted estimator or remembered Exception
        self._feature_order: list[str] | None = None    # model-service-owned (artifact meta)
        self._categorical: list[str] | None = None
        self._reference_records: list | None = None     # raw reference records, cached
        self._reference_auc: float | None = None

    # -- feature ownership: artifact meta -> sorted record keys -> churn last resort --
    def _resolve_order(self, records: list) -> list[str]:
        if self._feature_order:
            return self._feature_order
        if records:
            return sorted(records[0]["features"].keys())
        return list(self._fallback_features or [])

    def _resolve_categorical(self, order: list[str]) -> list[str]:
        cats = self._categorical if self._categorical is not None else (self._fallback_categorical or [])
        return [c for c in cats if c in order]

    def _get_reference(self) -> list:
        """Pull + cache /telemetry/reference — independent of the OPTIONAL artifact,
        so drift keeps working when the artifact endpoint is down."""
        if self._reference_records is None:
            self._reference_records = pull(self.base_url, "/telemetry/reference")["records"]
        return self._reference_records

    def _on_rebaseline(self) -> None:
        """Hook for subclass state tied to a model version (NBA baseline offer mix)."""

    # -- refit the CBPE baseline whenever the app's served model version changes --
    def _ensure_baseline(self) -> None:
        model, version, meta = pull_model(self.base_url)
        if meta.get("feature_order"):
            self._feature_order = meta["feature_order"]
        if meta.get("categorical"):
            self._categorical = meta["categorical"]
        if version == self._version and self._cbpe is not None:
            return
        # (re)baseline: do ALL fallible work first and commit the state LAST — a transient
        # failure mid-rebaseline must retry next tick, not leave the previous version's
        # CBPE/reference silently accepted as current (review finding)
        ref = pull(self.base_url, "/telemetry/reference")["records"]   # fresh, bypass cache
        order = self._resolve_order(ref)
        ref_feat = pd.DataFrame([r["features"] for r in ref])[order]
        # the shared ReferenceRow truth field is 'label' for EVERY lane; _label_field only
        # names the labels/rewards-window field (LabelRecord.label vs Reward.accepted)
        ref_labels = [int(r["label"]) for r in ref]
        proba = model.predict_proba(ref_feat.to_numpy(float))[:, 1]
        reference_auc = float(roc_auc_score(ref_labels, proba))
        try:
            cbpe = engines.cbpe_fit(
                engines.build_scored_frame(ref_feat, proba, ref_labels, feature_order=order),
                self.chunk_size)
        except Exception as e:  # noqa: BLE001 — remember; degrade estimated signal
            cbpe = e
        # atomic commit
        self._reference_records = ref
        self._reference_auc = reference_auc
        self._cbpe = cbpe
        self._version = version
        self._on_rebaseline()

    def _extend(self, res: LaneResult, inf: list, matched: list,
                coverage: float | None, pending: str | None, t: int) -> None:
        """Hook for subclass signals over the same pulled windows (NBA adds two)."""

    def monitor(self, use_case_id: str, tick: TickContext) -> LaneResult:
        res = LaneResult()
        t = tick.tick

        # --- OPTIONAL artifact: failure degrades ONLY the CBPE estimate, never drift ---
        artifact_err = None
        try:
            self._ensure_baseline()
        except Exception as e:  # noqa: BLE001 — /model/artifact is optional (contract 1.0)
            artifact_err = f"{type(e).__name__}: {e}"
            res.errors["artifact"] = artifact_err

        # --- REQUIRED current window: without it nothing can be computed. The frame
        # build sits inside the same degrade envelope: an empty (count=0) window or a
        # schema-skewed record must never escape and crash the tick (review finding) ---
        try:
            inference_env = pull(self.base_url, self.inferences_path, {"tick": t})
            inf = inference_env["records"]
            res.metadata.update(window_metadata(inference_env, t))
            if not inf:
                for k in self._signal_keys:
                    res.signals[k] = None
                res.errors["telemetry"] = "empty window (count=0)"
                return res
            order = self._resolve_order(inf)
            cur_feat = pd.DataFrame([r["features"] for r in inf])[order]
            cur_proba = [float(r[self.proba_field]) for r in inf]
        except Exception as e:  # noqa: BLE001 — telemetry unreachable / unknown tick / bad schema
            for k in self._signal_keys:
                res.signals[k] = None
            res.errors["telemetry"] = f"{type(e).__name__}: {e}"
            return res

        # A five-minute timeout may intentionally close an undersized window.  It is a
        # real observation and must advance the durable cursor, but statistical metrics
        # are not meaningful below the contract's 500-record ML window size.
        if len(inf) < self.chunk_size:
            for key in self._signal_keys:
                res.signals[key] = None
            res.records = {
                "drifted_features": [], "reference_auc": self._reference_auc,
                "model_version": self._version,
                "realized_pending_reason": (
                    f"insufficient sample: {len(inf)} of {self.chunk_size} records"),
                "realized_label_coverage": None, "realized_window_tick": t,
            }
            res.errors["insufficient_sample"] = (
                f"requires {self.chunk_size} records; observed {len(inf)}")
            return res

        # --- Evidently drift (reference pull is independent of the artifact) ---
        drifted: list[str] = []
        try:
            ref = self._get_reference()
            ref_feat = pd.DataFrame([r["features"] for r in ref])[order]
            share, drifted, html = engines.evidently_drift(
                ref_feat, cur_feat, features=order,
                categorical=self._resolve_categorical(order))
            res.signals["data_drift_share"] = share
            res.artifacts["evidently_html"] = self.write_artifact("evidently_html", t, html, "html")
        except Exception as e:  # noqa: BLE001
            res.signals["data_drift_share"] = None
            res.errors["evidently"] = f"{type(e).__name__}: {e}"

        # --- NannyML CBPE estimated ROC-AUC (label-free; needs the pulled model) ---
        try:
            if artifact_err:
                raise RuntimeError(f"model artifact unavailable: {artifact_err}")
            if isinstance(self._cbpe, Exception):
                raise self._cbpe
            res.signals["estimated_roc_auc"] = engines.cbpe_estimate(
                self._cbpe, engines.build_scored_frame(cur_feat, cur_proba, feature_order=order))
        except Exception as e:  # noqa: BLE001
            res.signals["estimated_roc_auc"] = None
            res.errors["nannyml"] = f"{type(e).__name__}: {e}"

        # --- realized ROC-AUC on the PARTIAL matched subset (labels lag by design) ---
        realized, pending, coverage, matched = None, None, None, []
        try:
            env = pull(self.base_url, self.labels_path, {"tick": t})
            labels = env.get("records", [])
            if env.get("available_at_tick") is not None:
                pending = "label lag"           # window not yet released by the app
            elif not labels:
                pending = "label lag"           # nothing arrived yet
            else:
                lab = {label[self.id_field]: int(label[self._label_field]) for label in labels}
                pairs = [(lab[r[self.id_field]], p) for r, p in zip(inf, cur_proba)
                         if r[self.id_field] in lab]
                matched = [y for y, _ in pairs]
                coverage = len(pairs) / len(inf) if inf else 0.0
                if coverage < 0.5:
                    pending = "label coverage below 50%"
                elif len(set(matched)) < 2:
                    pending = "single class in matched labels"
                else:
                    realized = float(roc_auc_score(matched, [p for _, p in pairs]))
        except Exception as e:  # noqa: BLE001
            pending = f"labels pull failed: {type(e).__name__}: {e}"
        res.signals["realized_roc_auc"] = realized
        if pending:
            res.errors["realized_pending"] = pending

        res.records = {"drifted_features": drifted, "reference_auc": self._reference_auc,
                       "model_version": self._version, "realized_pending_reason": pending,
                       "realized_label_coverage": coverage, "realized_window_tick": t}
        self._extend(res, inf, matched, coverage, pending, t)
        return res
