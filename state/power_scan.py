"""Scan a library for the absolute --power levels it sets, and check them against a
unit's calibration — so a deploy can warn about levels a unit can't produce (the agent
clips them at transmit; this is what tells the operator it happened).

Pure logic, no Qt and no network: the fleet passes in the library and a unit's fetched
calibration result. See ui/library_tab._report_deploy for how it's surfaced.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

POWER_FLAGS = ("--power", "-Power")
AMP_FLAGS = ("--amplitude", "-Amplitude", "--ampl")


def _extract_flag(args, flags) -> Optional[float]:
    """The float value of the first matching ``flags`` in a CLI arg list (last wins if
    repeated), or None if absent/unparseable."""
    val: Optional[float] = None
    args = list(args or [])
    for i, a in enumerate(args):
        if a in flags and i + 1 < len(args):
            try:
                val = float(args[i + 1])
            except (TypeError, ValueError):
                continue
    return val


def extract_power(args) -> Optional[float]:
    """The absolute ``--power`` value (dBm) in a CLI arg list, or None."""
    return _extract_flag(args, POWER_FLAGS)


def extract_amplitude(args) -> Optional[float]:
    """The baseband ``--amplitude`` value (0–1) in a CLI arg list, or None."""
    return _extract_flag(args, AMP_FLAGS)


def _scan(library, extract_fn) -> List[Tuple[str, Optional[str], float]]:
    """Every value ``extract_fn`` finds across a library's task commands and sequence
    step args, as ``[(where, signal_id, value)]``. ``signal_id`` is the task's
    ``SDR_CAL_SIGNAL_ID`` (None when it doesn't opt into calibration)."""
    sig_of = {t.name: (getattr(t, "env", None) or {}).get("SDR_CAL_SIGNAL_ID")
              for t in getattr(library, "tasks", [])}
    out: List[Tuple[str, Optional[str], float]] = []
    for t in getattr(library, "tasks", []):
        v = extract_fn(getattr(t, "command", []))
        if v is not None:
            out.append((f"task {t.name}", sig_of.get(t.name), v))
    for q in getattr(library, "sequences", []):
        for i, step in enumerate(getattr(q, "steps", [])):
            v = extract_fn(getattr(step, "args", []))
            if v is not None:
                out.append((f"sequence {q.name} · step {i + 1} ({step.task_name})",
                            sig_of.get(step.task_name), v))
    return out


def scan_absolute_power(library) -> List[Tuple[str, Optional[str], float]]:
    """Every absolute --power level a library sets (see _scan)."""
    return _scan(library, extract_power)


def scan_amplitudes(library) -> List[Tuple[str, Optional[str], float]]:
    """Every baseband --amplitude a library sets (see _scan)."""
    return _scan(library, extract_amplitude)


def amplitude_mismatch(levels, calibration) -> List[dict]:
    """Scanned --amplitude levels that differ from the amplitude the unit's calibration
    curve was measured at, as ``[{where, amp, cal_amp}]`` — power scales with amplitude,
    so a mismatch makes --power inaccurate. Signals the unit isn't calibrated for (or that
    record no amplitude) are skipped."""
    signals = (calibration or {}).get("signals") or {}
    out: List[dict] = []
    for where, sid, amp in levels:
        b = signals.get(sid) if sid else None
        cal_amp = (b or {}).get("amplitude")
        if cal_amp is None:
            continue
        if abs(float(amp) - float(cal_amp)) > 1e-9:
            out.append({"where": where, "amp": float(amp), "cal_amp": float(cal_amp)})
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
