"""SQLite persistence (SQLAlchemy Core, portable SQL only — ADR-1).

Tables: registry (seed rows) · baked_ticks (full per-tick state snapshots, JSON) ·
traces + scores (Langfuse-stub trace store) · artifact_map (artifact_id → file) ·
scenario_state (the player pointer) · bake_manifest (determinism manifest, §A.6).

Bake-mode design: each baked tick stores the COMPLETE state at that tick (signals,
lane healths, actions, alerts, events, summary) so advancing/jumping is one SELECT.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

from sqlalchemy import (
    Column, Float, Integer, LargeBinary, MetaData, String, Table, Text, UniqueConstraint,
    create_engine, delete, insert, inspect, or_, select, update,
)
from sqlalchemy.exc import IntegrityError

from . import config

metadata = MetaData()

schema_migrations = Table(
    "schema_migrations", metadata,
    Column("version", Integer, primary_key=True),
    Column("name", String, nullable=False),
    Column("applied_at", Float, nullable=False),
)

registry = Table(
    "registry", metadata,
    Column("registry_id", String, primary_key=True),
    Column("payload", Text, nullable=False),  # full Appendix-B row as JSON
)

baked_ticks = Table(
    "baked_ticks", metadata,
    Column("scenario_id", String, primary_key=True),
    Column("tick", Integer, primary_key=True),
    Column("payload", Text, nullable=False),  # complete tick state as JSON
)

traces = Table(
    "traces", metadata,
    Column("trace_id", String, primary_key=True),
    Column("scenario_id", String),
    Column("tick", Integer),
    Column("use_case_id", String),
    Column("name", String),
    Column("input", Text),
    Column("output", Text),
    Column("metadata_json", Text),
)

scores = Table(
    "scores", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String),
    Column("name", String),
    Column("value", Float),
)

artifact_map = Table(
    "artifact_map", metadata,
    Column("artifact_id", String, primary_key=True),
    Column("scenario_id", String),
    Column("tick", Integer),
    Column("kind", String),        # evidently_html | lime_html | shap_png
    Column("filename", String),    # relative to ARTIFACTS_DIR
    Column("content_type", String),
)

# Live artifacts must survive Autoscale instance replacement and be readable from any
# instance.  `artifact_map.filename` remains for deterministic demo/developer bakes;
# strict-live responses are served from this monitor-owned database blob table.
live_artifact_blobs = Table(
    "live_artifact_blobs", metadata,
    Column("artifact_id", String, primary_key=True),
    Column("content_type", String, nullable=False),
    Column("content", LargeBinary, nullable=False),
    Column("content_sha256", String, nullable=False),
    Column("created_at", Float, nullable=False),
)

scenario_state = Table(
    "scenario_state", metadata,
    Column("id", Integer, primary_key=True),  # single row, id=1
    Column("scenario_id", String),
    Column("tick", Integer),
    Column("playing", Integer),
    Column("speed", Integer),
    Column("mode", String),
    Column("seed", Integer),
)

bake_manifest = Table(
    "bake_manifest", metadata,
    Column("scenario_id", String, primary_key=True),
    Column("manifest", Text),
)

# ---------------------------------------------------------------- live control-tower plane
# These tables deliberately do not reference the baked/demo tables.  They are portable
# SQLAlchemy Core definitions and run unchanged on SQLite (tests/dev) and managed
# PostgreSQL (Autoscale production).

live_source_cursors = Table(
    "live_source_cursors", metadata,
    Column("source_id", String, primary_key=True),
    Column("next_tick", Integer, nullable=False, default=0),
    Column("source_tick", Integer),
    Column("observed_tick", Integer),
    Column("state", String, nullable=False, default="connecting"),
    Column("backlog", Integer, nullable=False, default=0),
    Column("last_checked_at", Float),
    Column("last_success_at", Float),
    Column("last_error_at", Float),
    Column("last_error", Text),
    Column("updated_at", Float, nullable=False),
)

live_observations = Table(
    "live_observations", metadata,
    Column("observation_id", String, primary_key=True),
    Column("source_id", String, nullable=False, index=True),
    Column("window_id", String, nullable=False),
    Column("batch_id", String, index=True),
    Column("source_instance_id", String),
    Column("source_tick", Integer),
    Column("observed_tick", Integer, nullable=False),
    Column("opened_at", String),
    Column("closed_at", String),
    Column("content_sha256", String, nullable=False),
    Column("first_record_id", String),
    Column("last_record_id", String),
    Column("model_version", String),
    Column("provenance_counts", Text, nullable=False, default="{}"),
    Column("record_count", Integer, nullable=False, default=0),
    Column("payload", Text, nullable=False),
    Column("observed_at", Float, nullable=False),
    Column("source_lag_ms", Float),
    Column("ack_status", String, nullable=False, default="pending"),
    Column("ack_error", Text),
    Column("acknowledged_at", Float),
    UniqueConstraint("source_id", "window_id", name="uq_live_observation_window"),
)

live_signal_history = Table(
    "live_signal_history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False, index=True),
    Column("observation_id", String, nullable=False),
    Column("tick", Integer, nullable=False),
    Column("signal_key", String, nullable=False),
    Column("value", Float),
    Column("health", String, nullable=False),
    UniqueConstraint("observation_id", "signal_key", name="uq_live_signal_observation"),
)

live_worker_leases = Table(
    "live_worker_leases", metadata,
    Column("lease_name", String, primary_key=True),
    Column("owner_id", String, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("renewed_at", Float, nullable=False),
)

_engine = None
_migration_lock = threading.RLock()


def migrate_engine(bind) -> list[int]:
    """Apply ordered idempotent migrations under a cross-instance Postgres lock."""
    def add_live_batch_id(cx) -> None:
        columns = {column["name"] for column in inspect(cx).get_columns("live_observations")}
        if "batch_id" not in columns:
            cx.exec_driver_sql("ALTER TABLE live_observations ADD COLUMN batch_id VARCHAR")
        cx.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_live_observations_batch_id "
            "ON live_observations(batch_id)")

    migrations = [
        (1, "initial monitor schema", lambda cx: metadata.create_all(bind=cx)),
        (2, "durable live artifact blobs",
         lambda cx: live_artifact_blobs.create(bind=cx, checkfirst=True)),
        (3, "scheduled POC batch correlation", add_live_batch_id),
    ]
    applied_now: list[int] = []
    # The local lock also makes SQLite thread-contention tests deterministic. Managed
    # PostgreSQL additionally serializes separate Autoscale processes with an advisory
    # transaction lock for the complete migration transaction.
    with _migration_lock, bind.begin() as cx:
        if bind.dialect.name == "postgresql":
            cx.exec_driver_sql("SELECT pg_advisory_xact_lock(731104221991)")
        schema_migrations.create(bind=cx, checkfirst=True)
        applied = set(cx.execute(select(schema_migrations.c.version)).scalars())
        for version, name, apply in migrations:
            if version in applied:
                continue
            apply(cx)
            cx.execute(insert(schema_migrations), {
                "version": version, "name": name, "applied_at": time.time()})
            applied_now.append(version)
    return applied_now


def engine():
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        if not url:
            config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{config.DB_PATH}"
        # Replit/managed providers commonly emit postgres:// or postgresql://.  Select
        # psycopg v3 explicitly so deployments do not depend on legacy psycopg2.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite:"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        migrate_engine(_engine)
    return _engine


def reset_engine() -> None:
    """Testing hook: drop the cached engine (e.g., after swapping DB path)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


