"""
ParamForm — a reusable form of typed parameter widgets from a paramkit/argparse
schema (the shape returned by GET /scripts/{name}/params).

One widget per parameter, chosen from the schema:
  - flag            → checkbox
  - named presets   → editable dropdown (pick a preset label, or type a raw value)
  - number + step   → spinbox (bounds from min/max, unit as suffix)
  - fixed choices   → dropdown
  - anything else   → line edit (with unit/range hint, validated on demand)

Used by both the task editor and the sequence step editor, so a task and a
sequence step configure a script's parameters the exact same way.

Public API:
  set_params(specs)         build the widgets for a schema (list of param dicts)
  set_values(args) -> extra prefill widgets from a CLI arg list; returns the args
                            it didn't recognise (for a separate "extra args" field)
  build_args() -> list[str] the CLI args the current widget values produce
  validate()   -> str|None  an error message if a value is missing/bad/out-of-range
  has_params() -> bool
  changed                   pyqtSignal emitted whenever a value changes
"""
from __future__ import annotations

import shlex
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
    QSpinBox, QWidget,
)

from .theme import Palette


# ── Schema helpers (paramkit superset over the classic argparse schema) ───────

def fmt_value(v) -> str:
    """Compact command-line string for a value (2.412e9 → '2412000000')."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def fmt_duration(seconds, *, signed: bool = False, compact: bool = False) -> str:
    """Human-readable duration from a seconds value, splitting into h/min/s once it
    passes each threshold:

        default:  42 → '42 s',  90 → '1 min, 30 s',  3725 → '1 h, 2 min, 5 s'
        compact:  42 → '42s',   90 → '1m 30s',       3725 → '1h 2m 5s'  (tight axes)

    signed=True prefixes '+'/'-' (zero → '0 s'); sub-minute values keep one decimal
    when not whole ('1.5 s')."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    sign = "-" if s < 0 else ("+" if signed and s > 0 else "")
    s = abs(s)
    whole = int(s)
    frac = s - whole
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    sec_val = sec + frac
    sv = int(sec_val) if float(sec_val).is_integer() else round(sec_val, 1)
    parts = []
    if compact:
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        if sec_val or not parts:
            parts.append(f"{sv}s")
        return sign + " ".join(parts)
    if h:
        parts.append(f"{h} h")
    if m:
        parts.append(f"{m} min")
    if sec_val or not parts:
        parts.append(f"{sv} s")
    return sign + ", ".join(parts)


