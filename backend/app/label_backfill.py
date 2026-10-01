"""Label-lag backfill: realize metrics for recently observed ticks once labels land.

Contract §7: labels (ML) and rewards (NBA) arrive `label_lag_ticks` / `reward_lag_ticks`
after a window closes.  The live tick can therefore only record *pending* for the window
it observes; this module revisits the ticks in `[t - L - 1, t)` on every poll cycle and
writes the outcome to `live_realized_metrics`.  A tick is *done* (`final`) once it is
`realized`, `evicted`, or the producer's `available_at_tick` is at or before the
producer's latest CLOSED tick (`latest_tick - 1`, the spec's "current source tick") with
still no labels (final `no_labels`, spec B.2); `pending` rows that slipped below the
window during a producer outage are swept once more so no tick is left `pending` forever.  It never
touches `live_observations`:
the stored payload and digest stay byte-identical, and the realized value is used by
`realized_view.apply_realized` when the use case is graded.

Runs inside the lease-held poll cycle (runner.tick), so two Autoscale instances never
backfill the same source concurrently; the table's unique key is the second guard.
"""
from __future__ import annotations

import logging

from . import db
from .adapters.telemetry_http import TelemetryIntegrityError, WindowEvicted

log = logging.getLogger(__name__)
DEFAULT_LAG = 3


def lag_from_meta(meta: dict, *, kind: str) -> int:
    """`label_lag_ticks` (ml) or `reward_lag_ticks` (nba) from /telemetry/meta, else 3."""
    key = "reward_lag_ticks" if kind == "nba" else "label_lag_ticks"
    try:
        return max(0, int(meta.get(key)))
    except (TypeError, ValueError):
        return DEFAULT_LAG


def status_from_reason(reason: str | None) -> str:
    """Map a live tick's pending reason onto a `live_realized_metrics.status`."""
    if reason is None:
        return "realized"
    if reason == "label lag":
        return "pending"
    if reason.startswith("label coverage"):
        return "insufficient_coverage"
    if reason.startswith("single class"):
        return "single_class"
    if reason.startswith("labels pull failed"):
        return "pending"        # transient: the backfill asks for the labels again
    return "no_labels"          # empty window, insufficient sample, no matched rewards


_PENDING_FIELD = {"realized_roc_auc": "realized_pending_reason",
                  "acceptance_rate": "acceptance_pending_reason"}


def record_current_tick(source_id: str, tick: int, payload: dict,
                        metric_keys: tuple[str, ...]) -> None:
    """Write the just-observed tick's realized rows from its graded payload.

    Called by the runner after the observation is durably stored.  The row is almost
    always `pending`; it exists so the backfill window and the detail payload can tell
    "never observed" from "observed, labels not yet here".
    """
    signals = payload.get("signals") or {}
    coverage = payload.get("realized_label_coverage")
    for key in metric_keys:
        sig = signals.get(key) or {}
        value = sig.get("value")
        reason = payload.get(_PENDING_FIELD.get(key, "realized_pending_reason"))
        status = "realized" if value is not None else status_from_reason(reason)
        db.put_realized_metric(source_id, tick, key, value=value, coverage=coverage,
                               status=status, reason=None if value is not None else reason)


def run(source_id: str, adapter, *, current_tick: int, lag: int,
        metric_keys: tuple[str, ...], source_tick: int | None = None) -> list[dict]:
    """Realize every non-final tick in `[current_tick - lag - 1, current_tick)`, plus any
    older tick still `pending`.

    `current_tick` (the monitor's own tick) bounds the window only.  Finality is judged
    against `source_tick`, the producer's latest closed tick (`latest_tick - 1`): while
    the monitor waits at the tail its tick equals the producer's still-OPEN window, in
    which labels may yet be published, so that tick must never finalize anything.  With
    no `source_tick` the monitor's tick is used (it is never later than the source tick).

    Returns the rows written as `{"tick", "metric_key", "status"}`.  A 404 on the
    inference window marks the tick `evicted` (final — the producer will never serve it
    again); a digest mismatch marks it `error` (retried, surfaced); labels missing once
    their due tick is at or before `source_tick` mark it final `no_labels`; any other
    failure is logged and stops this cycle's backfill so a flapping producer is not
    hammered.  Nothing here ever holds the source cursor.
    """
    if source_tick is None:
        source_tick = current_tick
    low, high = max(0, current_tick - lag - 1), current_tick
    in_window = {t for key in metric_keys
                 for t in db.ticks_needing_realization(source_id, key, low, high)}
    # Ticks that left the window still `pending` (the producer was unreachable for the
    # whole window): one more visit ends them realized or final no_labels.
    stragglers = {t for key in metric_keys
                  for t in db.pending_ticks_below(source_id, key, low)}
    written: list[dict] = []
    for t in sorted(in_window | stragglers):
        obs = db.get_live_observation_by_tick(source_id, t)
        expected = obs["content_sha256"] if obs else None
        try:
            results = adapter.realize_tick(t, expected, source_tick=source_tick,
                                           due_tick=t + lag)
        except WindowEvicted as exc:
            for key in metric_keys:
                db.put_realized_metric(source_id, t, key, value=None, coverage=None,
                                       status="evicted", reason=str(exc))
                written.append({"tick": t, "metric_key": key, "status": "evicted"})
            continue
        except TelemetryIntegrityError as exc:
            for key in metric_keys:
                db.put_realized_metric(source_id, t, key, value=None, coverage=None,
                                       status="error", reason=str(exc))
                written.append({"tick": t, "metric_key": key, "status": "error"})
            continue
        except Exception as exc:  # noqa: BLE001 — transient; retry next cycle
            log.warning("backfill %s tick %s failed: %s: %s",
                        source_id, t, type(exc).__name__, exc)
            break
        for key, r in results.items():
            if key not in metric_keys:
                continue
            db.put_realized_metric(source_id, t, key, value=r.value, coverage=r.coverage,
                                   status=r.status, reason=r.reason,
                                   final=True if r.final else None)
            written.append({"tick": t, "metric_key": key, "status": r.status})
    return written