# ---------------------------------------------------------------- helpers

def put_registry_rows(rows: list[dict]) -> None:
    with engine().begin() as cx:
        cx.execute(delete(registry))
        cx.execute(insert(registry), [
            {"registry_id": r["registry_id"], "payload": json.dumps(r)} for r in rows
        ])


def get_registry_rows() -> list[dict]:
    with engine().begin() as cx:
        rows = cx.execute(select(registry.c.payload).order_by(registry.c.registry_id)).fetchall()
    return [json.loads(r[0]) for r in rows]


def put_baked_tick(scenario_id: str, tick: int, payload: dict) -> None:
    with engine().begin() as cx:
        cx.execute(delete(baked_ticks).where(
            (baked_ticks.c.scenario_id == scenario_id) & (baked_ticks.c.tick == tick)))
        cx.execute(insert(baked_ticks), {
            "scenario_id": scenario_id, "tick": tick, "payload": json.dumps(payload)})


def get_baked_tick(scenario_id: str, tick: int) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(baked_ticks.c.payload).where(
            (baked_ticks.c.scenario_id == scenario_id) & (baked_ticks.c.tick == tick))).fetchone()
    return json.loads(row[0]) if row else None


def baked_tick_count(scenario_id: str) -> int:
    with engine().begin() as cx:
        rows = cx.execute(select(baked_ticks.c.tick).where(
            baked_ticks.c.scenario_id == scenario_id)).fetchall()
    return len(rows)


