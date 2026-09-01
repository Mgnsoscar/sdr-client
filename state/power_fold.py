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

from state.power_law import parse_bridge


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


def _active_applied(a: dict) -> tuple:
    """(applied_hi, applied_lo) — the applied gain range of an active component (matches
    calkit._active_applied)."""
    lo, hi = float(a["min_db"]), float(a["max_db"])
    if a.get("sense", "attenuation") == "attenuation":
        return (-lo, -hi)
    return (hi, lo)


def _active_param_value(a: dict, applied: float) -> float:
    """The parameter value to command on the component's task for a given applied gain."""
    lo, hi = float(a["min_db"]), float(a["max_db"])
    v = -applied if a.get("sense", "attenuation") == "attenuation" else applied
    return min(max(v, lo), hi)


class PowerFold:
    """The range read-outs of a resolved artifact at a frequency. Mirrors the relevant
    parts of calkit.PowerMap so the client shows exactly the range the script will map."""

    def __init__(self, gains, powers, min_gain_db, ceiling_const,
                 hops=(), freq_limits=(), center_freq=None, gain_step_db=None,
                 actives=(), source_bias=(), reported=None, limiting=None,
                 limiting_cap=None, reported_applies=False):
        pairs = sorted(zip((float(g) for g in gains), (float(p) for p in powers)))
        self._gains = [g for g, _ in pairs]
        self._powers = [p for _, p in pairs]
        self.min_gain_db = float(min_gain_db)
        self._ceiling_const = float(ceiling_const)
        self._gain_step = float(gain_step_db) if gain_step_db and float(gain_step_db) > 0 else None
        self._hops = [([float(f) for f, _ in t], [float(d) for _, d in t]) for t in hops]
        # Each freq-dependent limit: (max_dbm, freqs, deltas, anchor_gains, anchor_powers).
        # anchor_* is None when it inverts against the shared operating anchor, or a separate
        # LIMITING curve when the operating plane is REPORTED (mirrors calkit.PowerMap).
        self._freq_limits = []
        for item in freq_limits:
            mx, t = item[0], item[1]
            anchor = item[2] if len(item) > 2 else None
            fs = [float(f) for f, _ in t]
            ds = [float(d) for _, d in t]
            if anchor:
                pairs = sorted((float(g), float(p)) for g, p in anchor)
                ag = [g for g, _ in pairs]
                ap = [p for _, p in pairs]
            else:
                ag = ap = None
            self._freq_limits.append((float(mx), fs, ds, ag, ap))
        self._center_freq = None if center_freq is None else float(center_freq)
        # Active components (programmable gain/attenuation) — empty for a plain passive chain.
        self._actives = [dict(a) for a in (actives or [])]
        # Per-unit SOURCE BIAS Δ dB(f): the SDR's own output-vs-frequency flatness, shifting
        # the measured anchor with frequency (normalized to 0 at the rep frequency). Applied to
        # the anchor everywhere — delivered power AND the limit/ceiling inversion — byte-for-byte
        # with calkit.PowerMap. Empty ⇒ no bias.
        self._bias = [(float(f), float(d)) for f, d in (source_bias or [])]
        # Power-quantity BRIDGES (docs/calibration-v2.md §13) — the reported/limiting readings
        # and a limiting-reading cap, evaluated at the operator's live parameter values so the
        # form shows exactly the number and range the script will produce. Mirrors calkit.
        self._reported = reported
        self._limiting = limiting
        self._limiting_cap = None if limiting_cap is None else float(limiting_cap)
        self._reported_applies = bool(reported_applies and reported is not None)

    # ── the fold, at a frequency ──────────────────────────────────────────────────
    def _eff(self, freq: Optional[float]) -> Optional[float]:
        return freq if freq is not None else self._center_freq

    def _reading_delta(self, bridge, params: Optional[dict]) -> float:
        """dB a bridge adds to the measured value: at the live parameter values when keyed and
        supplied, else at the representative values (mirrors calkit.PowerMap)."""
        if bridge is None:
            return 0.0
        if params and bridge.keyed_params():
            return bridge.delta_db(params)
        return bridge.rep_delta_db()

    def _reported_shift(self, params: Optional[dict]) -> float:
        return self._reading_delta(self._reported, params) if self._reported_applies else 0.0

    def keyed_params(self) -> list:
        """Task parameters the reported/limiting bridges key on — so a form knows to re-fold
        when one of these changes (as it already re-folds on a frequency change)."""
        ps: list = []
        for b in (self._reported, self._limiting):
            if b is not None:
                for p in b.keyed_params():
                    if p not in ps:
                        ps.append(p)
        return ps

    @property
    def param_dependent(self) -> bool:
        return bool(self.keyed_params())

    def _source_bias_at(self, freq: Optional[float]) -> float:
        """The source-bias shift (dB) on the anchor at ``freq`` — 0 when there's no bias."""
        if not self._bias:
            return 0.0
        return _table_at([f for f, _ in self._bias], [d for _, d in self._bias], freq)

    @property
    def has_active(self) -> bool:
        return bool(self._actives)

    def _achievable(self, freq: Optional[float], params: Optional[dict] = None):
        """Build the shared achievable-power resolver at ``freq`` (grid + active descriptors),
        in the OPERATING (measured) quantity. The reported bridge is applied by the caller on
        top. The SDR power map (components at baseline — the active baseline is already folded
        into the passive hops) feeds it; the grid adds each component's applied-gain range."""
        from state.achievable import AchievableGrid, Active
        f = self._eff(freq)
        od = self._op_delta(f)
        b = self._source_bias_at(f)
        actives = []
        for a in self._actives:
            hi, lo = _active_applied(a)
            actives.append(Active(hi, lo, a["step_db"], a.get("engage_pct", 0.0), meta=a))
        grid = AchievableGrid(
            power_for_gain=lambda g: _interp(g, self._gains, self._powers) + od + b,
            gain_for_power=lambda p: _interp(p - od - b, self._powers, self._gains),
            min_gain=self.min_gain_db, ceiling=self._ceiling(f, params),
            gain_step=self._gain_step, actives=actives)
        return grid, actives

    def snap_power(self, power: float, freq: Optional[float] = None,
                   params: Optional[dict] = None) -> float:
        """The nearest achievable delivered power to ``power`` (drives the slider's snap)."""
        dr = self._reported_shift(params)
        return self._achievable(freq, params)[0].snap(float(power) - dr) + dr

    def quantize_up(self, power: float, freq: Optional[float] = None,
                    params: Optional[dict] = None) -> float:
        dr = self._reported_shift(params)
        return self._achievable(freq, params)[0].quantize_up(float(power) - dr) + dr

    def quantize_down(self, power: float, freq: Optional[float] = None,
                      params: Optional[dict] = None) -> float:
        dr = self._reported_shift(params)
        return self._achievable(freq, params)[0].quantize_down(float(power) - dr) + dr

    def realize(self, power: float, freq: Optional[float] = None,
                params: Optional[dict] = None) -> dict:
        """SDR-first realization: ``{power_dbm, sdr_gain_db, settings}`` where settings names
        each active component's task, parameter and the value to command on it — the client
        commands those alongside the transmit task's --power. Power is in the reported
        quantity; the grid works in the operating quantity (shifted by the reported bridge)."""
        dr = self._reported_shift(params)
        grid, actives = self._achievable(freq, params)
        res = grid.realize(float(power) - dr)
        settings = []
        for act, applied in zip(actives, res["applied"]):
            a = act.meta
            settings.append({"plane": a.get("plane"), "task": a["task"], "param": a["param"],
                             "applied_db": applied,
                             "value": round(_active_param_value(a, applied), 6)})
        return {"power_dbm": res["power_dbm"] + dr, "sdr_gain_db": res["sdr_gain_db"],
                "settings": settings}

    def _op_delta(self, freq: Optional[float]) -> float:
        return sum(_table_at(fs, ds, freq) for fs, ds in self._hops)

    def _ceiling(self, freq: Optional[float], params: Optional[dict] = None) -> float:
        cap = self._ceiling_const
        b = self._source_bias_at(freq)
        for max_dbm, fs, ds, ag, ap in self._freq_limits:
            target = max_dbm - _table_at(fs, ds, freq)
            if ag is not None:                    # own (downstream) limiting curve → no bias
                cap = min(cap, _interp(target, ap, ag))
            else:                                 # shared operating anchor = the biased source
                cap = min(cap, _interp(target - b, self._powers, self._gains))
        # Ceiling on the operating node's LIMITING reading, at the live parameter value.
        if self._limiting_cap is not None:
            target = self._limiting_cap - self._reading_delta(self._limiting, params)
            cap = min(cap, _interp(target - b, self._powers, self._gains))
        return cap

    def _snap(self, gain: float, freq: Optional[float], params: Optional[dict] = None) -> float:
        lo, hi = self.min_gain_db, self._ceiling(freq, params)
        step = self._gain_step
        if not step:
            return min(max(float(gain), lo), hi)
        g = round(float(gain) / step) * step
        if g > hi:
            g = math.floor(hi / step) * step
        if g < lo:
            g = math.ceil(lo / step) * step
        return round(g, 6)

    def power_for_gain(self, gain_db: float, freq: Optional[float] = None,
                       params: Optional[dict] = None) -> float:
        f = self._eff(freq)
        g = self._snap(float(gain_db), f, params)
        op = _interp(g, self._gains, self._powers) + self._op_delta(f) + self._source_bias_at(f)
        return op + self._reported_shift(params)

    def max_gain_db(self, freq: Optional[float] = None, params: Optional[dict] = None) -> float:
        f = self._eff(freq)
        return self._snap(self._ceiling(f, params), f, params)

    def bounds_at(self, freq: Optional[float], params: Optional[dict] = None) -> dict:
        """{min_power_dbm, max_power_dbm, min_gain_db, max_gain_db} at ``freq`` (the
        artifact's representative frequency when ``freq`` is None) and the operator's live
        parameter values (representative when omitted). Power is the reported quantity."""
        f = self._eff(freq)
        hi_gain = self.max_gain_db(f, params)
        out = {
            "min_gain_db": self.min_gain_db,
            "max_gain_db": hi_gain,
            "min_power_dbm": self.power_for_gain(self.min_gain_db, f, params),
            "max_power_dbm": self.power_for_gain(hi_gain, f, params),
        }
        if self._actives:                              # active components extend the range
            dr = self._reported_shift(params)
            lo, hi = self._achievable(f, params)[0].bounds()
            out["min_power_dbm"], out["max_power_dbm"] = lo + dr, hi + dr
        return out

    def finest_step(self, freq: Optional[float] = None) -> float:
        """The finest achievable power increment across the range — the smallest of the active
        components' device steps and the SDR grid's own power step. Used for the power field's
        display resolution (decimals) so a snapped level like −55.25 renders exactly."""
        f = self._eff(freq)
        cands = [a["step_db"] for a in self._actives
                 if isinstance(a.get("step_db"), (int, float)) and a["step_db"] > 0]
        if self._gain_step:
            g = self.max_gain_db(f)
            d = abs(self.power_for_gain(g, f) - self.power_for_gain(g - self._gain_step, f))
            if d > 1e-12:
                cands.append(round(d, 6))
        return min(cands) if cands else 0.5

    @property
    def freq_dependent(self) -> bool:
        """True when --power/gain (or the ceiling) actually moves with frequency."""
        return (any(len(fs) > 1 for fs, _ in self._hops)
                or any(len(fs) > 1 for _, fs, _ds, _ag, _ap in self._freq_limits)
                or len(self._bias) > 1)

    # ── constructor ───────────────────────────────────────────────────────────────
    @classmethod
    def from_artifact(cls, art: dict) -> Optional["PowerFold"]:
        """Build from a resolved artifact dict (the ``artifact`` embedded in a
        /calibration signal summary). Returns None when the artifact carries no usable
        curve (e.g. an uncalibrated signal)."""
        if not isinstance(art, dict):
            return None
        step = art.get("gain_step_db")
        actives = art.get("active_components") or ()
        reported = limiting = None
        limiting_cap = None
        readings = art.get("readings")
        if isinstance(readings, dict):
            reported = parse_bridge(readings.get("reported"))
            lim_spec = readings.get("limiting") or {}
            limiting = parse_bridge(lim_spec)
            if lim_spec.get("max_dbm") is not None:
                limiting_cap = lim_spec["max_dbm"]
        anchor = art.get("anchor_curve")
        if anchor:                                    # v2: fold passive hops at frequency
            gains = [pt[0] for pt in anchor]
            powers = [pt[1] for pt in anchor]
            if not gains:
                return None
            hops = [h.get("delta_db_by_freq") or [] for h in art.get("passive_hops", [])]
            freq_limits = [(lim["max_dbm"], lim.get("delta_db_by_freq") or [],
                            lim.get("anchor_curve"))
                           for lim in art.get("freq_dependent_limits", [])]
            ceiling_const = art.get("gain_ceiling_db")
            if ceiling_const is None:
                ceiling_const = float("inf")          # ceiling comes purely from limits
            return cls(gains, powers, art.get("min_gain_db"), ceiling_const,
                       hops=hops, freq_limits=freq_limits,
                       center_freq=art.get("center_freq_hz"), gain_step_db=step,
                       actives=actives,
                       source_bias=art.get("source_bias_delta_by_freq") or (),
                       reported=reported, limiting=limiting, limiting_cap=limiting_cap,
                       reported_applies=True)

        curve = art.get("curve") or []                # v1: pre-flattened operating curve
        gains = [pt[0] for pt in curve]
        powers = [pt[1] for pt in curve]
        if not gains:
            return None
        return cls(gains, powers, art.get("min_gain_db"), art.get("max_gain_db"),
                   gain_step_db=step, actives=actives)


