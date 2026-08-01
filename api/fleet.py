"""
Fleet — registry of all broadcaster units and fan-out helpers.

Holds one AgentClient per unit, keyed by unit_id. Provides:
  - add/remove/get units
  - broadcast operations (panic all, poll all health) that tolerate individual
    unit failures (one unit down doesn't break the others)

Fan-out results are returned as {unit_id: result_or_exception} so the caller can
show per-unit success/failure. Nothing here is Qt-aware; the UI layer wraps these
calls in worker threads.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple

from .client import AgentClient
from . import models as m

logger = logging.getLogger(__name__)


class Fleet:
    def __init__(self, max_workers: int = 16):
        self._units: Dict[str, AgentClient] = {}
        self._max_workers = max_workers

    # ── Registry ────────────────────────────────────────────────────────────────

    def add(self, client: AgentClient) -> None:
        # Key by hostname, which is stable for the client's lifetime. unit_id is
        # NOT used as the key because warmup() changes it from hostname to the
        # agent's real id after first contact — keying by it would break any
        # lookups (e.g. UI cards) made before warmup completes.
        self._units[client.hostname] = client
        logger.info("Fleet: added unit '%s' (%s)", client.unit_id, client.hostname)

    def add_by_hostname(self, hostname: str, api_key: str = "", **kwargs) -> AgentClient:
        """Create a client for a hostname and add it. unit_id starts as hostname
        and is corrected after the first successful info() call."""
        client = AgentClient(hostname, api_key=api_key, **kwargs)
        self.add(client)
        return client

    def remove(self, hostname: str) -> None:
        client = self._units.pop(hostname, None)
        if client:
            client.close()
            logger.info("Fleet: removed unit '%s'", hostname)

    def get(self, hostname: str) -> AgentClient:
        if hostname not in self._units:
            raise KeyError(f"Unknown unit: '{hostname}'")
        return self._units[hostname]

    def units(self) -> List[AgentClient]:
        return list(self._units.values())

    def hostnames(self) -> List[str]:
        return list(self._units.keys())

    def __len__(self) -> int:
        return len(self._units)

    def __contains__(self, hostname: str) -> bool:
        return hostname in self._units

    def close(self) -> None:
        for client in self._units.values():
            client.close()
        self._units.clear()

    # ── Fan-out core ──────────────────────────────────────────────────────────────

    def _fan_out(
        self, fn: Callable[[AgentClient], object],
        units: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        """
        Run fn(client) across the chosen units (default: all) concurrently.
        Returns {hostname: result_or_exception} — keyed by the stable hostname,
        so callers (UI cards, etc.) can match results regardless of warmup state.
        Never raises — failures are captured per unit so one bad unit doesn't sink
        the batch.
        """
        targets = (
            [self._units[u] for u in units if u in self._units]
            if units is not None else list(self._units.values())
        )
        if not targets:
            return {}

        results: Dict[str, object] = {}
        try:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(targets))) as pool:
                future_to_unit = {pool.submit(fn, c): c.hostname for c in targets}
                for fut in as_completed(future_to_unit):
                    hostname = future_to_unit[fut]
                    try:
                        results[hostname] = fut.result()
                    except Exception as exc:   # noqa: BLE001 — capture everything per unit
                        results[hostname] = exc
        except RuntimeError:
            # Raised by pool.submit() when the interpreter is shutting down
            # ("cannot schedule new futures after interpreter shutdown"). Honour
            # the "never raises" contract — return whatever we managed to collect.
            pass
        return results

    # ── Broadcast operations ──────────────────────────────────────────────────────

    def health_all(self, units: Optional[List[str]] = None) -> Dict[str, bool]:
        """Reachability of each unit. Values are bool (never exceptions —
        health() swallows HTTP errors)."""
        raw = self._fan_out(lambda c: c.health(), units)
        return {u: (r if isinstance(r, bool) else False) for u, r in raw.items()}

    def warmup_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """
        Pay the first-contact cost for every unit concurrently at startup. Returns
        {unit_id: ConnectionState}. Each unit's slow first hostname resolution
        happens in parallel in the thread pool, so total wait ≈ the slowest single
        unit, not the sum. Call this once (off the UI thread) before real actions
        so subsequent calls are fast. Never raises.
        """
        return self._fan_out(lambda c: c.warmup(), units)

    def system_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """Health snapshot per unit. Values are SystemHealth or an Exception."""
        return self._fan_out(lambda c: c.system(), units)

    def info_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        return self._fan_out(lambda c: c.info(), units)

    def sdr_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        return self._fan_out(lambda c: c.sdr(), units)

    def list_runs_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """Sequence runs per unit — used to rebuild the timeline / plan grouping."""
        return self._fan_out(lambda c: c.list_sequence_runs(), units)

    def sequences_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """Sequence definitions per unit — used by the plan editor to browse what's
        available on each unit. Values are list[Sequence] or an Exception."""
        return self._fan_out(lambda c: c.list_sequences(), units)

    def tasks_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """Task status list per unit. Values are list[ProcessStatus] or Exception."""
        return self._fan_out(lambda c: c.list_tasks(), units)

    def panic_all(self, units: Optional[List[str]] = None) -> Dict[str, object]:
        """
        EMERGENCY STOP across the fleet. Returns {unit_id: PanicResult|Exception}.
        Runs concurrently so all units stop as fast as possible. The GUI should
        report any unit that returned an exception so the operator knows it may
        NOT have stopped.
        """
        logger.warning("Fleet: PANIC broadcast to %d unit(s)", len(units or self._units))
        return self._fan_out(lambda c: c.panic(), units)

    # ── Clock-sync helper (pre-flight) ────────────────────────────────────────────

    def clock_skew(self, units: Optional[List[str]] = None) -> Tuple[Dict[str, object], Optional[float]]:
        """
        Fetch utc_now from each unit and compute the max pairwise skew (seconds)
        among reachable units. Returns (per_unit_systemhealth_or_exc, max_skew_s).
        max_skew_s is None if fewer than 2 units responded. Used by the pre-flight
        clock check before arming coordinated events.
        """
        from datetime import datetime, timezone

        snaps = self.system_all(units)
        times: List[datetime] = []
        for r in snaps.values():
            if isinstance(r, m.SystemHealth) and r.utc_now:
                try:
                    dt = datetime.fromisoformat(r.utc_now)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    times.append(dt.astimezone(timezone.utc))
                except ValueError:
                    pass

        max_skew = None
        if len(times) >= 2:
            max_skew = (max(times) - min(times)).total_seconds()
        return snaps, max_skew