def clear_bake(scenario_id: str) -> None:
    with engine().begin() as cx:
        cx.execute(delete(baked_ticks).where(baked_ticks.c.scenario_id == scenario_id))
        cx.execute(delete(traces).where(traces.c.scenario_id == scenario_id))
        cx.execute(delete(artifact_map).where(artifact_map.c.scenario_id == scenario_id))
        cx.execute(delete(bake_manifest).where(bake_manifest.c.scenario_id == scenario_id))


def add_trace(row: dict, score_rows: list[dict]) -> None:
    with engine().begin() as cx:
        cx.execute(insert(traces), row)
        if score_rows:
            cx.execute(insert(scores), score_rows)


def get_traces(scenario_id: str, tick: int, use_case_id: str, limit: int = 40) -> list[dict]:
    with engine().begin() as cx:
        rows = cx.execute(
            select(traces).where(
                (traces.c.scenario_id == scenario_id) & (traces.c.tick == tick)
                & (traces.c.use_case_id == use_case_id)).limit(limit)).mappings().all()
        out = []
        for r in rows:
            srows = cx.execute(select(scores.c.name, scores.c.value).where(
                scores.c.trace_id == r["trace_id"])).fetchall()
            out.append({
                "trace_id": r["trace_id"], "name": r["name"], "input": r["input"],
                "output": r["output"], "metadata": json.loads(r["metadata_json"] or "{}"),
                "scores": {n: v for n, v in srows},
            })
    return out


def put_artifact(artifact_id: str, scenario_id: str, tick: int, kind: str,
                 filename: str, content_type: str) -> None:
    with engine().begin() as cx:
        cx.execute(delete(artifact_map).where(artifact_map.c.artifact_id == artifact_id))
        cx.execute(insert(artifact_map), {
            "artifact_id": artifact_id, "scenario_id": scenario_id, "tick": tick,
            "kind": kind, "filename": filename, "content_type": content_type})


def get_artifact(artifact_id: str) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(artifact_map).where(
            artifact_map.c.artifact_id == artifact_id)).mappings().fetchone()
    return dict(row) if row else None


def put_live_artifact_blob(artifact_id: str, content_type: str, content: bytes) -> None:
    """Persist a redacted/live artifact independently of an instance filesystem."""
    row = {
        "artifact_id": artifact_id,
        "content_type": content_type,
        "content": bytes(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "created_at": time.time(),
    }
    with engine().begin() as cx:
        cx.execute(delete(live_artifact_blobs).where(
            live_artifact_blobs.c.artifact_id == artifact_id))
        cx.execute(insert(live_artifact_blobs), row)


def get_live_artifact_blob(artifact_id: str) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(live_artifact_blobs).where(
            live_artifact_blobs.c.artifact_id == artifact_id)).mappings().fetchone()
    return dict(row) if row else None