def num_or_none(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _flex_int(s: str) -> int:
    """Parse an int, honouring a 0x/0o/0b prefix (base 0) but also accepting a plain
    decimal with leading zeros ('08', '0123') — which base 0 rejects, an easy trap for
    an operator who pads a PRN or count. Mirrors paramkit._int_flexible so the form and
    the script agree on what an integer field accepts."""
    try:
        return int(s, 0)
    except ValueError:
        return int(s, 10)


def _typed(val_str: str, spec: dict):
    """Coerce a widget's string value to the type its schema declares (int/float),
    leaving anything else — and unparseable numbers — as the original string."""
    t = spec.get("type")
    try:
        if t == "int":
            return _flex_int(val_str)
        if t == "float":
            return float(val_str)
    except (TypeError, ValueError):
        pass
    return val_str


def resolve_preset_value(spec: dict, text: str) -> str:
    """Map a presets-combo's text (a preset label/key, or a raw value) to the
    value string that belongs on the command line."""
    text = (text or "").strip()
    if not text:
        return ""
    for p in spec.get("presets") or []:
        if text == str(p.get("label")) or text == str(p.get("key")):
            return fmt_value(p.get("value"))
    return text


def preset_label_for_value(spec: dict, val: str):
    """Reverse (for prefill): the preset label whose value equals `val`, else None."""
    fv = num_or_none(val)
    for p in spec.get("presets") or []:
        pv = p.get("value")
        if str(pv) == val or fmt_value(pv) == val or (fv is not None and pv == fv):
            return str(p.get("label"))
    return None


def range_hint(spec: dict) -> str:
    lo, hi = spec.get("min"), spec.get("max")
    if lo is None and hi is None:
        return ""
    return f"{'' if lo is None else fmt_value(lo)}..{'' if hi is None else fmt_value(hi)}"


# ── Power calibration: reflect a unit's real --power range in the form ──────────
# A transmit script declares wide/nominal --power bounds (the true range is
# unit-specific). When a task is run on a CALIBRATED unit, the agent's /calibration
# reports the resolved range for that signal; apply it to the power field so the user
# sees — and is held to — the actual acceptable min/max at the real operating plane,
# instead of guessing.

POWER_DEST = "power"
GAIN_DEST = "gain"


def find_power_index(specs: List[dict]):
    """Index of the absolute --power parameter in a spec list, or None."""
    for i, s in enumerate(specs):
        if s.get("dest") == POWER_DEST or "--power" in (s.get("flags") or []):
            return i
    return None


def find_gain_index(specs: List[dict]):
    """Index of the relative --gain parameter in a spec list, or None."""
    for i, s in enumerate(specs):
        if s.get("dest") == GAIN_DEST or "--gain" in (s.get("flags") or []):
            return i
    return None


def _compute_power_modes(specs, cal_bounds, absolute_allowed) -> List[str]:
    """Which power modes to offer, absolute first (so it's the default). Absolute (dBm)
    is offered when:
      • a specific unit is targeted (``absolute_allowed``) AND it's calibrated for the
        signal (``cal_bounds``) — the field is bounded to that unit's range; or
      • NO unit is targeted (the Library) — absolute power is the portable, plan-faithful
        quantity, entered free-form (each unit converts + clips it at transmit).
    A targeted-but-uncalibrated unit gets no absolute (nothing to convert against).
    Relative (raw gain) is offered whenever a --gain param exists."""
    has_power = find_power_index(specs) is not None
    has_gain = find_gain_index(specs) is not None
    modes: List[str] = []
    if has_power and (cal_bounds or not absolute_allowed):
        modes.append("absolute")
    if has_gain:
        modes.append("relative")
    if not modes and has_power:
        modes.append("absolute")
    return modes


def apply_power_hint(spec: dict, agg) -> dict:
    """Attach a soft, achievable-range HINT (from cached units) to an absolute --power
    field WITHOUT bounding it — the Library holds the plan's intended dBm level, and
    per-unit clipping happens at deploy/run. Returns a copy with an ``_hint`` caption and
    an appended help note. No-op when `agg` is falsy."""
    if not agg:
        return spec
    out = dict(spec)
    n = agg.get("n_units", 0)
    unit_word = "unit" if n == 1 else "units"
    any_lo, any_hi = agg.get("any_min"), agg.get("any_max")
    all_lo, all_hi = agg.get("all_min"), agg.get("all_max")
    if all_lo is not None and all_hi is not None and all_lo <= all_hi:
        hint = (f"Seen {n} {unit_word}: all reach {all_lo:g}…{all_hi:g} dBm · "
                f"at least one reaches {any_lo:g}…{any_hi:g} dBm")
    else:
        hint = (f"Seen {n} {unit_word}: their ranges don't overlap — no single dBm works "
                f"on all · at least one reaches {any_lo:g}…{any_hi:g} dBm")
    out["_hint"] = hint
    note = ("Absolute power from your plan — stored as-is; each unit converts it and "
            "clips to its own range at transmit. " + hint)
    base = (out.get("help") or "").strip()
    out["help"] = f"{base}\n\n{note}" if base else note
    out.setdefault("unit", "dBm")
    return out


def calibration_caution(has_signal: bool, targeted: bool, calibrated: bool,
                        script_calibratable: bool = True):
    """The 'no safeguards in place' caution for a power/gain form, or None when there's
    nothing to warn about. Shown so an operator knows when a value goes out raw:
      • ``script_calibratable`` — the SCRIPT declares a calibration signal, i.e. its
        power/gain is meant to be calibrated. When it doesn't, power/gain are raw by
        design (bounded only by the script's own schema) and there's no missing
        safeguard, so no caution.
      • ``has_signal`` — the TASK opts into calibration (sets SDR_CAL_SIGNAL_ID);
      • ``targeted``   — a specific unit is in play (a run/tune, or a plan/sequence
        pointed at a unit) as opposed to open Library authoring;
      • ``calibrated`` — that unit is calibrated for the signal (bounds available).
    A calibratable script whose task hasn't been assigned a signal is raw everywhere; a
    targeted-but-uncalibrated unit is raw too. Open Library authoring with a signal is
    fine — limits apply once it hits a calibrated unit (a deploy flags anything out of
    range)."""
    if not script_calibratable:
        return None
    if not has_signal:
        return ("This task's script supports calibrated power, but no calibration signal "
                "is assigned — power/gain go out RAW, with no limits protecting the "
                "hardware. Assign the signal (edit the task) to enable the limits.")
    if targeted and not calibrated:
        return ("This unit isn't calibrated for this signal, so no calibration limits "
                "apply — power/gain go out raw. Set them carefully.")
    return None


def apply_power_bounds(specs: List[dict], bounds) -> List[dict]:
    """Return a copy of `specs` with the --power param bounded to a unit's resolved
    calibration range and relabelled with its operating plane/quantity. No-op when
    `bounds` is falsy, there's no power param, or the range is incomplete.

    `bounds`: {min_power_dbm, max_power_dbm, quantity, operating_plane} (a signal
    entry from GET /calibration)."""
    i = find_power_index(specs)
    if i is None or not bounds:
        return specs
    lo, hi = bounds.get("min_power_dbm"), bounds.get("max_power_dbm")
    if lo is None or hi is None:
        return specs
    lo, hi = round(float(lo), 2), round(float(hi), 2)
    quantity = (bounds.get("quantity") or "").strip()
    plane = (bounds.get("operating_plane") or "").strip()

    out = [dict(s) for s in specs]
    sp = out[i]
    sp["min"], sp["max"] = lo, hi
    sp["type"] = "float"
    sp.setdefault("step", 0.5)                 # render as a bounded spinbox → can't overshoot
    d = sp.get("default")
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        sp["default"] = min(max(float(d), lo), hi)
    sp["unit"] = f"dBm {quantity}" if quantity and quantity.lower() != "power" else "dBm"
    where = quantity + (f" at {plane}" if plane else "") if quantity else (plane or "")
    note = f"This unit (calibrated): {lo}…{hi} dBm" + (f" — {where}." if where else ".")
    base = (sp.get("help") or "").strip()
    sp["help"] = f"{base}\n\n{note}" if base else note
    return out


def apply_gain_bounds(specs: List[dict], bounds) -> List[dict]:
    """Return a copy of `specs` with the relative --gain param bounded to a unit's
    resolved calibration gain range [min_gain_db, max_gain_db], so a gain that would
    breach a calibration limit can't be dialled in. No-op when `bounds` is falsy, there's
    no --gain param, or the gain range is incomplete."""
    i = find_gain_index(specs)
    if i is None or not bounds:
        return specs
    lo, hi = bounds.get("min_gain_db"), bounds.get("max_gain_db")
    if lo is None or hi is None:
        return specs
    lo, hi = round(float(lo), 2), round(float(hi), 2)
    out = [dict(s) for s in specs]
    sp = out[i]
    sp["min"], sp["max"] = lo, hi
    sp["type"] = "float"
    sp.setdefault("step", 0.25)                # bounded spinbox → can't breach the limit
    d = sp.get("default")
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        sp["default"] = min(max(float(d), lo), hi)
    sp.setdefault("unit", "dB")
    note = (f"This unit (calibrated): usable gain {lo}…{hi} dB — beyond this range would "
            f"break a calibration limit.")
    base = (sp.get("help") or "").strip()
    sp["help"] = f"{base}\n\n{note}" if base else note
    return out


_INT32 = 2_000_000_000


def _decimals_for(step) -> int:
    s = repr(float(step))
    if "e" in s or "E" in s:
        return 6
    if "." in s:
        return len(s.split(".", 1)[1].rstrip("0"))
    return 0


def _use_spinbox(spec: dict) -> bool:
    if not spec.get("step") or spec.get("presets"):
        return False
    if spec.get("type") not in ("int", "float"):
        return False
    if spec.get("type") == "int":
        for b in (spec.get("min"), spec.get("max")):
            if b is not None and not (-_INT32 <= b <= _INT32):
                return False
    return True


def _make_spinbox(spec: dict):
    is_int = spec.get("type") == "int"
    step = spec.get("step") or (1 if is_int else 1.0)
    lo, hi = spec.get("min"), spec.get("max")
    if is_int:
        w = QSpinBox()
        w.setRange(int(lo) if lo is not None else -_INT32,
                   int(hi) if hi is not None else _INT32)
        w.setSingleStep(max(1, int(step)))
    else:
        w = QDoubleSpinBox()
        w.setDecimals(_decimals_for(step))
        w.setRange(float(lo) if lo is not None else -1e12,
                   float(hi) if hi is not None else 1e12)
        w.setSingleStep(float(step))
    if spec.get("unit"):
        w.setSuffix(f" {spec['unit']}")
    default = spec.get("default")
    if default is not None:
        try:
            w.setValue(int(default) if is_int else float(default))
        except (TypeError, ValueError):
            pass
    return w


# ── The form widget ───────────────────────────────────────────────────────────

class ParamForm(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets: Dict[str, tuple] = {}   # dest -> (widget, spec)
        self._checks: Dict[str, QCheckBox] = {}  # dest -> include-checkbox (selectable mode)
        self._selectable = False
        # Power mode (relative gain vs absolute dBm) — see _compute_power_modes.
        self._base_specs: List[dict] = []
        self._cal_bounds = None
        self._hint_bounds = None
        self._caution = None
        self._absolute_allowed = False
        self._power_modes: List[str] = []
        self._power_mode = None
        self._form = QFormLayout(self)
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setSpacing(6)

    # ── Build ────────────────────────────────────────────────────────────────

    def set_params(self, specs: List[dict], selectable: bool = False,
                   cal_bounds=None, absolute_allowed: bool = False,
                   default_power_mode=None, hint_bounds=None, caution=None) -> None:
        """Rebuild the form for a parameter schema (clears existing widgets).

        selectable=True prefixes each row with an include checkbox: values() then
        returns only the ticked params. Used by tune steps.

        Power mode: when a script exposes both an absolute --power (dBm) and a
        relative --gain (raw dB), the form shows ONE of them plus a mode toggle.
        `absolute_allowed` (a unit is known) + `cal_bounds` (that unit is calibrated
        for the signal) unlock the Absolute option; otherwise only Relative (gain)
        is offered — which is the correct default in the Library, where no unit is
        attached. `default_power_mode` ('absolute'/'relative') picks the initial one."""
        self._base_specs = list(specs)
        self._selectable = selectable
        self._cal_bounds = cal_bounds
        self._hint_bounds = hint_bounds
        self._caution = caution
        self._absolute_allowed = absolute_allowed
        self._power_modes = _compute_power_modes(specs, cal_bounds, absolute_allowed)
        if default_power_mode in self._power_modes:
            self._power_mode = default_power_mode
        elif self._power_mode not in self._power_modes:
            self._power_mode = self._power_modes[0] if self._power_modes else None
        self._render()

    def _render(self) -> None:
        while self._form.count():
            item = self._form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._widgets.clear()
        self._checks.clear()

        # No-safeguard caution: raw power/gain with no calibration behind it (an
        # uncalibrated unit, or a task with no calibration signal). Only shown when there
        # IS a power/gain field to be careful about.
        has_power_or_gain = (find_power_index(self._base_specs) is not None
                             or find_gain_index(self._base_specs) is not None)
        if self._caution and has_power_or_gain:
            warn = QLabel("⚠ " + self._caution)
            warn.setWordWrap(True)
            warn.setStyleSheet(
                f"font-size: 11px; color: {Palette.ARMED}; font-weight: 600; "
                f"background: {Palette.ARMED_SOFT}; border: 1px solid {Palette.ARMED}; "
                f"border-radius: 6px; padding: 6px 8px;")
            self._form.addRow(warn)

        if len(self._power_modes) > 1:                 # relative/absolute chooser
            self._form.addRow(self._mode_label(), self._mode_toggle())

        specs = self._effective_specs()
        if not specs:
            note = QLabel("This script declares no parameters.")
            note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            self._form.addRow(note)
        for spec in specs:
            widget = self._widget_for(spec)
            self._widgets[spec["dest"]] = (widget, spec)
            if self._selectable:
                chk = self._check_for(spec)
                self._checks[spec["dest"]] = chk
                self._form.addRow(chk, widget)
            else:
                self._form.addRow(self._label_for(spec), widget)
            if spec.get("_hint"):                        # soft achievable-range caption
                cap = QLabel(spec["_hint"]); cap.setWordWrap(True)
                cap.setStyleSheet(f"font-size: 10px; color: {Palette.TEXT_FAINT};")
                self._form.addRow("", cap)
        self.changed.emit()

    # ── Power mode ─────────────────────────────────────────────────────────────
    def power_mode(self):
        return self._power_mode

    def _effective_specs(self) -> List[dict]:
        """The specs actually rendered: the active power/gain field (bounds applied
        for absolute) in place, the other one dropped, everything else unchanged."""
        pidx = find_power_index(self._base_specs)
        gidx = find_gain_index(self._base_specs)
        out: List[dict] = []
        for i, s in enumerate(self._base_specs):
            if i == pidx:
                if self._power_mode == "absolute":
                    if self._cal_bounds:                 # a targeted unit → hard bounds
                        out.append(apply_power_bounds([s], self._cal_bounds)[0])
                    elif self._hint_bounds:              # Library → free-form + soft hint
                        out.append(apply_power_hint(s, self._hint_bounds))
                    else:
                        out.append(s)
            elif i == gidx:
                if self._power_mode == "relative":
                    # Relative selects raw gain. If the schema gives no default, an
                    # empty field must block (don't silently fall back to the absolute
                    # default); but a --gain that HAS its own default is fine as-is.
                    g = dict(s)
                    if g.get("default") is None:
                        g["required"] = True
                    if self._cal_bounds:       # a calibrated unit → bound to its limits
                        g = apply_gain_bounds([g], self._cal_bounds)[0]
                    out.append(g)
            else:
                out.append(s)
        return out

    def _mode_label(self) -> QLabel:
        lbl = QLabel("Power mode")
        lbl.setStyleSheet(f"font-weight: 600; color: {Palette.TEXT};")
        return lbl

    def _mode_toggle(self) -> QComboBox:
        combo = QComboBox()
        abs_label = "Absolute (dBm, this unit)" if self._cal_bounds else "Absolute (dBm)"
        names = {"absolute": abs_label, "relative": "Relative (raw gain, dB)"}
        for m in self._power_modes:
            combo.addItem(names.get(m, m), m)
        combo.setCurrentIndex(self._power_modes.index(self._power_mode))
        combo.currentIndexChanged.connect(self._on_mode_changed)
        return combo

    def _on_mode_changed(self, idx: int) -> None:
        if not (0 <= idx < len(self._power_modes)):
            return
        keep = self.build_args()                # carry non-power params across
        self._power_mode = self._power_modes[idx]
        self._render()
        self.set_values(keep)
        self.changed.emit()

    def has_params(self) -> bool:
        return bool(self._widgets)

    # ── Values ───────────────────────────────────────────────────────────────

    def set_values(self, args: List[str]) -> List[str]:
        """Prefill widgets from a CLI arg list; return the args not recognised as
        one of this form's parameters (so a caller can surface them separately)."""
        flag_to_dest = {}
        for dest, (w, spec) in self._widgets.items():
            for f in spec["flags"]:
                flag_to_dest[f] = dest
        extra: List[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            dest = flag_to_dest.get(a)
            if dest is None:
                extra.append(a)
                i += 1
                continue
            w, spec = self._widgets[dest]
            # In selectable mode, prefilling a param means the caller wants it set.
            if self._selectable and dest in self._checks:
                self._checks[dest].setChecked(True)
            if spec.get("is_flag"):
                if isinstance(w, QCheckBox):
                    w.setChecked(True)
                i += 1
            elif i + 1 < len(args):
                val = args[i + 1]
                if spec.get("presets") and isinstance(w, QComboBox):
                    lbl = preset_label_for_value(spec, val)
                    w.setCurrentText(lbl if lbl is not None else val)
                elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    n = num_or_none(val)
                    if n is not None:
                        w.setValue(int(n) if isinstance(w, QSpinBox) else n)
                elif isinstance(w, QComboBox):
                    # Fixed-choice combo: setCurrentText is a no-op for a value not in
                    # the list, which would silently snap to the first choice and send
                    # THAT instead. Add the stored value so it's preserved and visible.
                    if w.findText(val) < 0:
                        w.addItem(val)
                    w.setCurrentText(val)
                elif isinstance(w, QLineEdit):
                    w.setText(val)
                i += 2
            else:
                i += 1
        return extra

    def build_args(self) -> List[str]:
        """The CLI args produced by the current widget values (params only). In
        selectable mode only TICKED params are emitted (mirrors values()/validate()),
        so an unchecked param is never carried across — e.g. a power-mode toggle in a
        tune step must not silently select params the operator left unchecked."""
        out: List[str] = []
        for dest, (w, spec) in self._widgets.items():
            if self._selectable:
                chk = self._checks.get(dest)
                if chk is None or not chk.isChecked():
                    continue
            flag = spec["flags"][0] if spec["flags"] else None
            if spec.get("is_flag"):
                if isinstance(w, QCheckBox) and w.isChecked() and flag:
                    out.append(flag)
            else:
                if spec.get("presets") and isinstance(w, QComboBox):
                    val = resolve_preset_value(spec, w.currentText())
                elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                    val = fmt_value(w.value())
                elif isinstance(w, QComboBox):
                    val = w.currentText().strip()
                else:
                    val = w.text().strip()
                if val == "":
                    continue
                if flag:
                    out += [flag, val]
                else:
                    out.append(val)
        return out

    def values(self) -> Dict[str, Any]:
        """Current widget values as a {param-name: typed-value} dict — numbers as
        numbers, choices/presets as their resolved value, flags as bool. Empty
        text fields are omitted. Used for live tuning (paramkit set-params), where
        values travel as JSON rather than CLI args."""
        out: Dict[str, Any] = {}
        for dest, (w, spec) in self._widgets.items():
            if self._selectable:
                chk = self._checks.get(dest)
                if chk is None or not chk.isChecked():
                    continue          # only ticked params are included
            if spec.get("is_flag"):
                out[dest] = bool(w.isChecked()) if isinstance(w, QCheckBox) else False
            elif spec.get("presets") and isinstance(w, QComboBox):
                raw = resolve_preset_value(spec, w.currentText())
                if raw != "":
                    out[dest] = _typed(raw, spec)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                out[dest] = w.value()
            elif isinstance(w, QComboBox):
                out[dest] = w.currentText().strip()
            else:
                txt = w.text().strip()
                if txt != "":
                    out[dest] = _typed(txt, spec)
        return out

    def validate(self) -> Optional[str]:
        """Return an error message if any value is missing/bad/out-of-range."""
        missing, bad_type, bad_range = [], [], []
        for dest, (w, spec) in self._widgets.items():
            if self._selectable:
                chk = self._checks.get(dest)
                if chk is None or not chk.isChecked():
                    continue          # unticked params aren't being set
            if spec.get("is_flag"):
                continue
            if spec.get("presets") and isinstance(w, QComboBox):
                val = resolve_preset_value(spec, w.currentText())
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                continue   # bounded stepper — always valid
            elif isinstance(w, QComboBox):
                continue   # fixed-choice dropdown — always valid
            else:
                val = w.text().strip()
            flag = spec["flags"][0] if spec["flags"] else dest
            if spec.get("required") and val == "":
                missing.append(flag)
                continue
            if val == "":
                continue
            if spec.get("type") in ("int", "float"):
                try:
                    num = _flex_int(val) if spec["type"] == "int" else float(val)
                except ValueError:
                    bad_type.append(f"{flag} ({spec['type']})")
                    continue
                lo, hi = spec.get("min"), spec.get("max")
                if (lo is not None and num < lo) or (hi is not None and num > hi):
                    unit = f" {spec['unit']}" if spec.get("unit") else ""
                    bad_range.append(f"{flag} (allowed {range_hint(spec)}{unit})")
        if missing:
            return "required parameter(s) missing: " + ", ".join(missing)
        if bad_type:
            return "invalid value for: " + ", ".join(bad_type)
        if bad_range:
            return "out of range: " + ", ".join(bad_range)
        return None

    # ── Widgets ──────────────────────────────────────────────────────────────

    def _check_for(self, spec: dict) -> QCheckBox:
        """An include checkbox used as a row's label in selectable mode. Its text
        is the parameter name + unit; ticking it means 'set this parameter'."""
        flag = spec["flags"][0] if spec["flags"] else spec["dest"]
        text = flag.lstrip("-").replace("-", " ")
        if spec.get("unit"):
            text = f"{text}  [{spec['unit']}]"
        chk = QCheckBox(text)
        if spec.get("help"):
            chk.setToolTip(spec["help"])
        chk.toggled.connect(lambda _=False: self.changed.emit())
        return chk

    def _label_for(self, spec: dict) -> QLabel:
        flag = spec["flags"][0] if spec["flags"] else spec["dest"]
        text: str = flag + (" *" if spec.get("required") else "")
        if text.startswith("--"):
            text = text[2:]
        elif text.startswith("-"):
            text = text[1:]
        text = text.replace("-", " ")
        if spec.get("unit"):
            text = f"{text}  [{spec['unit']}]"
        lbl = QLabel(text)
        if spec.get("live"):
            # Mark params the script can retune while running. Rich text so the
            # badge sits inline after the name; the plain name stays the label.
            from html import escape
            lbl.setText(
                f"{escape(text)} "
                f"<span style='color:{Palette.ACCENT}; font-size:9px; "
                f"font-weight:600; letter-spacing:0.4px;'>● LIVE</span>")
            lbl.setToolTip((spec.get("help") + "\n\n" if spec.get("help") else "")
                           + "Tunable while the task is running.")
            return lbl
        if spec.get("help"):
            lbl.setToolTip(spec["help"])
        return lbl

    def _widget_for(self, spec: dict) -> QWidget:
        default = spec.get("default")
        if spec.get("is_flag"):
            w = QCheckBox()
            w.setChecked(bool(default))
            w.stateChanged.connect(self.changed.emit)
        elif spec.get("presets"):
            w = QComboBox()
            w.setEditable(True)
            for p in spec["presets"]:
                w.addItem(str(p["label"]))
            if default is not None:
                lbl = preset_label_for_value(spec, fmt_value(default))
                w.setCurrentText(lbl if lbl is not None else fmt_value(default))
            else:
                w.setCurrentText("")
            ph = "pick a preset or type a value"
            if spec.get("unit"):
                ph += f" [{spec['unit']}]"
            rng = range_hint(spec)
            if rng:
                ph += f" ({rng})"
            if w.lineEdit() is not None:
                w.lineEdit().setPlaceholderText(ph)
            w.currentTextChanged.connect(self.changed.emit)
        elif _use_spinbox(spec):
            w = _make_spinbox(spec)
            w.valueChanged.connect(self.changed.emit)
        elif spec.get("choices"):
            w = QComboBox()
            w.addItems([str(c) for c in spec["choices"]])
            if default is not None and str(default) in [str(c) for c in spec["choices"]]:
                w.setCurrentText(str(default))
            w.currentTextChanged.connect(self.changed.emit)
        else:
            w = QLineEdit()
            if default is not None:
                w.setText(fmt_value(default))
            hint = spec.get("type") or "text"
            if spec.get("unit"):
                hint += f" [{spec['unit']}]"
            rng = range_hint(spec)
            if rng:
                hint += f" ({rng})"
            if spec.get("required"):
                hint += " (required)"
            w.setPlaceholderText(hint)
            w.textChanged.connect(self.changed.emit)
        if spec.get("help"):
            w.setToolTip(spec["help"])
        return w