def clamp_warning(artifact: Optional[dict], freq_hz: Optional[float],
                  power_dbm: Optional[float], tol: float = 0.05,
                  params: Optional[dict] = None) -> Optional[str]:
    """A one-line caption when an absolute ``power_dbm`` can't be delivered at ``freq_hz``
    (and the operator's live ``params``) on this artifact's chain — the transmitter clamps to
    the achievable range (safe, but it delivers a different power than asked). None when the
    request is in range, the chain isn't frequency- or parameter-dependent, or either value
    is unknown. Used to warn (never block) when a tune places the frequency/parameter where
    the power can't follow."""
    fold = PowerFold.from_artifact(artifact or {})
    if (fold is None or not (fold.freq_dependent or fold.param_dependent)
            or freq_hz is None or power_dbm is None):
        return None
    b = fold.bounds_at(float(freq_hz), params)
    lo, hi, p = b["min_power_dbm"], b["max_power_dbm"], float(power_dbm)
    mhz = float(freq_hz) / 1e6
    if p > hi + tol:
        return (f"at {mhz:.3f} MHz this unit delivers at most {hi:.2f} dBm — the requested "
                f"{p:.2f} dBm will be clamped down to it.")
    if p < lo - tol:
        return (f"at {mhz:.3f} MHz this unit's floor is {lo:.2f} dBm — the requested "
                f"{p:.2f} dBm will be raised to it.")
    return None


def refold_bounds(bounds: dict, freq_hz: Optional[float],
                  params: Optional[dict] = None) -> dict:
    """Return a copy of a /calibration signal summary ``bounds`` dict with its power/gain
    range re-folded at ``freq_hz`` and the operator's live ``params`` from the embedded
    ``artifact``. No-op (the same dict) when there's no artifact or the fold isn't frequency-
    or parameter-dependent — so a constant chain keeps the resolved range. A frequency-
    dependent fold still re-folds when ``freq_hz`` is given; a parameter-dependent one
    re-folds from ``params`` even without a frequency."""
    if not isinstance(bounds, dict):
        return bounds
    fold = PowerFold.from_artifact(bounds.get("artifact") or {})
    if fold is None:
        return bounds
    if fold.freq_dependent and freq_hz is not None:
        out = dict(bounds)
        out.update(fold.bounds_at(float(freq_hz), params))
        return out
    if fold.param_dependent and params:
        out = dict(bounds)
        out.update(fold.bounds_at(freq_hz, params))
        return out
    return bounds