def get_state() -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(scenario_state).where(scenario_state.c.id == 1)).mappings().fetchone()
    return dict(row) if row else None


def put_state(**kw: Any) -> None:
    with engine().begin() as cx:
        if cx.execute(select(scenario_state.c.id).where(scenario_state.c.id == 1)).fetchone():
            cx.execute(update(scenario_state).where(scenario_state.c.id == 1).values(**kw))
        else:
            cx.execute(insert(scenario_state), {"id": 1, **kw})


def put_manifest(scenario_id: str, manifest: dict) -> None:
    with engine().begin() as cx:
        cx.execute(delete(bake_manifest).where(bake_manifest.c.scenario_id == scenario_id))
        cx.execute(insert(bake_manifest), {"scenario_id": scenario_id, "manifest": json.dumps(manifest)})


def get_manifest(scenario_id: str) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(bake_manifest.c.manifest).where(
            bake_manifest.c.scenario_id == scenario_id)).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------- live persistence helpers

class WindowDigestMismatch(RuntimeError):
    """A producer rewrote a previously observed immutable window."""


def _source_defaults(source_id: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "source_id": source_id, "next_tick": 0, "source_tick": None,
        "observed_tick": None, "state": "connecting", "backlog": 0,
        "last_checked_at": None, "last_success_at": None,
        "last_error_at": None, "last_error": None, "updated_at": now,
    }


def ensure_live_source(source_id: str) -> None:
    """Create a cursor row once; tolerate another Autoscale instance winning."""
    with engine().begin() as cx:
        exists = cx.execute(select(live_source_cursors.c.source_id).where(
            live_source_cursors.c.source_id == source_id)).fetchone()
        if exists:
            return
        try:
            with cx.begin_nested():
                cx.execute(insert(live_source_cursors), _source_defaults(source_id))
        except IntegrityError:
            pass


def get_live_source(source_id: str) -> dict:
    ensure_live_source(source_id)
    with engine().begin() as cx:
        row = cx.execute(select(live_source_cursors).where(
            live_source_cursors.c.source_id == source_id)).mappings().one()
    return dict(row)


def list_live_sources() -> list[dict]:
    with engine().begin() as cx:
        rows = cx.execute(select(live_source_cursors).order_by(
            live_source_cursors.c.source_id)).mappings().all()
    return [dict(r) for r in rows]


def mark_live_contact(source_id: str, source_tick: int | None, backlog: int,
                      state: str) -> dict:
    ensure_live_source(source_id)
    now = time.time()
    with engine().begin() as cx:
        cx.execute(update(live_source_cursors).where(
            live_source_cursors.c.source_id == source_id).values(
                source_tick=source_tick, backlog=max(0, int(backlog)), state=state,
                last_checked_at=now, last_error=None, last_error_at=None, updated_at=now))
    return get_live_source(source_id)


def mark_live_error(source_id: str, error: str) -> dict:
    ensure_live_source(source_id)
    now = time.time()
    with engine().begin() as cx:
        cx.execute(update(live_source_cursors).where(
            live_source_cursors.c.source_id == source_id).values(
                state="error", last_error=error, last_error_at=now, updated_at=now))
    return get_live_source(source_id)


