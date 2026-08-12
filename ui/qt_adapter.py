"""
Qt adapter — the threading bridge between the pure-Python data layer and the UI.

The Fleet, the SSE event streams, and the Poller all run work on background threads and
communicate via plain callbacks. Qt widgets, however, may only be touched on the
GUI thread. This adapter is the single place that crosses that boundary:

  - It owns the Fleet, the SSE event stream manager, and the Poller.
  - It registers callbacks on the streams/poller that simply EMIT Qt signals.
  - Because the adapter is a QObject and the signals are connected with the
    default AutoConnection, Qt automatically queues the emission onto the GUI
    thread when it originates from a worker thread. So slots connected to these
    signals run safely on the UI thread.

The UI connects to:
    event_received(object)   # a CrashEvent | EventWebhook | SequenceWebhook | TaskEvent | dict
    fast_update(object)      # a FastSnapshot
    slow_update(object)      # a SlowSnapshot

Long/blocking fleet actions (panic, arm, start/stop) should be run via
run_async() so they don't block the GUI thread.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from api import Fleet
from state import Poller, Discovery
from webhook.stream_client import EventStreamManager

logger = logging.getLogger(__name__)


class DataHub(QObject):
    """Owns the data layer and re-emits its callbacks as Qt signals."""

    # Inbound stream event (crash / event_* / sequence_*). Payload is a model or dict.
    event_received = pyqtSignal(object)
    # Poller snapshots.
    fast_update = pyqtSignal(object)   # FastSnapshot
    slow_update = pyqtSignal(object)   # SlowSnapshot
    # Per-unit SSE stream connection status: (unit_id, connected).
    stream_status = pyqtSignal(str, bool)
    # Emitted when a run_async task finishes: (label, result_or_exception).
    task_done = pyqtSignal(str, object)
    # The set of discovered (mDNS) units changed — GUI-thread hook for auto-learn.
    discovery_changed = pyqtSignal()

    def __init__(
        self,
        fleet: Fleet,
        fast_interval_s: float = 3.0,
        slow_interval_s: float = 30.0,
        api_secret: str = "",
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.fleet = fleet
        self.streams = EventStreamManager(api_key=api_secret)
        self.poller = Poller(fleet, fast_interval_s, slow_interval_s)
        self.discovery = Discovery()    # mDNS browser for units advertising themselves
        self._api_secret = api_secret
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="hub-action")
        self._stopped = False

        # Wire the data layer's callbacks to signal emissions. Emitting a Qt signal
        # from a worker thread is safe and is delivered to the GUI thread via the
        # event loop (queued connection), so connected slots run on the GUI thread.
        self.streams.set_callback(lambda ev: self.event_received.emit(ev))
        self.streams.set_status_callback(lambda uid, ok: self.stream_status.emit(uid, ok))
        self.poller.set_fast_callback(lambda snap: self.fast_update.emit(snap))
        self.poller.set_slow_callback(lambda snap: self.slow_update.emit(snap))

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the event streams and poller. Call after the window is shown."""
        self.streams.start_all(self.fleet.units())
        self.poller.start()
        # Re-emit discovery changes as a Qt signal so the GUI thread can react
        # (the zeroconf callback fires on a background thread).
        self.discovery.set_callback(self.discovery_changed.emit)
        self.discovery.start()
        logger.info("DataHub started")

    def stop(self) -> None:
        # Idempotent: closeEvent and main() both call this, so the second call
        # should be a no-op rather than repeating the work (and double-logging).
        if self._stopped:
            return
        self._stopped = True
        self.poller.stop()
        self.streams.stop()
        self.discovery.stop()
        self.log_tailer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("DataHub stopped")

    # ── Runtime fleet membership ─────────────────────────────────────────────────

    def add_unit(self, client) -> None:
        """Register a newly-added unit and begin streaming from it. The poller picks
        it up automatically on its next tick (it iterates the fleet)."""
        self.fleet.add(client)
        if not self._stopped:
            self.streams.start_for(client)

    def remove_unit(self, hostname: str) -> None:
        """Stop streaming from a unit and drop it from the fleet."""
        self.streams.stop_for(hostname)
        self.fleet.remove(hostname)

    # ── Async action runner ─────────────────────────────────────────────────────

    def run_async(self, label: str, fn: Callable[[], object]) -> None:
        """
        Run a blocking fleet action on a worker thread. When done, emit
        task_done(label, result_or_exception) on the GUI thread. Use for panic,
        arm, start/stop, deploy, etc. — anything that hits the network.
        """
        def _wrapped():
            try:
                result = fn()
            except Exception as exc:   # noqa: BLE001
                result = exc
            # Emit from the worker thread; delivered to GUI thread via the event loop.
            self.task_done.emit(label, result)

        self._executor.submit(_wrapped)

    # ── Immediate refresh ────────────────────────────────────────────────────────

    def refresh_now(self) -> None:
        """
        Run one fast-tier poll immediately (off the GUI thread) and emit
        fast_update, instead of waiting up to fast_interval_s for the next tick.

        Called when a unit view opens and after a task action, so the UI reflects
        the new state right away rather than lagging a whole poll cycle behind.
        """
        if self._stopped:
            return

        def _run():
            try:
                snap = self.poller.poll_fast_once()
            except Exception:   # noqa: BLE001 — a failed ad-hoc poll is non-fatal
                return
            if not self._stopped:
                self.fast_update.emit(snap)

        self._executor.submit(_run)

    # ── Convenience: panic all ───────────────────────────────────────────────────

    def panic_all(self) -> None:
        """Fire the all-units emergency stop off the GUI thread."""
        self.run_async("panic_all", lambda: self.fleet.panic_all())

    # ── Convenience: keep unit replicas in sync ──────────────────────────────────

    def sync_state_to_units(self) -> None:
        """Push the PC's current plans + schedule to every unit so all replicas
        stay identical to the PC (the source of truth). Call this after any local
        edit to a plan or schedule entry. Runs off the GUI thread; reachable units
        update immediately, unreachable ones are left behind and reconcile on the
        next explicit deploy / drift check. Result is reported as a 'state_sync'
        task_done so the UI can note any units it couldn't reach."""
        from state import PlanStore, ScheduleStore
        plans = PlanStore().plans()
        schedule = ScheduleStore().entries()
        self.run_async(
            "state_sync",
            lambda: self.fleet.sync_plans_schedule_all(plans, schedule),
        )