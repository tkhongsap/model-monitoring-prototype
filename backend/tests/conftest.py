"""Shared fixtures: an isolated SQLite live database and a deterministic fake producer.

`fake_producer` stands in for the three `ai-use-cases` telemetry services without any
network. It patches every by-name import of the telemetry client that the live runner,
the live ML adapter and the live explain adapter hold, and keeps the heavy engines
(Evidently, NannyML CBPE, SHAP/LIME via the optional model artifact) out of the fast
path. Tests mutate its public attributes (`latest`, `empty`, `released`, `evicted`) to
script producer behaviour tick by tick.
"""
from __future__ import annotations

import pytest

from app import config, db
from app.adapters import telemetry_http
from app.adapters.explain import live_http as explain_live_http
from app.adapters.ml_monitor import engines, live_http
from app.scenario import live_runner as live_runner_module

WINDOW_SIZE = 600
LABEL_LAG = 3


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Fresh SQLite file, strict live mode, no producer acknowledgements."""
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live.db")
    monkeypatch.setattr(config, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(config, "CONTROL_TOWER_MODE", "live")
    monkeypatch.setattr(config, "ALLOW_INSECURE_LIVE_TESTING", True)
    monkeypatch.setattr(config, "LIVE_PRODUCER_URL", "")
    db.reset_engine()
    live_runner_module.reset_live_runner()
    yield tmp_path
    live_runner_module.reset_live_runner()
    db.reset_engine()


class FakeProducer:
    """Scriptable contract v1.1 producer served from memory.

    - `latest`: `/telemetry/meta` `latest_tick` (the open window; closed ticks are below it)
    - `empty`: ticks whose inference window closes with `count=0`
    - `released`: ticks whose labels/rewards window has records
    - `evicted`: ticks whose inference window the producer no longer serves (HTTP 404)
    - `label_lag`: value advertised as `label_lag_ticks` / `reward_lag_ticks`
    - `omit_available_at`: an unreleased labels window carries no `available_at_tick`
      (a v1.0 producer, or one that leaves the field out)
    """

    def __init__(self) -> None:
        self.latest = 3
        self.label_lag = LABEL_LAG
        self.empty: set[int] = set()
        self.released: dict[int, float] = {}      # tick -> label coverage fraction
        self.evicted: set[int] = set()
        self.omit_available_at = False
        self.calls: list[tuple[str, dict | None]] = []

    # -- scripting helpers -------------------------------------------------------
    def release_labels(self, tick: int, coverage: float = 1.0) -> None:
        self.released[tick] = coverage

    def evict(self, tick: int) -> None:
        self.evicted.add(tick)

    # -- data --------------------------------------------------------------------
    @staticmethod
    def records(tick: int, kind: str = "ml") -> list[dict]:
        if kind == "nba":
            return [{
                "rec_id": f"t{tick}-r{i}",
                "features": {"a": float(i % 7), "b": float(i % 3)},
                "accept_proba": (i % 10) / 10, "served_version": 1,
                "offer_mix": {"upgrade": 0.5, "retain": 0.5},
            } for i in range(WINDOW_SIZE)]
        return [{
            "inference_id": f"t{tick}-i{i}",
            "features": {"a": float(i % 7), "b": float(i % 3)},
            "churn_proba": (i % 10) / 10,
        } for i in range(WINDOW_SIZE)]

    def labels(self, tick: int, kind: str = "ml") -> list[dict]:
        n = int(round(WINDOW_SIZE * self.released.get(tick, 1.0)))
        if kind == "nba":
            return [{"rec_id": f"t{tick}-r{i}", "accepted": int((i % 10) >= 5)}
                    for i in range(n)]
        return [{"inference_id": f"t{tick}-i{i}", "label": int((i % 10) >= 5)}
                for i in range(n)]

    @staticmethod
    def reference() -> list[dict]:
        return [{"features": {"a": float(i % 7), "b": float(i % 3)}, "label": i % 2}
                for i in range(WINDOW_SIZE)]

    def _window(self, tick: int, records: list[dict]) -> dict:
        return {
            "contract_version": "1.1", "window_id": f"w{tick}", "count": len(records),
            "records": records, "model_version": 1,
            "content_sha256": telemetry_http.canonical_records_sha256(records),
        }

    # -- the patched client surface -------------------------------------------------
    def pull(self, base_url: str, path: str, params: dict | None = None,
             timeout: float = 30.0) -> dict:
        self.calls.append((path, params))
        tick = int(params["tick"]) if params and "tick" in params else None
        if path == "/telemetry/reference":
            return {"contract_version": "1.1", "records": self.reference()}
        kind = "nba" if path in ("/telemetry/recommendations", "/telemetry/rewards") else "ml"
        if path in ("/telemetry/inferences", "/telemetry/recommendations"):
            if tick in self.evicted:
                raise telemetry_http.WindowEvicted(
                    f"{path} tick={tick} not available (404)")
            return self._window(tick, [] if tick in self.empty else self.records(tick, kind))
        if path in ("/telemetry/labels", "/telemetry/rewards"):
            if tick in self.released:
                return self._window(tick, self.labels(tick, kind))
            env = {"contract_version": "1.1", "records": [], "count": 0}
            if not self.omit_available_at:
                env["available_at_tick"] = tick + self.label_lag
            return env
        raise AssertionError(f"fake producer has no route for {path}")

    def pull_meta(self, base_url: str, timeout: float = 10.0, *, strict: bool = False) -> dict:
        return {"contract_version": "1.1", "latest_tick": self.latest,
                "label_lag_ticks": self.label_lag, "reward_lag_ticks": self.label_lag}

    @staticmethod
    def pull_model(base_url: str, path: str = "/model/artifact", timeout: float = 60.0):
        raise RuntimeError("fake producer serves no model artifact")


@pytest.fixture()
def fake_producer(monkeypatch) -> FakeProducer:
    producer = FakeProducer()
    # telemetry client: patch the module and every by-name import of it
    monkeypatch.setattr(telemetry_http, "pull", producer.pull)
    monkeypatch.setattr(telemetry_http, "pull_meta", producer.pull_meta)
    monkeypatch.setattr(telemetry_http, "pull_model", producer.pull_model)
    monkeypatch.setattr(live_http, "pull", producer.pull)
    monkeypatch.setattr(live_http, "pull_model", producer.pull_model)
    monkeypatch.setattr(explain_live_http, "pull", producer.pull)
    monkeypatch.setattr(explain_live_http, "pull_model", producer.pull_model)
    monkeypatch.setattr(live_runner_module, "pull_meta", producer.pull_meta)
    # heavy engines stay out of the fast path
    monkeypatch.setattr(engines, "evidently_drift",
                        lambda ref, cur, features=None, categorical=None: (0.0, [], "<html/>"))
    monkeypatch.setattr(engines, "cbpe_fit", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("cbpe disabled in tests")))
    monkeypatch.setattr(engines, "cbpe_estimate", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("cbpe disabled in tests")))
    return producer