def _fallback_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def put_live_observation(source_id: str, payload: dict, *, source_tick: int | None,
                         next_tick: int, backlog: int, state: str) -> tuple[str, bool]:
    """Atomically deduplicate/persist one graded producer window and advance its cursor.

    `window_id` is immutable within a source.  Seeing the same id with another digest is
    an integrity error and the cursor is deliberately held for operator investigation.
    """
    ensure_live_source(source_id)
    now = time.time()
    observed_tick = int(payload.get("observed_tick", payload.get("tick", next_tick - 1)))
    window_id = str(payload.get("window_id") or f"{source_id}:t{observed_tick}")
    digest = str(payload.get("content_sha256") or _fallback_digest(payload))
    observation_id = str(payload.get("observation_id") or uuid.uuid5(
        uuid.NAMESPACE_URL, f"{source_id}/{window_id}/{digest}"))
    payload = {**payload, "window_id": window_id, "content_sha256": digest,
               "observation_id": observation_id}
    row = {
        "observation_id": observation_id,
        "source_id": source_id,
        "window_id": window_id,
        "batch_id": payload.get("batch_id"),
        "source_instance_id": payload.get("source_instance_id"),
        "source_tick": source_tick,
        "observed_tick": observed_tick,
        "opened_at": payload.get("opened_at"),
        "closed_at": payload.get("closed_at"),
        "content_sha256": digest,
        "first_record_id": payload.get("first_record_id"),
        "last_record_id": payload.get("last_record_id"),
        "model_version": None if payload.get("model_version") is None else str(payload["model_version"]),
        "provenance_counts": json.dumps(payload.get("provenance_counts") or {}, sort_keys=True),
        "record_count": int(payload.get("record_count") or 0),
        "payload": json.dumps(payload, sort_keys=True),
        "observed_at": now,
        "source_lag_ms": payload.get("source_lag_ms"),
        "ack_status": "pending" if config.LIVE_PRODUCER_URL else "not_configured",
        "ack_error": None,
        "acknowledged_at": None,
    }
    inserted = False
    with engine().begin() as cx:
        existing = cx.execute(select(live_observations).where(
            (live_observations.c.source_id == source_id)
            & (live_observations.c.window_id == window_id))).mappings().fetchone()
        if existing:
            if existing["content_sha256"] != digest:
                raise WindowDigestMismatch(
                    f"window {window_id} changed digest: "
                    f"{existing['content_sha256']} -> {digest}")
            observation_id = existing["observation_id"]
        else:
            try:
                with cx.begin_nested():
                    cx.execute(insert(live_observations), row)
                inserted = True
            except IntegrityError:  # another worker won after our SELECT
                existing = cx.execute(select(live_observations).where(
                    (live_observations.c.source_id == source_id)
                    & (live_observations.c.window_id == window_id))).mappings().one()
                if existing["content_sha256"] != digest:
                    raise WindowDigestMismatch(
                        f"window {window_id} changed digest: "
                        f"{existing['content_sha256']} -> {digest}")
                observation_id = existing["observation_id"]
            if inserted:
                history_rows = []
                for key, sig in payload.get("signals", {}).items():
                    value = sig.get("value")
                    history_rows.append({
                        "source_id": source_id, "observation_id": observation_id,
                        "tick": observed_tick, "signal_key": key,
                        "value": None if value is None else float(value),
                        "health": sig.get("health", "Unknown"),
                    })
                if history_rows:
                    cx.execute(insert(live_signal_history), history_rows)
        cursor = cx.execute(select(live_source_cursors.c.next_tick,
                                   live_source_cursors.c.observed_tick).where(
            live_source_cursors.c.source_id == source_id)).mappings().one()
        monotonic_next = max(int(cursor["next_tick"]), int(next_tick))
        monotonic_observed = max(
            int(cursor["observed_tick"]) if cursor["observed_tick"] is not None else -1,
            observed_tick)
        cx.execute(update(live_source_cursors).where(
            live_source_cursors.c.source_id == source_id).values(
                next_tick=monotonic_next, source_tick=source_tick,
                observed_tick=monotonic_observed, state=state, backlog=max(0, int(backlog)),
                last_checked_at=now, last_success_at=now,
                last_error_at=None, last_error=None, updated_at=now))
    return observation_id, inserted


def get_latest_live_observation(source_id: str) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(live_observations).where(
            live_observations.c.source_id == source_id).order_by(
                live_observations.c.observed_tick.desc()).limit(1)).mappings().fetchone()
    if not row:
        return None
    out = dict(row)
    out["payload"] = json.loads(out["payload"])
    out["provenance_counts"] = json.loads(out["provenance_counts"] or "{}")
    return out


