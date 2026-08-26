"""Fold a resolved calibration artifact to a --power / --gain range at a chosen
transmit frequency — the client-side mirror of the agent's calkit ``PowerMap``.

A frequency-dependent chain (a cable/antenna whose loss varies with frequency, or a
per-frequency safety limit) has a --power range that MOVES with the transmit frequency.
The agent's ``/calibration`` view resolves each signal at one representative frequency;
this module re-folds the bounds at whatever frequency the operator enters in a Run /
sequence form, so the displayed range is the range at THAT frequency — the same fold the
transmit script does through calkit (paramkit/calkit.py). The math here is a deliberate,
byte-for-byte port of calkit's; keep the two in step.

Only the range read-outs are needed here (min/max power and gain), not the full inverse
map, so this is a small compute over the artifact's ``anchor_curve`` + ``passive_hops`` +
``freq_dependent_limits`` (v2) or its flat ``curve`` (v1)."""
from __future__ import annotations

import math
from typing import Optional


def _interp(x: float, xs: list, ys: list) -> float:
    """Piecewise-linear y(x) over strictly-increasing xs, endpoint-clamped; a single
    sample degrades to a slope-1 line (1 dB gain ≈ 1 dB power) — matches calkit._interp."""
    n = len(xs)
    if n == 1:
        return ys[0] + (x - xs[0])
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, n):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def _table_at(freqs: list, deltas: list, freq: Optional[float]) -> float:
    """A delta table's value at ``freq``: constant if single-point; unknown frequency on a
    multi-point table falls back to its lowest-frequency value (matches calkit._table_at)."""
    if len(freqs) == 1:
        return deltas[0]
    if freq is None:
        return deltas[0]
    return _interp(freq, freqs, deltas)


class PowerFold:
    """The range read-outs of a resolved artifact at a frequency. Mirrors the relevant
    parts of calkit.PowerMap so the client shows exactly the range the script will map."""

    def __init__(self, gains, powers, min_gain_db, ceiling_const,
                 hops=(), freq_limits=(), center_freq=None, gain_step_db=None):
        pairs = sorted(zip((float(g) for g in gains), (float(p) for p in powers)))
        self._gains = [g for g, _ in pairs]
        self._powers = [p for _, p in pairs]
        self.min_gain_db = float(min_gain_db)
        self._ceiling_const = float(ceiling_const)
        self._gain_step = float(gain_step_db) if gain_step_db and float(gain_step_db) > 0 else None
        self._hops = [([float(f) for f, _ in t], [float(d) for _, d in t]) for t in hops]
        self._freq_limits = [
            (float(mx), [float(f) for f, _ in t], [float(d) for _, d in t])
            for mx, t in freq_limits]
        self._center_freq = None if center_freq is None else float(center_freq)

    # ── the fold, at a frequency ──────────────────────────────────────────────────
    def _eff(self, freq: Optional[float]) -> Optional[float]:
        return freq if freq is not None else self._center_freq

    def _op_delta(self, freq: Optional[float]) -> float:
        return sum(_table_at(fs, ds, freq) for fs, ds in self._hops)

    def _invert(self, target_power: float) -> float:
        return _interp(target_power, self._powers, self._gains)

    def _ceiling(self, freq: Optional[float]) -> float:
        cap = self._ceiling_const
        for max_dbm, fs, ds in self._freq_limits:
            cap = min(cap, self._invert(max_dbm - _table_at(fs, ds, freq)))
        return cap

    def _snap(self, gain: float, freq: Optional[float]) -> float:
        lo, hi = self.min_gain_db, self._ceiling(freq)
        step = self._gain_step
        if not step:
            return min(max(float(gain), lo), hi)
        g = round(float(gain) / step) * step
        if g > hi:
            g = math.floor(hi / step) * step
        if g < lo:
            g = math.ceil(lo / step) * step
        return round(g, 6)

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None) -> float:
        f = self._eff(freq)
        g = self._snap(float(gain_db), f)
        return _interp(g, self._gains, self._powers) + self._op_delta(f)

    def max_gain_db(self, freq: Optional[float] = None) -> float:
        f = self._eff(freq)
        return self._snap(self._ceiling(f), f)

    def bounds_at(self, freq: Optional[float]) -> dict:
        """{min_power_dbm, max_power_dbm, min_gain_db, max_gain_db} at ``freq`` (the
        artifact's representative frequency when ``freq`` is None)."""
        f = self._eff(freq)
        hi_gain = self.max_gain_db(f)
        return {
            "min_gain_db": self.min_gain_db,
            "max_gain_db": hi_gain,
            "min_power_dbm": self.power_for_gain(self.min_gain_db, f),
            "max_power_dbm": self.power_for_gain(hi_gain, f),
        }

    @property
    def freq_dependent(self) -> bool:
        """True when --power/gain (or the ceiling) actually moves with frequency."""
        return (any(len(fs) > 1 for fs, _ in self._hops)
                or any(len(fs) > 1 for _, fs, _ in self._freq_limits))

    # ── constructor ───────────────────────────────────────────────────────────────
    @classmethod
    def from_artifact(cls, art: dict) -> Optional["PowerFold"]:
        """Build from a resolved artifact dict (the ``artifact`` embedded in a
        /calibration signal summary). Returns None when the artifact carries no usable
        curve (e.g. an uncalibrated signal)."""
        if not isinstance(art, dict):
            return None
        step = art.get("gain_step_db")
        anchor = art.get("anchor_curve")
        if anchor:                                    # v2: fold passive hops at frequency
            gains = [pt[0] for pt in anchor]
            powers = [pt[1] for pt in anchor]
            if not gains:
                return None
            hops = [h.get("delta_db_by_freq") or [] for h in art.get("passive_hops", [])]
            freq_limits = [(lim["max_dbm"], lim.get("delta_db_by_freq") or [])
                           for lim in art.get("freq_dependent_limits", [])]
            ceiling_const = art.get("gain_ceiling_db")
            if ceiling_const is None:
                ceiling_const = float("inf")          # ceiling comes purely from limits
            return cls(gains, powers, art.get("min_gain_db"), ceiling_const,
                       hops=hops, freq_limits=freq_limits,
                       center_freq=art.get("center_freq_hz"), gain_step_db=step)

        curve = art.get("curve") or []                # v1: pre-flattened operating curve
        gains = [pt[0] for pt in curve]
        powers = [pt[1] for pt in curve]
        if not gains:
            return None
        return cls(gains, powers, art.get("min_gain_db"), art.get("max_gain_db"),
                   gain_step_db=step)


def refold_bounds(bounds: dict, freq_hz: Optional[float]) -> dict:
    """Return a copy of a /calibration signal summary ``bounds`` dict with its
    power/gain range re-folded at ``freq_hz`` from the embedded ``artifact``. No-op (the
    same dict) when there's no artifact, the fold isn't frequency-dependent, or ``freq_hz``
    is None — so a constant chain and a missing frequency both keep the resolved range."""
    if not isinstance(bounds, dict):
        return bounds
    fold = PowerFold.from_artifact(bounds.get("artifact") or {})
    if fold is None or not fold.freq_dependent or freq_hz is None:
        return bounds
    out = dict(bounds)
    out.update(fold.bounds_at(float(freq_hz)))
    return out
