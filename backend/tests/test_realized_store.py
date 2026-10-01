"""`live_realized_metrics`: upsert with finality, latest lookup, history, pending ticks."""
from __future__ import annotations

from app import db

UC = "AICT-L01"
KEY = "realized_roc_auc"


def _payload(tick: int) -> dict:
    return {
        "use_case_id": UC, "tick": tick, "observed_tick": tick, "window_id": f"w{tick}",
        "content_sha256": f"{tick:064x}", "record_count": 600, "overall": "Green",
        "lanes": {"Quality": "Green"}, "signals": {}, "errors": {},
    }


def test_upsert_and_finality(isolated_db):
    db.put_realized_metric(UC, 3, KEY, value=None, coverage=0.1, status="insufficient_coverage",
                           reason="label coverage 10% below 50%")
    row = db.get_realized_metric(UC, 3, KEY)
    assert row["status"] == "insufficient_coverage" and row["coverage"] == 0.1
    assert row["reason"] == "label coverage 10% below 50%"
    assert db.latest_realized(UC, KEY) is None

    db.put_realized_metric(UC, 3, KEY, value=0.81, coverage=0.9, status="realized")
    db.put_realized_metric(UC, 3, KEY, value=None, coverage=None, status="evicted")
    row = db.get_realized_metric(UC, 3, KEY)
    assert row["status"] == "realized" and row["value"] == 0.81 and row["reason"] is None
    assert db.latest_realized(UC, KEY)["tick"] == 3
    assert db.realized_history(UC, KEY, 10) == [{"tick": 3, "value": 0.81, "health": "Green"}]


def test_evicted_is_final_too(isolated_db):
    db.put_realized_metric(UC, 1, KEY, value=None, coverage=None, status="evicted",
                           reason="404")
    db.put_realized_metric(UC, 1, KEY, value=0.9, coverage=1.0, status="realized")
    assert db.get_realized_metric(UC, 1, KEY)["status"] == "evicted"
    assert db.latest_realized(UC, KEY) is None
    assert db.realized_history(UC, KEY, 10) == []


def test_latest_realized_and_history_order(isolated_db):
    db.put_realized_metric(UC, 5, KEY, value=0.70, coverage=1.0, status="realized")
    db.put_realized_metric(UC, 2, KEY, value=0.85, coverage=1.0, status="realized")
    db.put_realized_metric(UC, 7, KEY, value=None, coverage=0.0, status="no_labels")
    db.put_realized_metric("AICT-L03", 9, KEY, value=0.99, coverage=1.0, status="realized")
    latest = db.latest_realized(UC, KEY)
    assert latest["tick"] == 5 and latest["value"] == 0.70
    assert db.realized_history(UC, KEY, 10) == [
        {"tick": 2, "value": 0.85, "health": "Green"},
        {"tick": 5, "value": 0.70, "health": "Red"},
    ]
    assert db.realized_history(UC, KEY, 1) == [{"tick": 5, "value": 0.70, "health": "Red"}]
    assert db.get_realized_metric(UC, 4, KEY) is None


def test_ticks_needing_realization(isolated_db):
    for t in range(5):
        db.put_live_observation(UC, _payload(t), source_tick=t, next_tick=t + 1,
                                backlog=0, state="at_tail")
    db.put_realized_metric(UC, 1, KEY, value=0.8, coverage=1.0, status="realized")
    db.put_realized_metric(UC, 2, KEY, value=None, coverage=0.0, status="no_labels")
    db.put_realized_metric(UC, 3, KEY, value=None, coverage=None, status="evicted")
    assert db.ticks_needing_realization(UC, KEY, 0, 4) == [0, 2]
    assert db.ticks_needing_realization(UC, KEY, 0, 5) == [0, 2, 4]
    assert db.ticks_needing_realization(UC, KEY, 2, 3) == [2]
    assert db.ticks_needing_realization(UC, KEY, 5, 9) == []       # never observed
    assert db.ticks_needing_realization(UC, "acceptance_rate", 0, 2) == [0, 1]


def test_get_live_observation_by_tick(isolated_db):
    db.put_live_observation(UC, _payload(4), source_tick=4, next_tick=5, backlog=0,
                            state="at_tail")
    row = db.get_live_observation_by_tick(UC, 4)
    assert row["window_id"] == "w4" and row["content_sha256"] == f"{4:064x}"
    assert row["payload"]["tick"] == 4
    assert db.get_live_observation_by_tick(UC, 3) is None