def list_live_observations(source_id: str | None = None, limit: int = 100) -> list[dict]:
    q = select(live_observations)
    if source_id:
        q = q.where(live_observations.c.source_id == source_id)
    q = q.order_by(live_observations.c.observed_at.desc()).limit(max(1, min(limit, 500)))
    with engine().begin() as cx:
        rows = cx.execute(q).mappings().all()
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload"])
        item["provenance_counts"] = json.loads(item["provenance_counts"] or "{}")
        out.append(item)
    return out


def get_live_signal_history(source_id: str, signal_key: str, limit: int) -> list[dict]:
    q = (select(live_signal_history.c.tick, live_signal_history.c.value,
                live_signal_history.c.health)
         .where((live_signal_history.c.source_id == source_id)
                & (live_signal_history.c.signal_key == signal_key))
         .order_by(live_signal_history.c.tick.desc()).limit(max(1, limit)))
    with engine().begin() as cx:
        rows = cx.execute(q).mappings().all()
    return [dict(r) for r in reversed(rows)]


def set_live_ack(observation_id: str, *, ok: bool, error: str | None = None) -> None:
    with engine().begin() as cx:
        cx.execute(update(live_observations).where(
            live_observations.c.observation_id == observation_id).values(
                ack_status="acknowledged" if ok else "error",
                ack_error=None if ok else (error or "unknown acknowledgement error"),
                acknowledged_at=time.time() if ok else None))


def list_live_acks_to_retry(limit: int = 100) -> list[dict]:
    """Return durable pending/error acknowledgements for eventual producer delivery."""
    q = (select(
            live_observations.c.observation_id,
            live_observations.c.window_id,
            live_observations.c.content_sha256,
        )
        .where(live_observations.c.ack_status.in_(["pending", "error"]))
        .order_by(live_observations.c.observed_at)
        .limit(max(1, min(limit, 500))))
    with engine().begin() as cx:
        rows = cx.execute(q).mappings().all()
    return [dict(row) for row in rows]


def clear_live_state(source_id: str | None = None) -> None:
    """Developer/test reset. Strict-live HTTP routes never expose this operation."""
    with engine().begin() as cx:
        if source_id:
            obs_ids = select(live_observations.c.observation_id).where(
                live_observations.c.source_id == source_id)
            cx.execute(delete(live_signal_history).where(
                live_signal_history.c.observation_id.in_(obs_ids)))
            cx.execute(delete(live_observations).where(live_observations.c.source_id == source_id))
            cx.execute(delete(live_source_cursors).where(live_source_cursors.c.source_id == source_id))
        else:
            cx.execute(delete(live_signal_history))
            cx.execute(delete(live_observations))
            cx.execute(delete(live_source_cursors))


def acquire_live_lease(lease_name: str, owner_id: str, ttl_seconds: float) -> bool:
    now = time.time()
    expires = now + max(1.0, ttl_seconds)
    with engine().begin() as cx:
        updated = cx.execute(update(live_worker_leases).where(
            (live_worker_leases.c.lease_name == lease_name)
            & or_(live_worker_leases.c.owner_id == owner_id,
                  live_worker_leases.c.expires_at <= now)).values(
                      owner_id=owner_id, expires_at=expires, renewed_at=now))
        if updated.rowcount:
            return True
        try:
            with cx.begin_nested():
                cx.execute(insert(live_worker_leases), {
                    "lease_name": lease_name, "owner_id": owner_id,
                    "expires_at": expires, "renewed_at": now})
            return True
        except IntegrityError:
            return False


def release_live_lease(lease_name: str, owner_id: str) -> None:
    with engine().begin() as cx:
        cx.execute(delete(live_worker_leases).where(
            (live_worker_leases.c.lease_name == lease_name)
            & (live_worker_leases.c.owner_id == owner_id)))


def get_live_lease(lease_name: str) -> dict | None:
    with engine().begin() as cx:
        row = cx.execute(select(live_worker_leases).where(
            live_worker_leases.c.lease_name == lease_name)).mappings().fetchone()
    return dict(row) if row else None
