"""Lease-protected background telemetry poller for Autoscale deployments."""
from __future__ import annotations

import os
import socket
import threading
import uuid

from . import config, db

LEASE_NAME = "control-tower-live-poller"


class LivePoller:
    def __init__(self) -> None:
        self.owner_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The database lease elects one Autoscale *process*, but renewals by the same
        # owner are intentionally allowed.  Serialize cycles inside that process so an
        # authenticated wake request cannot overlap the warm background loop.
        self._cycle_lock = threading.Lock()
        self.last_cycle_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if (self.running or config.LIVE_POLL_SECONDS <= 0
                or config.live_configuration_errors()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="live-telemetry-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(10.0, config.LIVE_POLL_SECONDS + 2.0))
        db.release_live_lease(LEASE_NAME, self.owner_id)

    def run_once(self) -> bool:
        """Run a single lease-protected cycle; public for deterministic tests."""
        if not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            return self._run_once_with_lease()
        finally:
            self._cycle_lock.release()

    def _run_once_with_lease(self) -> bool:
        """Run one cycle after the process-local guard has been acquired."""
        if not db.acquire_live_lease(
                LEASE_NAME, self.owner_id, config.LIVE_POLL_LEASE_SECONDS):
            return False
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            # Renew while a single Evidently/SHAP/Claude call is in flight.  A model
            # window can take 15-60s; renewing only between sources is not sufficient.
            interval = max(0.25, config.LIVE_POLL_LEASE_SECONDS / 3.0)
            while not heartbeat_stop.wait(interval):
                try:
                    renewed = db.acquire_live_lease(
                        LEASE_NAME, self.owner_id, config.LIVE_POLL_LEASE_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    self.last_cycle_error = f"lease heartbeat failed: {type(exc).__name__}: {exc}"
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name="live-poller-lease-heartbeat", daemon=True)
        heartbeat_thread.start()
        from . import alert_delivery, alerting
        from .api.live_portfolio import LIVE_UCS
        from .scenario.live_runner import live_runner, retry_pending_acknowledgements
        try:
            retry_pending_acknowledgements()
            for uc in LIVE_UCS:
                if lease_lost.is_set() or not db.acquire_live_lease(
                        LEASE_NAME, self.owner_id, config.LIVE_POLL_LEASE_SECONDS):
                    return False
                try:
                    live_runner(uc).tick()
                    # Evaluate on every cycle, not only on a new observation: a lagged
                    # label realized by the backfill can turn a lane Red on its own.
                    alerting.evaluate(uc)
                except Exception as exc:  # noqa: BLE001 — isolate one source from the others
                    error = f"{type(exc).__name__}: {exc}"
                    db.mark_live_error(uc, error)
                    self.last_cycle_error = error
            if not lease_lost.is_set():
                try:  # notification only: a webhook outage must never fail the cycle
                    alert_delivery.deliver_pending()
                except Exception as exc:  # noqa: BLE001
                    self.last_cycle_error = f"alert delivery: {type(exc).__name__}: {exc}"
            return not lease_lost.is_set()
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — next cycle retries
                self.last_cycle_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(config.LIVE_POLL_SECONDS)
        db.release_live_lease(LEASE_NAME, self.owner_id)


_poller = LivePoller()


def poller() -> LivePoller:
    return _poller
