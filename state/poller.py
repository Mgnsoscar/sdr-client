"""
Poller — background polling of the fleet so the UI has live steady-state data.

Webhooks tell the UI when something *changes*; the poller provides the steady
baseline (connection health, CPU temp, clock sync, current task/run state) and
acts as a safety net if a webhook is ever missed.

Tiered cadence:
  - FAST tier (default 3s): health, system, tasks, sequence runs — the things you
    watch during an operation.
  - SLOW tier (default 30s): SDR probe and agent info — expensive or rarely
    changing.

Design mirrors the receiver: pure-Python core with a callback. A Qt adapter
(built with the UI) sets the callback to emit a signal so updates marshal to the
UI thread. The poll loops run in daemon threads.

Each poll cycle produces a PollSnapshot per tier and hands it to the callback.
Per-unit failures are captured (the Fleet fan-out returns exceptions per unit),
so one down unit never stops the cycle.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from api import Fleet
from api import models as m

logger = logging.getLogger(__name__)


@dataclass
class FastSnapshot:
    """One fast-tier poll cycle's results. Values are model instances or Exceptions."""
    health: Dict[str, bool] = field(default_factory=dict)          # unit_id -> reachable
    system: Dict[str, object] = field(default_factory=dict)        # unit_id -> SystemHealth | Exc
    tasks: Dict[str, object] = field(default_factory=dict)         # unit_id -> list[ProcessStatus] | Exc
    runs: Dict[str, object] = field(default_factory=dict)          # unit_id -> list[SequenceRun] | Exc


@dataclass
class SlowSnapshot:
    """One slow-tier poll cycle's results."""
    sdr: Dict[str, object] = field(default_factory=dict)           # unit_id -> SdrStatus | Exc
    info: Dict[str, object] = field(default_factory=dict)          # unit_id -> AgentInfo | Exc


FastCallback = Callable[[FastSnapshot], None]
SlowCallback = Callable[[SlowSnapshot], None]


class Poller:
    def __init__(
        self,
        fleet: Fleet,
        fast_interval_s: float = 3.0,
        slow_interval_s: float = 30.0,
    ):
        self.fleet = fleet
        self.fast_interval_s = fast_interval_s
        self.slow_interval_s = slow_interval_s

        self._fast_cb: Optional[FastCallback] = None
        self._slow_cb: Optional[SlowCallback] = None

        self._stop = threading.Event()
        self._fast_thread: Optional[threading.Thread] = None
        self._slow_thread: Optional[threading.Thread] = None

    # ── Callbacks ──────────────────────────────────────────────────────────────

    def set_fast_callback(self, cb: FastCallback) -> None:
        self._fast_cb = cb

    def set_slow_callback(self, cb: SlowCallback) -> None:
        self._slow_cb = cb

    # ── One-shot polls (also usable directly, e.g. for an eager first paint) ────

    def poll_fast_once(self) -> FastSnapshot:
        # Build sequentially, checking the stop flag between calls. Each fleet
        # call can block on connection timeouts to an unreachable unit, so this
        # lets a shutdown interrupt the cycle promptly instead of grinding
        # through every timeout (and colliding with interpreter teardown).
        snap = FastSnapshot()
        if self._stop.is_set():
            return snap
        snap.health = self.fleet.health_all()
        if self._stop.is_set():
            return snap
        snap.system = self.fleet.system_all()
        if self._stop.is_set():
            return snap
        snap.tasks = self.fleet.tasks_all()
        if self._stop.is_set():
            return snap
        snap.runs = self.fleet.list_runs_all()
        return snap

    def poll_slow_once(self) -> SlowSnapshot:
        snap = SlowSnapshot()
        if self._stop.is_set():
            return snap
        snap.sdr = self.fleet.sdr_all()
        if self._stop.is_set():
            return snap
        snap.info = self.fleet.info_all()
        return snap

    # ── Loops ──────────────────────────────────────────────────────────────────

    def _fast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self.poll_fast_once()
                if self._fast_cb and not self._stop.is_set():
                    self._fast_cb(snap)
            except Exception:
                if self._stop.is_set():
                    break   # shutting down — a failed cycle here is expected, stay quiet
                logger.exception("Fast poll cycle failed")
            self._stop.wait(self.fast_interval_s)

    def _slow_loop(self) -> None:
        # Stagger: let the fast loop get one cycle in before the slow tier runs.
        self._stop.wait(1.0)
        while not self._stop.is_set():
            try:
                snap = self.poll_slow_once()
                if self._slow_cb and not self._stop.is_set():
                    self._slow_cb(snap)
            except Exception:
                if self._stop.is_set():
                    break   # shutting down — stay quiet
                logger.exception("Slow poll cycle failed")
            self._stop.wait(self.slow_interval_s)

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._fast_thread is not None:
            return
        self._stop.clear()
        self._fast_thread = threading.Thread(
            target=self._fast_loop, name="poller-fast", daemon=True
        )
        self._slow_thread = threading.Thread(
            target=self._slow_loop, name="poller-slow", daemon=True
        )
        self._fast_thread.start()
        self._slow_thread.start()
        logger.info("Poller started (fast=%.1fs, slow=%.1fs)",
                    self.fast_interval_s, self.slow_interval_s)

    def stop(self) -> None:
        self._stop.set()
        for t in (self._fast_thread, self._slow_thread):
            if t is not None:
                t.join(timeout=self.fast_interval_s + 1.0)
        self._fast_thread = None
        self._slow_thread = None
        logger.info("Poller stopped")

    def __enter__(self) -> "Poller":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()