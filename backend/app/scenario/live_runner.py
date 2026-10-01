"""Live runner — the monitor's LIVE observation of an external model app.

On each tick() it advances its read position, pulls the current telemetry window
through the LIVE adapters (which reach OUT to the model app over HTTP), grades the
signals with the SAME health engine the baker uses (Green/Amber/Red rollup), and stores
the graded payload. Cursor sync (contract v1.0): before pulling, each runner checks the
app's /telemetry/meta latest_tick best-effort — if the monitor is ahead it reports
{"waiting": true} WITHOUT advancing, so it never burns ticks against 404s. It is kept
ENTIRELY SEPARATE from the baked scenario player/baker — those stay byte-deterministic
and untouched. tick() does sync HTTP + heavy engine work, so callers run it in a worker
thread (never the event loop).
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading

from .. import config, db, label_backfill
from ..adapters.base import ExplainResult, TickContext
from ..adapters.explain.lime_shap import make_explain
from ..adapters.explain.live_http import LiveHttpExplainAdapter
from ..adapters.llm_eval.judge import make_llm_eval
from ..adapters.ml_monitor.evidently_nannyml import make_ml_monitor
from ..adapters.ml_monitor.nba_live_http import LiveHttpNBAAdapter
from ..adapters.telemetry_http import acknowledge_observation, pull_meta
from ..engines import health
from ..live_sync import source_sync
from .baker import _artifact_writer_factory

LIVE_UC = "AICT-L01"      # the live churn use case (ML lane)
LIVE_LLM_UC = "AICT-L02"  # the live chatbot use case (LLM lane)
LIVE_NBA_UC = "AICT-L03"  # the live NBA recommender use case (ML + feedback lanes)

# Unmeasured lanes are explicitly Unknown — never optimistic Green.  They are excluded
# from the measured overall rollup, but stay visible with an auditable reason.
_NOT_INSTRUMENTED = "Unknown — not instrumented"
_HAND_SET = {"Feedback & action loop": "Unknown", "Safety & security": "Unknown",
             "Reliability": "Unknown"}
_EXCLUDED_LANES = set(_HAND_SET)

# lanes an LLM use case doesn't produce signals for (its Quality/Safety/Reliability lanes
# come from the 5 LLM signals); Drift is assumed clean, Feedback is declared-Unknown.
_HAND_SET_LLM = {"Drift & degradation": "Unknown", "Feedback & action loop": "Unknown"}
_EXCLUDED_LANES_LLM = set(_HAND_SET_LLM)

# NBA hand-sets only Safety/Reliability — Feedback is a REAL lane (acceptance_rate);
# while rewards lag it is reasoned-Unknown and excluded, once arrived it COUNTS.
_HAND_SET_NBA = {"Safety & security": "Unknown", "Reliability": "Unknown"}

# windows with nothing (or too little) to explain: skip LIME/SHAP, keep the observation
_NO_EXPLAIN = {"insufficient_sample", "empty_window"}


def _source_lag_ms(closed_at: str | None) -> float | None:
    if not closed_at:
        return None
    try:
        dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def _window_fields(result, tick: int) -> dict:
    meta = dict(getattr(result, "metadata", {}) or {})
    if meta.get("model_version") is None:
        meta.pop("model_version", None)  # do not erase adapter-derived legacy metadata
    meta.setdefault("window_id", f"legacy-{tick}")
    meta.setdefault("content_sha256", "")
    meta["record_count"] = int(meta.get("record_count") or 0)
    meta["source_lag_ms"] = _source_lag_ms(meta.get("closed_at"))
    return meta


def _commit_tick(runner, payload: dict, telemetry_err: str | None, meta: dict) -> dict:
    """Durably store a graded tick before advancing the shared source cursor."""
    if telemetry_err:
        db.mark_live_error(runner.use_case_id, telemetry_err)
        held = runner.state() or payload
        held = dict(held)
        held["cursor_held"] = True
        held["sync_state"] = "error"
        held["state"] = "error"
        held["errors"] = {**held.get("errors", {}), "telemetry": telemetry_err}
        runner._current = held
        return held

    latest_tick = meta.get("latest_tick")
    source_tick = (int(latest_tick) - 1) if latest_tick is not None else runner._read_tick
    next_tick = runner._read_tick + 1
    backlog = max(0, int(latest_tick) - next_tick) if latest_tick is not None else 0
    state = "catching_up" if backlog else "at_tail"
    payload.update({
        "source_tick": source_tick,
        "producer_tick": source_tick,
        "observed_tick": runner._read_tick,
        "backlog": backlog,
        "sync_state": state,
        "state": state,
    })
    try:
        observation_id, _ = db.put_live_observation(
            runner.use_case_id, payload, source_tick=source_tick,
            next_tick=next_tick, backlog=backlog, state=state)
    except db.WindowDigestMismatch as exc:
        return _commit_tick(runner, payload, str(exc), meta)

    runner._read_tick = next_tick
    payload["observation_id"] = observation_id
    payload["window_digest"] = payload.get("content_sha256")
    runner._current = payload

    # The just-observed tick's realized rows (normally `pending`: labels lag).  Lives in
    # live_realized_metrics only — the stored observation above is never rewritten.
    realized_keys = getattr(runner, "realized_keys", ())
    if realized_keys:
        try:
            label_backfill.record_current_tick(
                runner.use_case_id, payload["observed_tick"], payload, realized_keys)
        except Exception as exc:  # noqa: BLE001 — never undo a committed observation
            db.mark_live_warning(runner.use_case_id, f"realized row: {exc}")

    # Acknowledgement is deliberately after the durable local commit.  Its failure is
    # persisted separately and never rolls back or loses the observation.
    if config.LIVE_PRODUCER_URL:
        try:
            acknowledge_observation(
                config.LIVE_PRODUCER_URL,
                window_id=payload["window_id"], observation_id=observation_id,
                content_sha256=payload["content_sha256"])
            db.set_live_ack(observation_id, ok=True)
            payload["ack_status"] = "acknowledged"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            db.set_live_ack(observation_id, ok=False, error=error)
            payload["ack_status"] = "error"
            payload["ack_error"] = error
    else:
        payload["ack_status"] = "not_configured"
    return payload


def retry_pending_acknowledgements(limit: int = 100) -> dict:
    """Retry durable producer acknowledgements independently of cursor advancement."""
    if not config.LIVE_PRODUCER_URL:
        return {"attempted": 0, "acknowledged": 0, "failed": 0}
    rows = db.list_live_acks_to_retry(limit)
    acknowledged = 0
    for row in rows:
        try:
            acknowledge_observation(
                config.LIVE_PRODUCER_URL,
                window_id=row["window_id"],
                observation_id=row["observation_id"],
                content_sha256=row["content_sha256"],
            )
            db.set_live_ack(row["observation_id"], ok=True)
            acknowledged += 1
        except Exception as exc:  # noqa: BLE001 — retained for the next lease cycle
            db.set_live_ack(
                row["observation_id"], ok=False,
                error=f"{type(exc).__name__}: {exc}")
    return {"attempted": len(rows), "acknowledged": acknowledged,
            "failed": len(rows) - acknowledged}


def _signal_view(sig: dict, s_health: dict, extra: dict | None = None) -> dict:
    """Shape a signals dict {key -> {value, health, label, lane, ...}} for the UI."""
    specs = health.SIGNAL_SPECS
    out = {}
    for k, v in sig.items():
        sp = specs.get(k)
        out[k] = {
            "value": None if v is None else round(float(v), 4), "health": s_health[k],
            "label": sp.label if sp else k, "lane": sp.lane if sp else "Quality",
            "unit": sp.unit if sp else "", "direction": sp.direction if sp else "",
            "green_bar": sp.green_bar if sp else None, "red_bar": sp.red_bar if sp else None,
            **((extra or {}).get(k, {}))}
    return out


def _rollup_meta(hand_set: dict, excluded_lanes: set, excluded_keys: set) -> dict:
    """The rollup inputs, persisted with the payload so `realized_view.apply_realized`
    can recompute lanes/overall once a lagged label lands (read-time, never a rewrite)."""
    return {"hand_set_lanes": dict(hand_set), "excluded_lanes": sorted(excluded_lanes),
            "excluded_keys": sorted(excluded_keys)}


def _ahead_of_app(base_url: str, read_tick: int, uc: str) -> tuple[dict | None, dict]:
    """Cursor sync: best-effort /telemetry/meta; the monitor reads only CLOSED windows
    (tick < latest_tick — the latest window is still open: /chat appends to it, contract
    §6). If the cursor has caught up, return a waiting payload (caller must NOT advance
    the cursor)."""
    try:
        meta = pull_meta(base_url, strict=True)
        if meta.get("latest_tick") is None:
            raise ValueError("/telemetry/meta omitted latest_tick")
        latest = int(meta["latest_tick"])
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        db.mark_live_error(uc, error)
        return ({"use_case_id": uc, "tick": None, "mode": "live",
                 "waiting": True, "cursor_held": True, "sync_state": "error",
                 "state": "error", "errors": {"telemetry": error}}, {})

    source_tick = latest - 1
    backlog = max(0, latest - read_tick)
    contact_state = "catching_up" if backlog else "at_tail"
    db.mark_live_contact(uc, source_tick, backlog, contact_state)
    if read_tick >= latest:
        sync = source_sync(uc)
        return ({"use_case_id": uc, "tick": None, "mode": "live",
                 "waiting": True, "latest_app_tick": latest, **sync}, meta)
    return None, meta


def _backfill_labels(runner, meta: dict, current_tick: int) -> None:
    """Realize lagged labels for recent ticks; a failure here never fails the tick.

    `current_tick` (the monitor's tick) bounds the window.  Finality is judged against
    the producer's latest CLOSED tick, `latest_tick - 1` (the same `source_tick` that
    `_commit_tick` stores): while waiting at the tail `current_tick == latest_tick`, the
    window the producer is still writing, and labels due in it may still be published.
    """
    try:
        latest_tick = meta.get("latest_tick")
        source_tick = (int(latest_tick) - 1) if latest_tick is not None else current_tick
        label_backfill.run(
            runner.use_case_id, runner.ml, current_tick=current_tick,
            lag=label_backfill.lag_from_meta(meta, kind=runner.lane_kind),
            metric_keys=runner.realized_keys, source_tick=source_tick)
    except Exception as exc:  # noqa: BLE001
        db.mark_live_warning(runner.use_case_id, f"backfill: {type(exc).__name__}: {exc}")


class LiveRunner:
    realized_keys = ("realized_roc_auc",)
    lane_kind = "ml"

    def __init__(self, churn_url: str | None = None, seed: int | None = None) -> None:
        self.use_case_id = LIVE_UC
        self.seed = seed if seed is not None else config.DEMO_SEED
        self.base_url = (churn_url or config.LIVE_CHURN_URL).rstrip("/")
        # per-use-case artifact namespace: L01 and L03 write the same artifact KINDS at
        # the same ticks — a shared "LIVE" namespace makes them overwrite each other's
        # files and db rows (review finding)
        writer = _artifact_writer_factory(f"LIVE-{LIVE_UC}")
        cfg = {"base_url": self.base_url, "chunk_size": 500, "model_name": "telco-churn"}
        self.ml = make_ml_monitor(self.seed, cfg, writer, impl="live_http")
        self.explain = make_explain(self.seed, cfg, writer, impl="live_http")
        cursor = db.get_live_source(self.use_case_id)
        self._read_tick = int(cursor["next_tick"])
        latest = db.get_latest_live_observation(self.use_case_id)
        self._current: dict | None = latest["payload"] if latest else None
        self._lock = threading.Lock()

    def _grade(self, ml_res, ex_res, t: int) -> dict:
        sig = dict(ml_res.signals)
        s_health = {k: health.evaluate(k, v) for k, v in sig.items()}
        records = ml_res.records if isinstance(ml_res.records, dict) else {}
        pending = records.get("realized_pending_reason")
        excluded = {"realized_roc_auc"} if pending else set()
        if pending:
            s_health["realized_roc_auc"] = "Unknown"  # reasoned-Unknown, excluded from rollup
        lanes, overall = health.rollup(
            s_health, excluded_keys=excluded,
            hand_set_lanes=_HAND_SET, excluded_lanes=_EXCLUDED_LANES)
        extra = {"realized_roc_auc": {"pending_reason": pending}} if pending else {}
        return {
            "use_case_id": LIVE_UC, "tick": t, "mode": "live",
            "signals": _signal_view(sig, s_health, extra), "lanes": lanes, "overall": overall,
            "rollup_meta": _rollup_meta(_HAND_SET, _EXCLUDED_LANES, excluded),
            "lane_reasons": {lane: _NOT_INSTRUMENTED for lane in _HAND_SET},
            "drifted_features": records.get("drifted_features", []),
            "reference_auc": records.get("reference_auc"),
            "model_version": records.get("model_version"),
            "realized_pending_reason": pending,
            "realized_label_coverage": records.get("realized_label_coverage"),
            **({"empty_window": True} if records.get("empty_window") else {}),
            "lime_top": ex_res.lime_top, "lime_instance": ex_res.instance,
            "artifacts": {**ml_res.artifacts, **ex_res.artifacts},
            "errors": {**ml_res.errors, **ex_res.errors},
            **_window_fields(ml_res, t),
        }

    def tick(self) -> dict:
        """Observe the next telemetry window, grade it, store + return the payload."""
        with self._lock:
            self._read_tick = int(db.get_live_source(self.use_case_id)["next_tick"])
            t = self._read_tick
            waiting, meta = _ahead_of_app(self.base_url, t, LIVE_UC)
            if waiting:
                if meta:  # producer reachable: labels may have landed for older ticks
                    _backfill_labels(self, meta, t)
                return waiting  # don't advance, don't store as _current
            ctx = TickContext(tick=t, seed=self.seed, scenario_id="LIVE")
            ml_res = self.ml.monitor(LIVE_UC, ctx)
            ex_res = (ExplainResult() if _NO_EXPLAIN & ml_res.errors.keys()
                      else self.explain.explain(LIVE_UC, ctx))
            payload = self._grade(ml_res, ex_res, t)
            out = _commit_tick(self, payload, ml_res.errors.get("telemetry"), meta)
            if not out.get("cursor_held"):
                _backfill_labels(self, meta, t)
            return out

    def state(self) -> dict | None:
        latest = db.get_latest_live_observation(self.use_case_id)
        current = dict(latest["payload"]) if latest else (dict(self._current) if self._current else None)
        if current is None:
            return None
        sync = source_sync(self.use_case_id)
        current.update(sync)
        if sync.get("last_error"):
            current["errors"] = {**current.get("errors", {}),
                                 "telemetry": sync["last_error"]}
        return current


class LiveLLMRunner:
    """LIVE observation of an external chatbot: pull traces -> LLM-as-judge -> grade the
    LLM lanes with the SAME health engine. Separate from the baked player (byte-deterministic
    and untouched). tick() does sync HTTP + judging, so callers run it in a worker thread."""

    def __init__(self, chatbot_url: str | None = None, seed: int | None = None) -> None:
        self.use_case_id = LIVE_LLM_UC
        self.seed = seed if seed is not None else config.DEMO_SEED
        self.base_url = (chatbot_url or config.LIVE_CHATBOT_URL).rstrip("/")
        cfg = {"base_url": self.base_url, "judge_model": config.LLM_JUDGE_MODEL}
        self.llm = make_llm_eval(self.seed, "LIVE", LIVE_LLM_UC, cfg=cfg, impl="live_http")
        cursor = db.get_live_source(self.use_case_id)
        self._read_tick = int(cursor["next_tick"])
        latest = db.get_latest_live_observation(self.use_case_id)
        self._current: dict | None = latest["payload"] if latest else None
        self._lock = threading.Lock()

    def _grade(self, res, t: int) -> dict:
        sig = dict(res.signals)  # the 5 LLM signals
        s_health = {k: health.evaluate(k, v) for k, v in sig.items()}
        lanes, overall = health.rollup(
            s_health, hand_set_lanes=_HAND_SET_LLM, excluded_lanes=_EXCLUDED_LANES_LLM)
        return {
            "use_case_id": LIVE_LLM_UC, "tick": t, "mode": "live",
            "signals": _signal_view(sig, s_health), "lanes": lanes, "overall": overall,
            "rollup_meta": _rollup_meta(_HAND_SET_LLM, _EXCLUDED_LANES_LLM, set()),
            "lane_reasons": {lane: _NOT_INSTRUMENTED for lane in _HAND_SET_LLM},
            "judge": (config.LLM_JUDGE_MODEL if config.ANTHROPIC_API_KEY
                      else ("required-unavailable" if config.strict_live_mode() else "heuristic-v1")),
            "judge_sample": res.records if isinstance(res.records, list) else [],
            "errors": res.errors,
            **_window_fields(res, t),
        }

    def tick(self) -> dict:
        with self._lock:
            self._read_tick = int(db.get_live_source(self.use_case_id)["next_tick"])
            t = self._read_tick
            waiting, meta = _ahead_of_app(self.base_url, t, LIVE_LLM_UC)
            if waiting:
                return waiting
            ctx = TickContext(tick=t, seed=self.seed, scenario_id="LIVE")
            res = self.llm.evaluate(LIVE_LLM_UC, ctx)
            payload = self._grade(res, t)
            return _commit_tick(self, payload, res.errors.get("telemetry"), meta)

    def state(self) -> dict | None:
        latest = db.get_latest_live_observation(self.use_case_id)
        current = dict(latest["payload"]) if latest else (dict(self._current) if self._current else None)
        if current is None:
            return None
        sync = source_sync(self.use_case_id)
        current.update(sync)
        if sync.get("last_error"):
            current["errors"] = {**current.get("errors", {}),
                                 "telemetry": sync["last_error"]}
        return current


class LiveNBARunner:
    """LIVE observation of the NBA recommender. Instantiates the live adapters DIRECTLY
    (not via the seeded factories — those are part of the golden-bake swap seam and stay
    untouched): LiveHttpNBAAdapter for drift/CBPE/realized-AUC + acceptance_rate +
    recommendation_drift, and LiveHttpExplainAdapter for LIME/SHAP on the pulled model.
    Unlike the churn runner, Feedback & action loop is a REAL lane here — graded from
    acceptance_rate once rewards arrive, reasoned-Unknown (excluded from overall) while
    they lag."""

    realized_keys = ("realized_roc_auc", "acceptance_rate")
    lane_kind = "nba"

    def __init__(self, nba_url: str | None = None, seed: int | None = None) -> None:
        self.use_case_id = LIVE_NBA_UC
        self.seed = seed if seed is not None else config.DEMO_SEED
        self.base_url = (nba_url or config.LIVE_NBA_URL).rstrip("/")
        writer = _artifact_writer_factory(f"LIVE-{LIVE_NBA_UC}")   # see LiveRunner note
        self.ml = LiveHttpNBAAdapter(self.base_url, writer)
        self.explain = LiveHttpExplainAdapter(
            base_url=self.base_url, artifact_writer=writer, model_name="nba-recommender",
            seed=self.seed, class_names=["decline", "accept"],
            inferences_path="/telemetry/recommendations")
        cursor = db.get_live_source(self.use_case_id)
        self._read_tick = int(cursor["next_tick"])
        latest = db.get_latest_live_observation(self.use_case_id)
        self._current: dict | None = latest["payload"] if latest else None
        self._lock = threading.Lock()

    def _grade(self, ml_res, ex_res, t: int) -> dict:
        sig = dict(ml_res.signals)
        s_health = {k: health.evaluate(k, v) for k, v in sig.items()}
        records = ml_res.records if isinstance(ml_res.records, dict) else {}
        pending = records.get("realized_pending_reason")
        acc_pending = ml_res.errors.get("acceptance_pending")
        excluded: set[str] = set()
        excluded_lanes: set[str] = set(_HAND_SET_NBA)
        hand_set = dict(_HAND_SET_NBA)
        extra: dict = {}
        if pending:  # reasoned-Unknown, excluded from rollup (labels lag by design)
            excluded.add("realized_roc_auc")
            s_health["realized_roc_auc"] = "Unknown"
            extra["realized_roc_auc"] = {"pending_reason": pending}
        if acc_pending:  # same treatment for the Feedback lane while rewards lag
            excluded.add("acceptance_rate")
            s_health["acceptance_rate"] = "Unknown"
            extra["acceptance_rate"] = {"pending_reason": acc_pending}
            hand_set["Feedback & action loop"] = "Unknown"
            excluded_lanes.add("Feedback & action loop")
        rec_pending = ml_res.errors.get("recommendation_drift_pending")
        if rec_pending:  # awaiting a current-version window to anchor the baseline mix
            excluded.add("recommendation_drift")
            s_health["recommendation_drift"] = "Unknown"
            extra["recommendation_drift"] = {"pending_reason": rec_pending}
        lanes, overall = health.rollup(
            s_health, excluded_keys=excluded,
            hand_set_lanes=hand_set, excluded_lanes=excluded_lanes)
        return {
            "use_case_id": LIVE_NBA_UC, "tick": t, "mode": "live",
            "signals": _signal_view(sig, s_health, extra), "lanes": lanes, "overall": overall,
            "rollup_meta": _rollup_meta(hand_set, excluded_lanes, excluded),
            "lane_reasons": {lane: _NOT_INSTRUMENTED for lane in _HAND_SET_NBA},
            "drifted_features": records.get("drifted_features", []),
            "reference_auc": records.get("reference_auc"),
            "model_version": records.get("model_version"),
            "realized_pending_reason": pending,
            "realized_label_coverage": records.get("realized_label_coverage"),
            "acceptance_pending_reason": acc_pending,
            "offer_mix": records.get("offer_mix"),
            "baseline_offer_mix": records.get("baseline_offer_mix"),
            **({"empty_window": True} if records.get("empty_window") else {}),
            "lime_top": ex_res.lime_top, "lime_instance": ex_res.instance,
            "artifacts": {**ml_res.artifacts, **ex_res.artifacts},
            "errors": {**ml_res.errors, **ex_res.errors},
            **_window_fields(ml_res, t),
        }

    def tick(self) -> dict:
        with self._lock:
            self._read_tick = int(db.get_live_source(self.use_case_id)["next_tick"])
            t = self._read_tick
            waiting, meta = _ahead_of_app(self.base_url, t, LIVE_NBA_UC)
            if waiting:
                if meta:
                    _backfill_labels(self, meta, t)
                return waiting
            ctx = TickContext(tick=t, seed=self.seed, scenario_id="LIVE")
            ml_res = self.ml.monitor(LIVE_NBA_UC, ctx)
            ex_res = (ExplainResult() if _NO_EXPLAIN & ml_res.errors.keys()
                      else self.explain.explain(LIVE_NBA_UC, ctx))
            payload = self._grade(ml_res, ex_res, t)
            out = _commit_tick(self, payload, ml_res.errors.get("telemetry"), meta)
            if not out.get("cursor_held"):
                _backfill_labels(self, meta, t)
            return out

    def state(self) -> dict | None:
        latest = db.get_latest_live_observation(self.use_case_id)
        current = dict(latest["payload"]) if latest else (dict(self._current) if self._current else None)
        if current is None:
            return None
        sync = source_sync(self.use_case_id)
        current.update(sync)
        if sync.get("last_error"):
            current["errors"] = {**current.get("errors", {}),
                                 "telemetry": sync["last_error"]}
        return current


_ml_runner: LiveRunner | None = None
_llm_runner: LiveLLMRunner | None = None
_nba_runner: LiveNBARunner | None = None
_runner_lock = threading.Lock()   # concurrent first ticks must not build two runners


def live_runner(uc: str = LIVE_UC):
    """Return the live runner for a use case (churn ML by default, chatbot LLM for L02,
    NBA recommender for L03)."""
    global _ml_runner, _llm_runner, _nba_runner
    with _runner_lock:
        if uc == LIVE_LLM_UC:
            if _llm_runner is None:
                _llm_runner = LiveLLMRunner()
            return _llm_runner
        if uc == LIVE_NBA_UC:
            if _nba_runner is None:
                _nba_runner = LiveNBARunner()
            return _nba_runner
        if _ml_runner is None:
            _ml_runner = LiveRunner()
        return _ml_runner


def reset_live_runner(uc: str | None = None) -> None:
    global _ml_runner, _llm_runner, _nba_runner
    if uc in (None, LIVE_UC):
        _ml_runner = None
    if uc in (None, LIVE_LLM_UC):
        _llm_runner = None
    if uc in (None, LIVE_NBA_UC):
        _nba_runner = None
