"""Scan a library for the absolute --power levels it sets, and check them against a
unit's calibration — so a deploy can warn about levels a unit can't produce (the agent
clips them at transmit; this is what tells the operator it happened).

Pure logic, no Qt and no network: the fleet passes in the library and a unit's fetched
calibration result. See ui/library_tab._report_deploy for how it's surfaced.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

POWER_FLAGS = ("--power", "-Power")


def extract_power(args) -> Optional[float]:
    """The absolute ``--power`` value (dBm, float) in a CLI arg list, or None if absent
    or unparseable. The last one wins if repeated (mirrors argparse)."""
    val: Optional[float] = None
    args = list(args or [])
    for i, a in enumerate(args):
        if a in POWER_FLAGS and i + 1 < len(args):
            try:
                val = float(args[i + 1])
            except (TypeError, ValueError):
                continue
    return val


def scan_absolute_power(library) -> List[Tuple[str, Optional[str], float]]:
    """Every absolute --power level a library sets, as ``[(where, signal_id, dbm)]``.
    Covers each task's command default and each sequence step's args. ``signal_id`` is the
    task's ``SDR_CAL_SIGNAL_ID`` (None when the task doesn't opt into calibration, so its
    power can't be range-checked)."""
    sig_of = {t.name: (getattr(t, "env", None) or {}).get("SDR_CAL_SIGNAL_ID")
              for t in getattr(library, "tasks", [])}
    out: List[Tuple[str, Optional[str], float]] = []
    for t in getattr(library, "tasks", []):
        p = extract_power(getattr(t, "command", []))
        if p is not None:
            out.append((f"task {t.name}", sig_of.get(t.name), p))
    for q in getattr(library, "sequences", []):
        for i, step in enumerate(getattr(q, "steps", [])):
            p = extract_power(getattr(step, "args", []))
            if p is not None:
                out.append((f"sequence {q.name} · step {i + 1} ({step.task_name})",
                            sig_of.get(step.task_name), p))
    return out


def power_out_of_range(levels, calibration) -> List[dict]:
    """Given scanned ``levels`` and a unit's calibration result, the ones outside the
    unit's achievable range, as ``[{where, dbm, limit, side}]`` (side 'above'/'below').
    Levels whose signal the unit isn't calibrated for are skipped — we can't judge them."""
    signals = (calibration or {}).get("signals") or {}
    out: List[dict] = []
    for where, sid, dbm in levels:
        b = signals.get(sid) if sid else None
        if not b:
            continue
        lo, hi = b.get("min_power_dbm"), b.get("max_power_dbm")
        if hi is not None and dbm > float(hi):
            out.append({"where": where, "dbm": dbm, "limit": float(hi), "side": "above"})
        elif lo is not None and dbm < float(lo):
            out.append({"where": where, "dbm": dbm, "limit": float(lo), "side": "below"})
    return out
