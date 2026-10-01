"""NBA baseline offer mix persists across restarts (spec D.4).

`recommendation_drift` is the distance between a window's offer mix and the baseline mix
captured from the first window served by the current model version.  The baseline used
to live only in the adapter instance, so every Autoscale cold start lost it and the
signal was pending again until a fresh capture.  It now lives in `live_baselines`.
"""
from __future__ import annotations

from app import db
from app.adapters.base import TickContext
from app.adapters.ml_monitor.nba_live_http import LiveHttpNBAAdapter

UC = "AICT-L03"
MIX = {"upgrade": 0.5, "retain": 0.5}


def _adapter(monkeypatch, version: int = 1) -> LiveHttpNBAAdapter:
    """An adapter whose model version is known without a model artifact pull."""
    adapter = LiveHttpNBAAdapter("https://producer.test", lambda *args: "artifact")
    monkeypatch.setattr(adapter, "_ensure_baseline", lambda: None)
    adapter._version = version
    return adapter


def test_capture_persists_the_baseline(isolated_db, fake_producer, monkeypatch):
    adapter = _adapter(monkeypatch)
    res = adapter.monitor(UC, TickContext(tick=0, seed=42))
    assert "recommendation_drift_pending" not in res.errors
    assert res.signals["recommendation_drift"] == 0.0
    assert res.records["baseline_offer_mix"] == MIX
    stored = db.get_baseline(UC, "offer_mix", "1")
    assert stored is not None and stored["payload"] == MIX
    assert stored["captured_at"] > 0


def test_baseline_survives_a_new_adapter_instance(isolated_db, fake_producer, monkeypatch):
    first = _adapter(monkeypatch)
    first.monitor(UC, TickContext(tick=0, seed=42))

    # the policy shifted: the next window's mix differs from the captured baseline
    shifted = {"upgrade": 0.9, "retain": 0.1}
    base_records = fake_producer.records
    fake_producer.records = lambda tick, kind="ml": [
        {**r, "offer_mix": dict(shifted)} if kind == "nba" else r
        for r in base_records(tick, kind)]

    second = _adapter(monkeypatch)          # a cold start: fresh process, same database
    assert second._baseline_mix is None
    res = second.monitor(UC, TickContext(tick=1, seed=42))
    assert "recommendation_drift_pending" not in res.errors
    # measured against the STORED baseline, not re-captured from the shifted window
    assert res.signals["recommendation_drift"] == 0.4
    assert res.records["baseline_offer_mix"] == MIX
    assert res.records["offer_mix"] == shifted
    assert second._baseline_mix == MIX


def test_baseline_is_keyed_by_model_version(isolated_db, fake_producer, monkeypatch):
    first = _adapter(monkeypatch, version=1)
    first.monitor(UC, TickContext(tick=0, seed=42))

    # the model was retrained: version 2 has no stored baseline and the windows are
    # still served by version 1, so the signal is pending until a v2 window arrives
    retrained = _adapter(monkeypatch, version=2)
    res = retrained.monitor(UC, TickContext(tick=1, seed=42))
    assert res.signals["recommendation_drift"] is None
    assert "awaiting a window served by model v2" in res.errors["recommendation_drift_pending"]
    assert db.get_baseline(UC, "offer_mix", "2") is None
    assert db.get_baseline(UC, "offer_mix", "1")["payload"] == MIX   # untouched


def test_rebaseline_reloads_the_stored_mix_for_the_new_version(isolated_db, monkeypatch):
    db.put_baseline(UC, "offer_mix", "2", {"upgrade": 0.2, "retain": 0.8})
    adapter = _adapter(monkeypatch, version=1)
    adapter._baseline_mix = dict(MIX)
    adapter._version = 2
    adapter._on_rebaseline()
    assert adapter._baseline_mix == {"upgrade": 0.2, "retain": 0.8}
    adapter._version = 3
    adapter._on_rebaseline()
    assert adapter._baseline_mix is None


def test_put_baseline_replaces_and_clear_live_state_drops(isolated_db):
    db.put_baseline(UC, "offer_mix", "1", {"a": 1.0})
    db.put_baseline(UC, "offer_mix", "1", {"a": 0.4, "b": 0.6})
    assert db.get_baseline(UC, "offer_mix", "1")["payload"] == {"a": 0.4, "b": 0.6}
    db.put_baseline("AICT-L01", "offer_mix", "1", {"x": 1.0})
    db.clear_live_state(UC)
    assert db.get_baseline(UC, "offer_mix", "1") is None
    assert db.get_baseline("AICT-L01", "offer_mix", "1")["payload"] == {"x": 1.0}
    db.clear_live_state()
    assert db.get_baseline("AICT-L01", "offer_mix", "1") is None


def test_migration_seven_is_recorded(isolated_db):
    bind = db.engine()
    with bind.begin() as cx:
        versions = list(cx.execute(db.select(db.schema_migrations.c.version)).scalars())
    assert versions == [1, 2, 3, 4, 5, 6, 7]


def test_unversioned_capture_stays_in_memory(isolated_db, fake_producer, monkeypatch):
    """A contract 1.0 producer serves no /model/artifact, so `_version` stays None; the
    in-memory capture still grades the tick but no row keyed "None" is persisted."""
    adapter = _adapter(monkeypatch, version=None)
    base_records = fake_producer.records
    fake_producer.records = lambda tick, kind="ml": [
        {k: v for k, v in r.items() if k != "served_version"} if kind == "nba" else r
        for r in base_records(tick, kind)]
    res = adapter.monitor(UC, TickContext(tick=0, seed=42))
    assert res.signals["recommendation_drift"] == 0.0
    assert adapter._baseline_mix == MIX
    assert db.get_baseline(UC, "offer_mix", None) is None
    assert db.get_baseline(UC, "offer_mix", "None") is None