def test_clear_live_state_removes_realized_rows(isolated_db):
    db.put_realized_metric(UC, 1, KEY, value=0.8, coverage=1.0, status="realized")
    db.put_realized_metric("AICT-L03", 1, KEY, value=0.8, coverage=1.0, status="realized")
    db.clear_live_state(UC)
    assert db.get_realized_metric(UC, 1, KEY) is None
    assert db.get_realized_metric("AICT-L03", 1, KEY) is not None
    db.clear_live_state()
    assert db.get_realized_metric("AICT-L03", 1, KEY) is None


def test_migrations_four_and_five_are_recorded(isolated_db):
    bind = db.engine()
    with bind.begin() as cx:
        versions = list(cx.execute(db.select(db.schema_migrations.c.version)).scalars())
    assert versions == [1, 2, 3, 4, 5]


def test_final_flag_defaults_from_status_and_can_be_forced(isolated_db):
    db.put_realized_metric(UC, 1, KEY, value=0.8, coverage=1.0, status="realized")
    db.put_realized_metric(UC, 2, KEY, value=None, coverage=0.0, status="pending",
                           reason="label lag")
    db.put_realized_metric(UC, 3, KEY, value=None, coverage=0.0, status="no_labels",
                           reason="no labels published by tick 6 (due at tick 6)", final=True)
    assert db.get_realized_metric(UC, 1, KEY)["final"] is True
    assert db.get_realized_metric(UC, 2, KEY)["final"] is False
    assert db.get_realized_metric(UC, 3, KEY)["final"] is True
    # a forced-final no_labels row is as immutable as a realized one
    db.put_realized_metric(UC, 3, KEY, value=0.9, coverage=1.0, status="realized")
    row = db.get_realized_metric(UC, 3, KEY)
    assert row["status"] == "no_labels" and row["value"] is None and row["final"] is True
    assert db.latest_realized(UC, KEY)["tick"] == 1


def test_forced_final_no_labels_leaves_the_backfill_window(isolated_db):
    for t in range(4):
        db.put_live_observation(UC, _payload(t), source_tick=t, next_tick=t + 1,
                                backlog=0, state="at_tail")
    db.put_realized_metric(UC, 0, KEY, value=None, coverage=0.0, status="no_labels",
                           reason="overdue", final=True)
    db.put_realized_metric(UC, 1, KEY, value=None, coverage=0.0, status="no_labels",
                           reason="empty window")                       # not forced: revisited
    db.put_realized_metric(UC, 2, KEY, value=None, coverage=0.0, status="pending")
    assert db.ticks_needing_realization(UC, KEY, 0, 4) == [1, 2, 3]


def test_pending_ticks_below(isolated_db):
    db.put_realized_metric(UC, 0, KEY, value=None, coverage=0.0, status="pending")
    db.put_realized_metric(UC, 1, KEY, value=None, coverage=0.0, status="error", reason="x")
    db.put_realized_metric(UC, 2, KEY, value=None, coverage=0.0, status="pending")
    db.put_realized_metric(UC, 5, KEY, value=None, coverage=0.0, status="pending")
    db.put_realized_metric("AICT-L03", 0, KEY, value=None, coverage=0.0, status="pending")
    assert db.pending_ticks_below(UC, KEY, 4) == [0, 2]
    assert db.pending_ticks_below(UC, KEY, 0) == []


def test_migration_5_backfills_final_for_existing_rows(isolated_db):
    """A database created before migration 5 gets `final` set from the stored status."""
    bind = db.engine()
    with bind.begin() as cx:
        cx.exec_driver_sql("DROP TABLE live_realized_metrics")
        cx.exec_driver_sql("DELETE FROM schema_migrations WHERE version IN (4, 5)")
        cx.exec_driver_sql(
            "CREATE TABLE live_realized_metrics (source_id VARCHAR NOT NULL, tick INTEGER "
            "NOT NULL, metric_key VARCHAR NOT NULL, value FLOAT, coverage FLOAT, status "
            "VARCHAR NOT NULL, reason TEXT, computed_at FLOAT NOT NULL, "
            "CONSTRAINT uq_live_realized_metric UNIQUE (source_id, tick, metric_key))")
        for t, status in ((0, "realized"), (1, "evicted"), (2, "pending"), (3, "no_labels")):
            cx.exec_driver_sql(
                "INSERT INTO live_realized_metrics (source_id, tick, metric_key, status, "
                f"computed_at) VALUES ('{UC}', {t}, '{KEY}', '{status}', 0.0)")
    assert db.migrate_engine(bind) == [4, 5]
    finals = {t: db.get_realized_metric(UC, t, KEY)["final"] for t in range(4)}
    assert finals == {0: True, 1: True, 2: False, 3: False}
