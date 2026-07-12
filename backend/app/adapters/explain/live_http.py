"""LiveHttpExplainAdapter — the LIVE counterpart of `LimeShapAdapter`.

Pulls the external model app's serialized model + reference/current windows and runs
LIME (per-instance, highest-risk row) + SHAP (global importance) on the REAL fitted
model. Feature order/categoricals come from the artifact meta headers (the model
service OWNS its feature list, contract v1.0), falling back to sorted record keys and
finally the churn lists. Self-contained on purpose: it does NOT touch the
determinism-critical seeded LimeShapAdapter (that class's golden bake stays
byte-identical). SHAP importance is cached per pulled model version; every engine call
degrades gracefully.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from ... import config
from ...datagen import churn
from ..base import ExplainResult, TickContext
from ..telemetry_http import pull, pull_model


class LiveHttpExplainAdapter:
    name = "live_http"

    def __init__(self, base_url: str, artifact_writer, model_name: str = "telco-churn",
                 seed: int = 42, class_names: list[str] | None = None,
                 inferences_path: str = "/telemetry/inferences") -> None:
        self.base_url = base_url.rstrip("/")
        self.write_artifact = artifact_writer
        self.model_name = model_name
        self.seed = seed
        self.class_names = class_names or ["stay", "churn"]
        self.inferences_path = inferences_path
        self._model = None
        self._version: int | None = None
        self._feature_order: list[str] | None = None   # model-service-owned (artifact meta)
        self._categorical: list[str] | None = None
        self._reference: pd.DataFrame | None = None    # reference FEATURE frame
        self._shap_png_by_version: dict[int, bytes] = {}

    def _ensure_model(self) -> None:
        model, version, meta = pull_model(self.base_url)
        if meta.get("feature_order"):
            self._feature_order = meta["feature_order"]
        if meta.get("categorical"):
            self._categorical = meta["categorical"]
        if version == self._version and self._model is not None:
            return
        ref = pull(self.base_url, "/telemetry/reference")["records"]
        # feature order: artifact meta -> sorted record keys -> churn last resort
        if not self._feature_order:
            self._feature_order = (sorted(ref[0]["features"].keys()) if ref
                                   else list(churn.FEATURES))
        if self._categorical is None:
            self._categorical = [c for c in churn.CATEGORICAL if c in self._feature_order]
        self._reference = pd.DataFrame([r["features"] for r in ref])[self._feature_order]
        # commit the version LAST — a partial failure above must retry next tick, not
        # leave a half-initialized adapter accepted as current (review finding)
        self._model, self._version = model, version

    def _shap_png(self, version: int) -> bytes | None:
        if version in self._shap_png_by_version:
            return self._shap_png_by_version[version]
        try:
            import matplotlib
            matplotlib.use("Agg")  # headless
            import matplotlib.pyplot as plt
            import shap
            sample = self._reference.sample(
                min(500, len(self._reference)), random_state=0).to_numpy(float)
            explainer = shap.TreeExplainer(self._model)  # the apps serve tree ensembles
            values = explainer.shap_values(sample)
            vals = values[1] if isinstance(values, list) else values
            if getattr(vals, "ndim", 2) == 3:            # (n, features, classes) on newer shap
                vals = vals[:, :, 1]
            shap.summary_plot(vals, sample, feature_names=self._feature_order,
                              show=False, plot_type="bar")
            buf = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format="png", dpi=110, bbox_inches="tight")
            plt.close("all")
            png = buf.getvalue()
            self._shap_png_by_version[version] = png
            return png
        except Exception:  # noqa: BLE001
            return None

    def explain(self, use_case_id: str, tick: TickContext) -> ExplainResult:
        res = ExplainResult()
        t = tick.tick
        try:
            self._ensure_model()
        except Exception as e:  # noqa: BLE001
            res.errors["explain"] = f"{type(e).__name__}: {e}"
            return res

        # the telemetry pull + frame build must degrade like everything else — a 404
        # (unknown tick), transient network failure, or schema-skewed record must never
        # escape and 500 the whole live tick (review finding)
        try:
            inf = pull(self.base_url, self.inferences_path, {"tick": t})["records"]
            if not inf:
                res.errors["explain"] = "no inferences for tick"
                return res
            order = self._feature_order
            if not order:
                res.errors["explain"] = "feature order unavailable"
                return res
            cur_feat = pd.DataFrame([r["features"] for r in inf])[order]
            proba_field = next((f for f in ("churn_proba", "accept_proba") if f in inf[0]), "churn_proba")
            proba = np.asarray([float(r[proba_field]) for r in inf])
        except Exception as e:  # noqa: BLE001 — degrade, never crash the tick
            res.errors["explain"] = f"{type(e).__name__}: {e}"
            return res

        # LIME HTML embeds the selected row's feature values.  Keep it available for
        # local developer analysis, but never persist or expose it in the public strict
        # live plane. SHAP below is aggregate, global feature importance.
        try:
            if config.strict_live_mode():
                raise PermissionError("per-instance LIME is redacted in strict live mode")
            from lime.lime_tabular import LimeTabularExplainer
            idx = int(np.argmax(proba))
            cat_idx = [order.index(c) for c in (self._categorical or []) if c in order]
            explainer = LimeTabularExplainer(
                training_data=self._reference.to_numpy(float),
                feature_names=order, class_names=self.class_names,
                categorical_features=cat_idx, discretize_continuous=True,
                mode="classification", random_state=self.seed)
            exp = explainer.explain_instance(
                cur_feat.to_numpy(float)[idx], self._model.predict_proba, num_features=6)
            res.lime_top = [[name, float(w)] for name, w in exp.as_list()]
            res.instance = {"index": idx, "churn_probability": float(proba[idx])}
            res.artifacts["lime_html"] = self.write_artifact("lime_html", t, exp.as_html(), "html")
        except PermissionError:
            res.errors["lime"] = "redacted: per-instance feature values are not public"
        except Exception as e:  # noqa: BLE001
            res.errors["lime"] = f"{type(e).__name__}: {e}"

        # --- SHAP global importance (per model version) ---
        png = self._shap_png(self._version)
        if png is not None:
            res.artifacts["shap_png"] = self.write_artifact("shap_png", t, png, "png")
        else:
            res.errors.setdefault("shap", "shap unavailable or failed")
        return res
