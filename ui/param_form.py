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

import math
import shlex
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QEvent, QLocale, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from .theme import Palette, mono_font
from .param_widgets import (
    Dropdown, LimitChip, LiveBadge, RangeRail, SegmentedControl, ToggleSwitch,
    field_name_label, unit_chip,
)
from state.power_fold import PowerFold, refold_bounds


# Scoped styling for the value inputs, so a field looks like the mockup's recessed
# control (rounded inset, accent focus ring) without restyling QLineEdit/QComboBox
# app-wide. Applied to the ParamForm (objectName paramForm); it cascades to children.
_FORM_QSS = f"""
#paramForm {{ background: {Palette.SURFACE}; }}
#paramForm QLineEdit, #paramForm QComboBox, #paramForm QAbstractSpinBox {{
    background: {Palette.INSET};
    border: 1px solid {Palette.BORDER};
    border-radius: 9px;
    min-height: 34px;
    padding: 0 11px;
    color: {Palette.TEXT};
    selection-background-color: {Palette.ACCENT_SOFT};
    selection-color: {Palette.TEXT};
}}
#paramForm QLineEdit:focus, #paramForm QComboBox:focus, #paramForm QAbstractSpinBox:focus,
#paramForm QComboBox:on {{
    border: 1px solid {Palette.ACCENT};
    background: {Palette.SURFACE};
}}
#paramForm QAbstractSpinBox::up-button, #paramForm QAbstractSpinBox::down-button {{
    width: 0; border: none;
}}
#paramForm QComboBox::drop-down {{
    border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center right;
}}
#paramForm QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
#paramForm QComboBox QAbstractItemView {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 8px;
    padding: 4px;
    outline: 0;
    selection-background-color: {Palette.ACCENT_SOFT};
    selection-color: {Palette.ACCENT_INK};
}}
"""


class _WheelGuard(QObject):
    """Stops the mouse wheel from changing (or focusing) a spinbox / dropdown the pointer
    merely scrolls past: a wheel event over an unfocused field is not applied to the field
    but FORWARDED to the enclosing scroll area, so scrolling past a field scrolls the whole
    form (rather than nudging a value, stealing focus, or dead-stopping the scroll). One
    shared instance is installed on each numeric/choice widget."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            area = _enclosing_scroll_area(obj)
            if area is not None:
                # Hand the scroll to the surrounding view so the form still scrolls.
                QApplication.sendEvent(area.viewport(), event)
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def _enclosing_scroll_area(w):
    """The nearest QAbstractScrollArea ancestor of `w`, or None."""
    p = w.parent()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return p
        p = p.parent()
    return None


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


def choice_token(w) -> str:
    """The CLI token for a fixed-choice combo's selection. Each item stores its token
    as itemData (the value the script receives, as a string); a labelled choice shows
    the label as text but sends the token. Falls back to the visible text for a plain
    choice whose text *is* its token."""
    data = w.currentData()
    return str(data) if data is not None else w.currentText().strip()


def choice_typed_value(spec: dict, token: str):
    """The typed value a choice token maps to (float/int/str) for live-tune JSON.
    For a labelled choice this is choice_values[token]; a plain choice sends the token
    itself."""
    values = spec.get("choice_values") or {}
    return values.get(token, token)


def choice_default_index(spec: dict, default) -> int:
    """Index of the option matching `default` — accepting the typed value, its token,
    or its display label — or -1 if none match (or there's no default)."""
    if default is None:
        return -1
    choices = [str(c) for c in (spec.get("choices") or [])]
    labels = spec.get("choice_labels") or {}
    values = spec.get("choice_values") or {}
    dstr = str(default).strip()
    for i, tok in enumerate(choices):
        if dstr == tok:
            return i
        if tok in values and (default == values[tok] or dstr == str(values[tok])):
            return i
        if tok in labels and dstr.lower() == str(labels[tok]).strip().lower():
            return i
    return -1


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
AMP_DEST = "amplitude"

_POWER_FLAGS = ("--power", "-Power")
_GAIN_FLAGS = ("--gain", "-Gain")
_AMP_FLAGS = ("--amplitude", "-Amplitude", "--ampl")


def power_mode_of_args(args) -> Optional[str]:
    """The power mode a saved arg list was authored in: 'absolute' if it sets --power,
    'relative' if it sets --gain, else None (let the form pick its default). Absolute
    wins if somehow both are present. Used so fetching a task preserves the mode it was
    saved with instead of falling back to the form's default."""
    have = list(args or [])
    if any(a in _POWER_FLAGS for a in have):
        return "absolute"
    if any(a in _GAIN_FLAGS for a in have):
        return "relative"
    return None


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


def find_amplitude_index(specs: List[dict]):
    """Index of the baseband --amplitude parameter in a spec list, or None. This is the
    knob that must match the amplitude the calibration curve was measured at (power scales
    with it), so the form can default it to the calibrated value and flag an override."""
    for i, s in enumerate(specs):
        flags = s.get("flags") or []
        if s.get("dest") == AMP_DEST or any(f in _AMP_FLAGS for f in flags):
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
    # A resolved artifact lets the field step through the chain's *true* achievable power
    # levels (non-uniform: attenuator-only at the bottom, SDR-only at the top). Mark the
    # field so the widget wires the resolver, and set its display resolution to the finest
    # achievable increment so a snapped level like −55.25 dBm renders exactly.
    art = bounds.get("artifact") or {}
    fold = PowerFold.from_artifact(art)
    if fold is not None:
        sp["snap_role"] = "power"
        sp["step"] = fold.finest_step()
    d = sp.get("default")
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        sp["default"] = min(max(float(d), lo), hi)
    # The reported reading's unit is script-defined (docs/calibration-v2.md §13): once a node
    # is calibrated with a reported bridge, --power is shown in that unit (e.g. dBm/MHz), not
    # always dBm. `operating_unit` rides the artifact; fall back to the old "dBm <quantity>"
    # label for a plain (bridge-less) calibration.
    unit_lbl = (art.get("operating_unit") or "").strip()
    if not unit_lbl:
        unit_lbl = f"dBm {quantity}" if quantity and quantity.lower() != "power" else "dBm"
    sp["unit"] = unit_lbl
    where = quantity + (f" at {plane}" if plane else "") if quantity else (plane or "")
    note = f"This unit (calibrated): {lo}…{hi} {unit_lbl}" + (f" — {where}." if where else ".")
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
    # Step on the SDR's real gain grid when the artifact gives one, so the field lands on
    # commandable gains (SDR-only chains snap to the true grid too); else a sensible default.
    gs = ((bounds.get("artifact") or {}).get("gain_step_db"))
    sp["step"] = float(gs) if isinstance(gs, (int, float)) and gs > 0 else sp.get("step", 0.25)
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


class _AchievableSpin(QDoubleSpinBox):
    """A calibrated-power spinbox whose up/down steps land on the chain's *true* achievable
    power levels (non-uniform across the range — attenuator-only at the bottom, SDR-only at
    the top) and whose typed value snaps to the nearest achievable level on commit. With no
    snappers attached it behaves like a plain ``QDoubleSpinBox``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snap = self._qup = self._qdown = None
        self.editingFinished.connect(self._snap_typed)

    def set_snappers(self, snap, qup, qdown) -> None:
        self._snap, self._qup, self._qdown = snap, qup, qdown

    def stepBy(self, steps: int) -> None:                # arrows / scroll / page keys
        if not self._qup or not self._qdown or not steps:
            return super().stepBy(steps)
        v = float(self.value())
        nxt = self._qup if steps > 0 else self._qdown
        for _ in range(abs(int(steps))):
            nv = float(nxt(v))
            if abs(nv - v) < 1e-9:                       # already at the rail's end
                break
            v = nv
        self.setValue(v)

    def _snap_typed(self) -> None:
        if self._snap is None:
            return
        v = float(self.value())
        sv = float(self._snap(v))
        if abs(sv - v) > 1e-9:
            self.setValue(sv)


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
        w = _AchievableSpin() if spec.get("snap_role") == "power" else QDoubleSpinBox()
        w.setDecimals(_decimals_for(step))
        w.setRange(float(lo) if lo is not None else -1e12,
                   float(hi) if hi is not None else 1e12)
        w.setSingleStep(float(step))
    # Use a '.' decimal separator regardless of the OS locale (a ',' locale would otherwise
    # show — and accept — '0,00', which the scripts never use).
    w.setLocale(QLocale(QLocale.Language.C))
    if spec.get("unit"):
        w.setSuffix(f" {spec['unit']}")
    default = spec.get("default")
    if default is not None:
        try:
            w.setValue(int(default) if is_int else float(default))
        except (TypeError, ValueError):
            pass
    return w


def _fmt_bound(v) -> str:
    """Compact display of a range end: whole values without a decimal or exponent, fractional
    values trimmed, with a proper minus sign (−1.8). Module-level twin of ParamForm._fmt_bound
    so standalone bounded widgets format their rails/chips identically."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = str(int(f)) if f.is_integer() else f"{f:g}"
    return s.replace("-", "−")


class BoundedNumberField(QWidget):
    """A numeric input with a live range rail + limit chip — the parameter form's bounded
    field, reusable on its own. For a calibrated --power spec (``snap_role`` 'power' with a
    ``PowerFold``) the arrows, a rail drag and a typed commit all snap to the chain's true
    achievable levels at ``fold_freq``. Emits ``valueChanged()`` on any change. ``value()`` /
    ``setValue()`` read and write the current value."""

    valueChanged = pyqtSignal()

    def __init__(self, spec: dict, fold: Optional["PowerFold"] = None,
                 fold_freq: Optional[float] = None, note: str = "", parent=None):
        super().__init__(parent)
        self._spec = dict(spec)
        self._is_int = self._spec.get("type") == "int"
        lo, hi = self._spec.get("min"), self._spec.get("max")
        self._lo = float(lo) if lo is not None else None
        self._hi = float(hi) if hi is not None else None
        self._spin = _make_spinbox(self._spec)
        self._psnap = None
        if self._spec.get("snap_role") == "power" and fold is not None:
            self._psnap = lambda p: fold.snap_power(p, fold_freq)
            if isinstance(self._spin, _AchievableSpin):
                self._spin.set_snappers(self._psnap,
                                        lambda p: fold.quantize_up(p, fold_freq),
                                        lambda p: fold.quantize_down(p, fold_freq))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(8)
        crow.addWidget(self._spin, 1)
        self._bounded = (self._spec.get("type") in ("int", "float")
                         and self._lo is not None and self._hi is not None)
        self._chip = self._rail = self._warn = None
        if self._bounded:
            self._chip = LimitChip()
            self._chip.set_range(_fmt_bound(self._lo), _fmt_bound(self._hi))
            crow.addWidget(self._chip)
        outer.addLayout(crow)
        if self._bounded:
            self._rail = RangeRail()
            self._rail.set_bounds(self._lo, self._hi, _fmt_bound)
            if note:
                self._rail.set_note(note)
            outer.addWidget(self._rail)
            self._warn = QLabel(); self._warn.setWordWrap(True); self._warn.setVisible(False)
            self._warn.setStyleSheet(
                f"font-size: 12px; color: {Palette.ARMED}; background: {Palette.ARMED_SOFT}; "
                f"border: 1px solid {Palette.ARMED}; border-radius: 9px; padding: 8px 10px;")
            outer.addWidget(self._warn)
            self._spin.valueChanged.connect(self._on_change)
            self._rail.valueChanged.connect(self._on_rail)
            self._on_change()
        else:
            self._spin.valueChanged.connect(lambda *_: self.valueChanged.emit())

    def _on_rail(self, value: float) -> None:
        value = min(max(value, self._lo), self._hi)          # a drag can't leave the range
        if self._psnap is not None:                          # snap to a real achievable level
            value = min(max(self._psnap(value), self._lo), self._hi)
        self._spin.setValue(int(round(value)) if self._is_int else value)

    def _on_change(self, *_) -> None:
        v = float(self._spin.value())
        self._rail.set_value(v)
        over = v > self._hi + 1e-9
        under = v < self._lo - 1e-9
        self._chip.set_state(over=over, under=under)
        u = f" {self._spec['unit']}" if self._spec.get("unit") else ""
        if over:
            self._warn.setText(f"⚠ Above the maximum this unit can deliver "
                               f"({_fmt_bound(self._hi)}{u}) — the request will be clamped "
                               f"down to it.")
        elif under:
            self._warn.setText(f"⚠ Below the minimum this unit can deliver "
                               f"({_fmt_bound(self._lo)}{u}) — the request will be clamped "
                               f"up to it.")
        self._warn.setVisible(over or under)
        self.valueChanged.emit()

    def value(self) -> float:
        return float(self._spin.value())

    def setValue(self, v) -> None:
        try:
            self._spin.setValue(int(round(float(v))) if self._is_int else float(v))
        except (TypeError, ValueError):
            pass


# ── The form widget ───────────────────────────────────────────────────────────

class ParamForm(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets: Dict[str, tuple] = {}   # dest -> (widget, spec)
        self._checks: Dict[str, QCheckBox] = {}  # dest -> include-checkbox (selectable mode)
        self._selectable = False
        # Conditional visibility (show_when) + derived (read-only computed) fields.
        self._cond_values: Dict[str, Any] = {}   # controller dest -> current value (for show_when)
        self._derived: Dict[str, dict] = {}       # dest -> {spec, value_lbl, chip, warn}
        self._rebuilding_cond = False             # re-entrancy guard for a show_when rebuild
        # Power mode (relative gain vs absolute dBm) — see _compute_power_modes.
        self._base_specs: List[dict] = []
        self._cal_bounds = None
        self._cal_freq_param = None             # dest of the freq field (CAL_FREQ_PARAM)
        self._cal_freq_default = None           # freq to fold at when the field isn't set
        self._folded_at = None                  # freq the power/gain bounds were last folded at
        self._render_freq = None                # freq to fold at for the in-progress render
        self._folded_params = None              # bridge params the bounds were last folded at
        self._render_params = None              # bridge params to fold at for this render
        self._refolding = False                 # re-entrancy guard for the re-fold re-render
        self._loading = False                   # a programmatic prefill (set_values) is running
        self._hint_bounds = None
        self._caution = None
        self._cal_amplitude = None              # amplitude the calibration curve assumes
        self._amp_warn = None                   # the live amplitude-mismatch caption
        self._absolute_allowed = False
        self._power_modes: List[str] = []
        self._power_mode = None
        self._wheel_guard = _WheelGuard(self)   # eats stray wheel events over unfocused fields
        self.setObjectName("paramForm")
        # WA_StyledBackground lets the #paramForm { background } rule actually paint the
        # form's own surface (a plain QWidget otherwise ignores a stylesheet background),
        # so an embedded form reads white like the Run dialog instead of the grey viewport.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_FORM_QSS)
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)          # per-field frames own their own spacing
        # Keep the amplitude-mismatch caption in step with edits (and with set_values).
        self.changed.connect(self._update_amplitude_warning)

    # ── Build ────────────────────────────────────────────────────────────────

    def set_params(self, specs: List[dict], selectable: bool = False,
                   cal_bounds=None, absolute_allowed: bool = False,
                   default_power_mode=None, hint_bounds=None, caution=None,
                   cal_freq_param=None, cal_freq_default=None) -> None:
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
        self._cal_freq_param = cal_freq_param
        # A carried step frequency arrives in the freq field's own unit (e.g. MHz); fold
        # frequencies are Hz internally, so scale it once here (needs _base_specs +
        # _cal_freq_param, both set above).
        self._cal_freq_default = (cal_freq_default * self._freq_unit_factor()
                                  if cal_freq_default is not None else None)
        self._folded_at = None
        # Fold at the carried-forward frequency (a sequence step's effective freq) when
        # given, else the freq field's own default (both already in Hz).
        self._render_freq = (self._cal_freq_default if self._cal_freq_default is not None
                             else self._spec_default_freq())
        self._hint_bounds = hint_bounds
        self._caution = caution
        self._cal_amplitude = (cal_bounds or {}).get("amplitude")
        self._absolute_allowed = absolute_allowed
        self._power_modes = _compute_power_modes(specs, cal_bounds, absolute_allowed)
        if default_power_mode in self._power_modes:
            self._power_mode = default_power_mode
        elif self._power_mode not in self._power_modes:
            self._power_mode = self._power_modes[0] if self._power_modes else None
        # Seed show_when controller values from the controlling fields' defaults, so the
        # first render shows the right mode's fields (a controller is any dest named as a
        # key in some field's show_when).
        controllers = {k for s in specs for k in (s.get("show_when") or {})}
        self._cond_values = {s["dest"]: s.get("default")
                             for s in specs if s.get("dest") in controllers}
        self._render()

    def _render(self) -> None:
        self._clear_layout(self._body)
        self._widgets.clear()
        self._checks.clear()
        self._derived.clear()

        # No-safeguard caution: raw power/gain with no calibration behind it (an
        # uncalibrated unit, or a task with no calibration signal). Only shown when there
        # IS a power/gain field to be careful about.
        has_power_or_gain = (find_power_index(self._base_specs) is not None
                             or find_gain_index(self._base_specs) is not None)
        if self._caution and has_power_or_gain:
            self._body.addWidget(self._caution_banner(self._caution))
            self._body.addSpacing(8)

        if len(self._power_modes) > 1:                 # relative/absolute chooser
            self._body.addWidget(self._mode_segments())
            self._body.addSpacing(12)

        self._amp_warn = None
        aidx = find_amplitude_index(self._base_specs)
        amp_dest = self._base_specs[aidx]["dest"] if aidx is not None else None
        specs = self._effective_specs()
        if not specs:
            note = QLabel("This script declares no parameters.")
            note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT}; padding: 6px 0;")
            self._body.addWidget(note)
        first = True
        for spec in specs:
            if spec.get("kind") == "derived":
                # A read-only computed readout (e.g. the carrier / sweep width a
                # start/stop pair implies). Never an input — not in self._widgets.
                self._body.addWidget(self._derived_frame(spec, top_sep=not first))
                first = False
                continue
            widget = self._widget_for(spec)
            self._widgets[spec["dest"]] = (widget, spec)
            chk = None
            if self._selectable:
                chk = self._check_for(spec)
                self._checks[spec["dest"]] = chk
            self._body.addWidget(self._field_frame(spec, widget, chk, top_sep=not first))
            first = False
            if spec.get("_hint"):                        # soft achievable-range caption
                cap = QLabel(spec["_hint"]); cap.setWordWrap(True)
                cap.setStyleSheet(f"font-size: 10px; color: {Palette.TEXT_FAINT}; padding: 2px 2px 0;")
                self._body.addWidget(cap)
            # Live amplitude-mismatch caption, right under the amplitude field.
            if spec["dest"] == amp_dest and self._cal_amplitude is not None:
                self._amp_warn = QLabel(""); self._amp_warn.setVisible(False)
                self._amp_warn.setWordWrap(True)
                self._amp_warn.setStyleSheet(
                    f"font-size: 10px; color: {Palette.ARMED}; font-weight: 600; padding: 2px 2px 0;")
                self._body.addWidget(self._amp_warn)
        self._body.addStretch(1)          # keep fields compact + top-aligned
        self._wire_conditional()          # controllers rebuild the form; derived sources recompute
        self._wire_freq_refold()
        self._recompute_derived()
        self.changed.emit()

    # ── Layout building blocks (mockup field frames) ───────────────────────────

    @staticmethod
    def _clear_layout(layout) -> None:
        """Tear down every item in a layout (widgets and nested layouts) so a
        re-render starts clean."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)     # drop from the tree now (deleteLater is deferred),
                w.deleteLater()       # so findChildren / a re-render never see the old one
                continue
            child = item.layout()
            if child is not None:
                ParamForm._clear_layout(child)

    @staticmethod
    def _hairline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {Palette.BORDER}; border: none;")
        return line

    @staticmethod
    def _fmt_bound(v) -> str:
        """Compact display of a range end: whole values without a decimal or exponent
        (1575420000, not 1.57542e+09), fractional values trimmed (61.44), with a proper
        minus sign (−1.8)."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        s = str(int(f)) if f.is_integer() else f"{f:g}"
        return s.replace("-", "−")

    def _display_name(self, spec: dict) -> str:
        """The field's human name: the long flag without dashes, spaces for hyphens,
        a trailing * when required (units live in their own chip)."""
        flag = spec["flags"][0] if spec.get("flags") else spec.get("dest", "")
        name = flag.lstrip("-").replace("-", " ")
        return name + (" *" if spec.get("required") else "")

    @staticmethod
    def _is_power_or_gain(spec: dict) -> bool:
        flags = spec.get("flags") or []
        return (spec.get("dest") in (POWER_DEST, GAIN_DEST)
                or "--power" in flags or "--gain" in flags)

    def _caution_banner(self, text: str) -> QLabel:
        warn = QLabel("⚠ " + text)
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"font-size: 11px; color: {Palette.ARMED}; font-weight: 600; "
            f"background: {Palette.ARMED_SOFT}; border: 1px solid {Palette.ARMED}; "
            f"border-radius: 9px; padding: 8px 10px;")
        return warn

    def _mode_segments(self) -> SegmentedControl:
        """The Absolute / Relative power-mode chooser as a segmented control with a
        sliding thumb (replaces the old dropdown; same _on_mode_changed logic)."""
        subs = {
            "absolute": "dBm · this unit" if self._cal_bounds else "dBm",
            "relative": "raw gain · dB",
        }
        names = {"absolute": "Absolute", "relative": "Relative"}
        items = [(names.get(m, m), subs.get(m, "")) for m in self._power_modes]
        seg = SegmentedControl(items)
        seg.setCurrentIndex(self._power_modes.index(self._power_mode), animate=False)
        seg.changed.connect(self._on_mode_changed)
        return seg

    def _field_frame(self, spec: dict, widget: QWidget, chk, top_sep: bool) -> QWidget:
        """One field, laid out like the mockup: a name row (uppercase name + unit chip +
        live badge), a control row (input + always-visible limit chip), and — for a
        bounded numeric — a range rail (with a frequency note on a freq-dependent power
        field) and a clamp warning."""
        frame = QWidget()
        # Minimum vertical policy: a field keeps (at least) its content height and is
        # never squeezed to overlap its neighbour when the form is taller than the view
        # (the scroll area then scrolls, as it should).
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 2 if not top_sep else 0, 0, 9)
        v.setSpacing(5)
        if top_sep:
            v.addWidget(self._hairline())

        # name row
        lrow = QHBoxLayout(); lrow.setContentsMargins(0, 0, 0, 0); lrow.setSpacing(8)
        if chk is not None:
            chk.setText("")                       # the name is shown separately
            lrow.addWidget(chk)
        lrow.addWidget(field_name_label(self._display_name(spec)))
        if spec.get("unit"):
            lrow.addWidget(unit_chip(spec["unit"].replace(" ", " · ")))
        lrow.addStretch(1)
        if spec.get("live"):
            lrow.addWidget(LiveBadge())
        v.addLayout(lrow)

        # control row
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(8)
        crow.addWidget(widget, 1)
        lo, hi = spec.get("min"), spec.get("max")
        # A range rail + limit chip belong to a numeric input the operator types into —
        # not a preset/choice dropdown that happens to carry min/max (e.g. a frequency
        # picker). Gate on the actual widget being a spinbox or line edit.
        bounded = (spec.get("type") in ("int", "float") and lo is not None and hi is not None
                   and isinstance(widget, (QSpinBox, QDoubleSpinBox, QLineEdit)))
        chip = None
        if bounded:
            chip = LimitChip()
            chip.set_range(self._fmt_bound(lo), self._fmt_bound(hi))
            crow.addWidget(chip)
        v.addLayout(crow)

        if bounded:
            rail = RangeRail()
            rail.set_bounds(float(lo), float(hi), self._fmt_bound)
            # On a frequency-dependent calibration, the power/gain range moves with the
            # carrier — note the frequency it was folded at (the note lives inside the rail
            # and is rebuilt, so re-folded, when the frequency changes).
            if (self._is_power_or_gain(spec) and self._is_freq_dependent()
                    and isinstance(self._folded_at, (int, float))):
                mhz = self._folded_at / 1e6
                rail.set_note(
                    f'Range at <span style="color:{Palette.ACCENT_INK}; font-weight:600;">'
                    f'{mhz:.2f} MHz</span> · moves with frequency')
            v.addWidget(rail)
            warn = self._warn_line()
            v.addWidget(warn)
            self._wire_rail(widget, spec, rail, chip, warn)
        return frame

    def _warn_line(self) -> QLabel:
        """A hidden clamp warning; _wire_rail fills in the message and shows it when a
        value is above the max or below the min."""
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setVisible(False)
        lbl.setStyleSheet(
            f"font-size: 12px; color: {Palette.ARMED}; background: {Palette.ARMED_SOFT}; "
            f"border: 1px solid {Palette.ARMED}; border-radius: 9px; padding: 8px 10px;")
        return lbl

    def _wire_rail(self, widget, spec: dict, rail: "RangeRail", chip, warn) -> None:
        """Keep a bounded field's rail, limit chip and clamp warning in step with the
        value the operator types/steps, and let a drag on the rail set the value back
        into the field's input widget (which then re-emits and refreshes everything)."""
        lo, hi = float(spec["min"]), float(spec["max"])
        is_int = spec.get("type") == "int"
        step = spec.get("step")
        step = float(step) if isinstance(step, (int, float)) and step > 0 else None
        u = f" {spec['unit']}" if spec.get("unit") else ""
        # On the calibrated power field a rail drag snaps to a true achievable level (the same
        # non-uniform grid the arrows step), not the decoupled uniform step.
        _sn = self._power_snappers() if spec.get("snap_role") == "power" else None
        psnap = _sn[0] if _sn else None

        def read():
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                return float(widget.value())
            if isinstance(widget, QLineEdit):
                return num_or_none(widget.text().strip())
            return None

        def set_widget(value):
            value = min(max(value, lo), hi)                  # a drag can't leave the range
            if psnap is not None:                            # snap to a real achievable level
                value = min(max(psnap(value), lo), hi)
            elif step:                                       # snap to the script's step grid
                value = min(max(lo + round((value - lo) / step) * step, lo), hi)
            if isinstance(widget, QSpinBox):
                widget.setValue(int(round(value)))
            elif isinstance(widget, QDoubleSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QLineEdit):
                widget.setText(str(int(round(value))) if is_int else f"{value:g}")

        def update(*_):
            v = read()
            rail.set_value(v)
            over = v is not None and v > hi + 1e-9
            under = v is not None and v < lo - 1e-9
            if chip is not None:
                chip.set_state(over=over, under=under)
            if warn is not None:
                if over:
                    warn.setText(
                        f"⚠ Above the maximum this unit can deliver "
                        f"({self._fmt_bound(hi)}{u}) — the request will be clamped down to it.")
                elif under:
                    warn.setText(
                        f"⚠ Below the minimum this unit can deliver "
                        f"({self._fmt_bound(lo)}{u}) — the request will be clamped up to it.")
                warn.setVisible(over or under)

        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.valueChanged.connect(update)
        elif isinstance(widget, QLineEdit):
            widget.textChanged.connect(update)
        rail.valueChanged.connect(set_widget)                # drag → field value → update
        update()

    def _is_freq_dependent(self) -> bool:
        """True when this signal's --power/gain range actually moves with frequency (a
        frequency-dependent cable/antenna or per-frequency limit in the embedded artifact).
        A constant chain never re-folds, so no wiring / re-render churn."""
        fold = PowerFold.from_artifact((self._cal_bounds or {}).get("artifact") or {})
        return fold is not None and fold.freq_dependent

    def _param_dependent(self) -> bool:
        """True when a reported/limiting bridge keys on a task parameter present in this
        schema, so the --power range/number move as that parameter is tuned (e.g. a chirp's
        full-bandwidth-power law keyed on --bw)."""
        return bool(self._bridge_param_dests())

    def _bridge_param_dests(self) -> List[str]:
        """The dests of the fields a reported/limiting bridge keys on (present in the schema)."""
        fold = PowerFold.from_artifact((self._cal_bounds or {}).get("artifact") or {})
        if fold is None:
            return []
        return [p for p in fold.keyed_params()
                if any(s.get("dest") == p for s in self._base_specs)]

    def _live_params(self) -> Optional[dict]:
        """The current values of the bridge's keyed parameters, or None when they can't all be
        resolved as numbers (the fold then uses the law's representative value). Read from the
        live fields, falling back to each field's schema default (for the first render)."""
        dests = self._bridge_param_dests()
        if not dests:
            return None
        cur = self.values() if self._widgets else {}
        out = {}
        for d in dests:
            v = cur.get(d)
            if v is None:
                v = next((s.get("default") for s in self._base_specs
                          if s.get("dest") == d), None)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                return None
            out[d] = float(v)
        return out

    def _power_snappers(self):
        """``(snap, quantize_up, quantize_down)`` for the calibrated --power field, bound to
        the resolved artifact and the frequency + bridge parameters this render folds at, so
        the widget steps through only achievable power levels (universal — SDR-only chains
        snap to the real gain grid too). None when there's no usable fold."""
        fold = PowerFold.from_artifact((self._cal_bounds or {}).get("artifact") or {})
        if fold is None:
            return None
        f = self._render_freq
        pr = self._render_params
        return (lambda p: fold.snap_power(p, f, pr),
                lambda p: fold.quantize_up(p, f, pr),
                lambda p: fold.quantize_down(p, f, pr))

    @staticmethod
    def _connect_commit(w, cb) -> None:
        """Re-fold on COMMIT (preset pick, Enter, or focus-out), never per keystroke — a
        re-render mid-typing would steal focus."""
        if isinstance(w, QComboBox):
            w.currentIndexChanged.connect(cb)
            if w.isEditable() and w.lineEdit() is not None:
                w.lineEdit().editingFinished.connect(cb)
        elif isinstance(w, (QSpinBox, QDoubleSpinBox, QLineEdit)):
            w.editingFinished.connect(cb)

    def _wire_freq_refold(self) -> None:
        """Connect the fold inputs' change signals so the power/gain bounds re-fold: the
        frequency source (frequency-dependent chains) and any field a reported/limiting bridge
        keys on (parameter-dependent chains, e.g. a chirp's --bw). When the frequency source is
        a derived midpoint (start/stop mode), its input fields drive the re-fold."""
        # Bridge-keyed parameter fields (e.g. --bw): re-fold when one is committed.
        for pdest in self._bridge_param_dests():
            if pdest in self._widgets:
                self._connect_commit(self._widgets[pdest][0], self._on_freq_changed)
                if self._selectable and pdest in self._checks:
                    self._checks[pdest].toggled.connect(self._on_freq_changed)
        if not self._is_freq_dependent():
            return
        dest = self._freq_source_dest()
        if dest is None:
            return
        if dest in self._derived:
            for src in self._formula_sources(self._derived[dest]["spec"]):
                if src in self._widgets:
                    self._connect_commit(self._widgets[src][0], self._on_freq_changed)
            return
        if dest not in self._widgets:
            return
        self._connect_commit(self._widgets[dest][0], self._on_freq_changed)
        # In tune (selectable) mode, ticking the freq param on/off changes whether it
        # overrides the carried-forward frequency, so re-fold on that too.
        if self._selectable and dest in self._checks:
            self._checks[dest].toggled.connect(self._on_freq_changed)

    def _on_freq_changed(self, *_) -> None:
        """Re-fold the power/gain bounds when a fold input (the transmit frequency, or a
        bridge-keyed parameter such as --bw) is committed."""
        if self._refolding or self._loading:
            return
        if self._power_mode not in ("absolute", "relative"):
            return
        # nothing actually moved (neither frequency nor a bridge parameter)
        if (self._fold_freq_now() == self._folded_at
                and self._live_params() == self._folded_params):
            return
        self._do_refold()

    def _maybe_refold_after_load(self) -> None:
        """After a programmatic prefill (set_values), re-fold once if the loaded frequency or
        a bridge parameter differs from what the bounds were folded at during the last render."""
        if self._refolding or self._loading:
            return
        if self._power_mode not in ("absolute", "relative"):
            return
        freq_dep = self._is_freq_dependent() and self._freq_source_dest() is not None
        if not freq_dep and not self._param_dependent():
            return
        if (self._fold_freq_now() == self._folded_at
                and self._live_params() == self._folded_params):
            return
        self._do_refold()

    def _do_refold(self) -> None:
        """Re-render folding at the frequency and bridge parameters now in effect, preserving
        the other field values (the same rebuild-and-restore the power-mode toggle uses)."""
        self._render_freq = self._fold_freq_now()      # capture BEFORE the widgets clear
        self._render_params = self._live_params()
        self._refolding = True
        try:
            keep = self.build_args()
            self._render()
            self._set_values(keep)
        finally:
            self._refolding = False
        self.changed.emit()

    def _update_amplitude_warning(self) -> None:
        """Show a caption when the baseband amplitude differs from the value the
        calibration curve assumes — power scales with amplitude, so a mismatch makes
        --power (and the reported power) inaccurate until you recalibrate at it."""
        lbl = self._amp_warn
        if lbl is None or self._cal_amplitude is None:
            return
        aidx = find_amplitude_index(self._base_specs)
        dest = self._base_specs[aidx]["dest"] if aidx is not None else None
        val = self.values().get(dest) if dest else None
        if isinstance(val, (int, float)) and not isinstance(val, bool) \
                and abs(float(val) - float(self._cal_amplitude)) > 1e-9:
            lbl.setText(
                f"⚠ Amplitude {float(val):g} differs from the calibrated "
                f"{float(self._cal_amplitude):g} — the gain→power curve was measured at "
                f"{float(self._cal_amplitude):g}, so --power will be off. Match it, or "
                f"recalibrate the signal at {float(val):g}.")
            lbl.setVisible(True)
        else:
            lbl.setVisible(False)

    # ── Power mode ─────────────────────────────────────────────────────────────
    def power_mode(self):
        return self._power_mode

    def _freq_dest(self) -> Optional[str]:
        """The dest of the frequency field the calibration folds at (CAL_FREQ_PARAM),
        if the script declared one and that param exists in this schema."""
        fp = self._cal_freq_param
        if not fp:
            return None
        return next((s["dest"] for s in self._base_specs if s.get("dest") == fp), None)

    def _freq_source_dest(self) -> Optional[str]:
        """The dest of the currently-RENDERED field that carries the fold frequency: the
        CAL_FREQ_PARAM field when it's visible, else a visible derived field flagged
        is_freq (e.g. a start/stop midpoint when the centre field is hidden by a mode)."""
        fp = self._cal_freq_param
        if fp and fp in self._widgets:
            return fp
        for dest, info in self._derived.items():
            if info["spec"].get("is_freq"):
                return dest
        return None

    def _freq_unit_factor(self) -> float:
        """Hz per unit of the calibration frequency field, so a field value in its own unit
        (Hz / kHz / MHz / GHz) converts to Hz — which is what the fold (refold_bounds,
        power snapping) and the 'range at N MHz' note all expect. The unit is the
        CAL_FREQ_PARAM field's; when a mode hides it, a derived is_freq field's (they carry
        the same frequency unit). Unknown/absent unit ⇒ 1.0 (treat as Hz), the old behaviour."""
        fp = self._cal_freq_param
        unit = None
        fallback = None
        for s in self._base_specs:
            u = (s.get("unit") or "").strip().lower()
            if s.get("dest") == fp:
                unit = u
                break
            if s.get("is_freq") and fallback is None:
                fallback = u
        return {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}.get((unit or fallback or "hz"), 1.0)

    def _current_freq_hz(self) -> Optional[float]:
        """The transmit frequency in Hz the form is currently at, from the active freq source
        (the freq field, or a derived is_freq midpoint), or None when unset/unparseable. The
        field's value is in its own unit (e.g. MHz), so it is scaled to Hz here."""
        dest = self._freq_source_dest()
        if dest is None:
            return None
        if dest in self._derived:
            v = self._eval_formula(self._derived[dest]["spec"].get("formula"))
        else:
            val = self.values().get(dest)
            v = (float(val) if isinstance(val, (int, float)) and not isinstance(val, bool)
                 else None)
        return None if v is None else v * self._freq_unit_factor()

    def _fold_freq_now(self) -> Optional[float]:
        """The frequency to fold the power/gain range at right now: the active freq
        source's value when set (in selectable/tune mode a plain freq field must be
        ticked; a derived source is always active), else the carried-forward frequency."""
        dest = self._freq_source_dest()
        if dest is not None:
            active = (dest in self._derived
                      or not self._selectable
                      or (dest in self._checks and self._checks[dest].isChecked()))
            if active:
                v = self._current_freq_hz()
                if v is not None:
                    return v
        return self._cal_freq_default

    def _spec_default_freq(self) -> Optional[float]:
        """The freq field's default from the schema, in Hz — the fold frequency for the FIRST
        render, before the widget (and any prefilled value) exists."""
        dest = self._freq_dest()
        if dest is None:
            return None
        for s in self._base_specs:
            if s.get("dest") == dest:
                d = s.get("default")
                if isinstance(d, (int, float)) and not isinstance(d, bool):
                    return float(d) * self._freq_unit_factor()
                return None
        return None

    def _effective_cal_bounds(self):
        """The calibration bounds re-folded at ``_render_freq`` — the frequency captured
        for this render — so a frequency-dependent chain's --power / --gain range tracks the
        chosen frequency (the same fold the transmit script does). The specs are computed
        while the widgets are being rebuilt, so the frequency can't be read from the field
        here; the caller stamps ``_render_freq`` first. A no-op for a constant chain, a blank
        frequency, or a summary with no embedded artifact."""
        if not self._cal_bounds:
            return self._cal_bounds
        self._folded_at = self._render_freq
        self._folded_params = self._render_params
        return refold_bounds(self._cal_bounds, self._render_freq, self._render_params)

    def _effective_specs(self) -> List[dict]:
        """The specs actually rendered: the active power/gain field (bounds applied
        for absolute) in place, the other one dropped, everything else unchanged."""
        pidx = find_power_index(self._base_specs)
        gidx = find_gain_index(self._base_specs)
        aidx = find_amplitude_index(self._base_specs)
        cal_bounds = self._effective_cal_bounds()
        out: List[dict] = []
        for i, s in enumerate(self._base_specs):
            if not self._show_when_visible(s):
                continue                    # hidden by its mode → not rendered/emitted
            if i == aidx and self._cal_amplitude is not None:
                # Default the baseband amplitude to the value the calibration was measured
                # at, so a fresh form matches the curve (an override is flagged live).
                out.append({**s, "default": self._cal_amplitude})
            elif i == pidx:
                if self._power_mode == "absolute":
                    if cal_bounds:                       # a targeted unit → hard bounds
                        out.append(apply_power_bounds([s], cal_bounds)[0])
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
                    if cal_bounds:             # a calibrated unit → bound to its limits
                        g = apply_gain_bounds([g], cal_bounds)[0]
                    out.append(g)
            else:
                out.append(s)
        return out

    def _on_mode_changed(self, idx: int) -> None:
        if not (0 <= idx < len(self._power_modes)):
            return
        keep = self.build_args()                # carry non-power params across
        self._power_mode = self._power_modes[idx]
        self._render_freq = self._fold_freq_now()       # fold at the effective frequency
        self._render()
        self.set_values(keep)
        self.changed.emit()

    def has_params(self) -> bool:
        return bool(self._widgets)

    # ── Conditional visibility (show_when) + derived (computed) fields ──────────

    def _show_when_visible(self, spec: dict) -> bool:
        """True if `spec` should be rendered given the current controller values. A
        field with no show_when is always shown; otherwise EVERY named controller's
        current value must be (one of) the listed value(s)."""
        cond = spec.get("show_when")
        if not cond:
            return True
        for ctrl, allowed in cond.items():
            cur = self._cond_values.get(ctrl)
            allow = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
            if cur in allow or str(cur) in [str(a) for a in allow]:
                continue
            return False
        return True

    def _control_value(self, dest: str):
        """The current value of a controller field (for show_when comparison)."""
        if dest not in self._widgets:
            return self._cond_values.get(dest)
        w, spec = self._widgets[dest]
        if spec.get("is_flag"):
            return bool(w.isChecked()) if hasattr(w, "isChecked") else False
        if isinstance(w, QComboBox):
            return choice_token(w)
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return w.value()
        if isinstance(w, QLineEdit):
            return w.text().strip()
        return None

    def _wire_conditional(self) -> None:
        """After a render: rebuild the form when a controller (a dest named in some
        show_when) changes, and recompute the derived readouts when their source
        fields change."""
        controllers = {k for s in self._base_specs for k in (s.get("show_when") or {})}
        for dest in controllers:
            if dest not in self._widgets:
                continue
            w, spec = self._widgets[dest]
            cb = lambda *a, d=dest: self._on_condition_changed(d)
            if isinstance(w, QComboBox):
                w.currentIndexChanged.connect(cb)
            elif spec.get("is_flag") and hasattr(w, "toggled"):
                w.toggled.connect(cb)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(cb)
            elif isinstance(w, QLineEdit):
                w.editingFinished.connect(cb)
        srcs = set()
        for info in self._derived.values():
            srcs |= set(self._formula_sources(info["spec"]))
        for dest in srcs:
            if dest not in self._widgets:
                continue
            w, _spec = self._widgets[dest]
            if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(self._recompute_derived)
            elif isinstance(w, QLineEdit):
                w.textChanged.connect(self._recompute_derived)
            elif isinstance(w, QComboBox):
                w.currentTextChanged.connect(self._recompute_derived)

    def _on_condition_changed(self, dest: str) -> None:
        """A controller changed → rebuild the form for the new mode, preserving the
        other fields' values (the same rebuild-and-restore the power-mode toggle uses)."""
        if self._rebuilding_cond or self._loading:
            return
        newv = self._control_value(dest)
        if self._cond_values.get(dest) == newv:
            return
        self._cond_values[dest] = newv
        self._render_freq = self._fold_freq_now()      # capture before the widgets clear
        self._rebuilding_cond = True
        try:
            keep = self.build_args()
            self._render()
            self._set_values(keep)
        finally:
            self._rebuilding_cond = False
        self.changed.emit()

    @staticmethod
    def _formula_sources(spec: dict) -> List[str]:
        """The field names a derived spec's formula reads from."""
        out: List[str] = []
        for args in (spec.get("formula") or {}).values():
            if isinstance(args, (list, tuple)):
                out.extend(str(a) for a in args)
        return out

    def _source_num(self, dest: str) -> Optional[float]:
        """The current numeric value of a field a derived formula reads, or None."""
        if dest not in self._widgets:
            return None
        w, spec = self._widgets[dest]
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return float(w.value())
        if isinstance(w, QLineEdit):
            return num_or_none(w.text().strip())
        if isinstance(w, QComboBox):
            if spec.get("presets"):
                return num_or_none(resolve_preset_value(spec, w.currentText()))
            return num_or_none(choice_token(w))
        return None

    def _eval_formula(self, formula) -> Optional[float]:
        """Evaluate a small derived formula over other fields' current values, or None
        if any source is missing. Ops: center=(a+b)/2, span=|last-first|, sum, diff."""
        if not formula:
            return None
        try:
            op, args = next(iter(formula.items()))
        except StopIteration:
            return None
        if not isinstance(args, (list, tuple)):
            return None
        vals = [self._source_num(str(a)) for a in args]
        if any(v is None for v in vals):
            return None
        if op == "center":
            return sum(vals) / len(vals)
        if op == "span":
            return abs(vals[-1] - vals[0]) if len(vals) >= 2 else 0.0
        if op == "sum":
            return sum(vals)
        if op == "diff":
            return vals[0] - vals[1] if len(vals) >= 2 else vals[0]
        # Arithmetic-progression helpers (a comb's first/spacing/last ↔ count).
        if op == "count":                      # [a, b, s] terms of a..b step s
            a, b, s = vals[0], vals[1], vals[2]
            if s <= 0 or b < a:
                return None
            return float(math.floor((b - a) / s + 1e-9) + 1)
        if op == "span_to":                    # [a, b, s] extent covered a..b step s
            a, b, s = vals[0], vals[1], vals[2]
            if s <= 0 or b < a:
                return None
            return float(math.floor((b - a) / s + 1e-9) * s)
        if op == "term":                       # [a, n, s] the n-th term: a + (n-1)s
            a, n, s = vals[0], vals[1], vals[2]
            return a + (n - 1) * s
        if op == "extent":                     # [n, s] span of n terms: (n-1)s
            n, s = vals[0], vals[1]
            return (n - 1) * s
        return None

    def _derived_frame(self, spec: dict, top_sep: bool) -> QWidget:
        """A read-only field showing a value computed from other fields — a dashed
        inset readout with a limit chip + warning when it has bounds."""
        dest = spec["dest"]
        frame = QWidget()
        frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 2 if not top_sep else 0, 0, 9)
        v.setSpacing(5)
        if top_sep:
            v.addWidget(self._hairline())
        lrow = QHBoxLayout(); lrow.setContentsMargins(0, 0, 0, 0); lrow.setSpacing(8)
        lrow.addWidget(field_name_label(self._display_name(spec)))
        if spec.get("unit"):
            lrow.addWidget(unit_chip(spec["unit"].replace(" ", " · ")))
        lrow.addStretch(1)
        tag = QLabel("computed")
        tag.setStyleSheet(f"font-size: 9px; letter-spacing: .08em; color: {Palette.TEXT_FAINT};")
        lrow.addWidget(tag)
        v.addLayout(lrow)
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(8)
        val_lbl = QLabel("—")
        val_lbl.setFont(mono_font(13, 600))
        val_lbl.setStyleSheet(
            f"background: {Palette.INSET}; border: 1px dashed {Palette.BORDER}; "
            f"border-radius: 9px; min-height: 34px; padding: 0 11px; color: {Palette.TEXT};")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        crow.addWidget(val_lbl, 1)
        lo, hi = spec.get("min"), spec.get("max")
        chip = None
        if lo is not None and hi is not None:
            chip = LimitChip()
            chip.set_range(self._fmt_bound(lo), self._fmt_bound(hi))
            crow.addWidget(chip)
        v.addLayout(crow)
        warn = self._warn_line()
        v.addWidget(warn)
        if spec.get("help"):
            val_lbl.setToolTip(spec["help"])
        self._derived[dest] = {"spec": spec, "value_lbl": val_lbl, "chip": chip,
                               "warn": warn, "error": None}
        return frame

    def _recompute_derived(self, *_) -> None:
        """Refresh every derived readout from its sources, flagging (and recording) an
        out-of-range value so validate() can block it."""
        for info in self._derived.values():
            spec = info["spec"]
            val = self._eval_formula(spec.get("formula"))
            unit = f" {spec['unit']}" if spec.get("unit") else ""
            lo, hi = spec.get("min"), spec.get("max")
            warn = info["warn"]
            info["error"] = None
            if val is None:
                info["value_lbl"].setText("—")
                if info["chip"] is not None:
                    info["chip"].set_state(over=False, under=False)
                warn.setVisible(False)
                continue
            info["value_lbl"].setText(f"{val:g}{unit}")
            over = hi is not None and val > hi + 1e-9
            under = lo is not None and val < lo - 1e-9
            if info["chip"] is not None:
                info["chip"].set_state(over=over, under=under)
            name = self._display_name(spec).rstrip(" *")
            if over:
                info["error"] = (f"{name} {val:g}{unit} exceeds the maximum "
                                 f"{self._fmt_bound(hi)}{unit}.")
                warn.setText("⚠ " + info["error"]); warn.setVisible(True)
            elif under:
                info["error"] = (f"{name} {val:g}{unit} is below the minimum "
                                 f"{self._fmt_bound(lo)}{unit}.")
                warn.setText("⚠ " + info["error"]); warn.setVisible(True)
            else:
                warn.setVisible(False)

    # ── Values ───────────────────────────────────────────────────────────────

    def set_values(self, args: List[str]) -> List[str]:
        """Prefill widgets from a CLI arg list; return the args not recognised as
        one of this form's parameters (so a caller can surface them separately)."""
        # Setting the frequency widget here would fire its commit signal and re-fold
        # mid-prefill — rebuilding the widgets this loop is still iterating. Suppress the
        # re-fold during the loop; then fold once at the end for the prefilled frequency.
        self._loading = True
        try:
            extra = self._set_values(args)
        finally:
            self._loading = False
        self._maybe_refold_after_load()
        return extra

    def _set_values(self, args: List[str]) -> List[str]:
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
                    # Fixed-choice combo: the stored CLI value is the option token, but
                    # the combo shows labels, so match on itemData first. Selecting a
                    # value not in the list would silently snap to the first choice and
                    # send THAT instead, so add the stored value to preserve it.
                    idx = w.findData(val)
                    if idx < 0:
                        idx = w.findText(val)          # plain choice: text == token
                    if idx < 0:
                        w.addItem(val, val); idx = w.count() - 1
                    w.setCurrentIndex(idx)
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
                    val = choice_token(w)         # the value the script receives
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
                out[dest] = choice_typed_value(spec, choice_token(w))
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
        # A derived readout (e.g. a start/stop-implied sweep width) out of its bounds.
        for info in self._derived.values():
            if info.get("error"):
                return info["error"]
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

    def _widget_for(self, spec: dict) -> QWidget:
        default = spec.get("default")
        numeric = spec.get("type") in ("int", "float")
        if spec.get("is_flag"):
            # On/Off toggle (a QCheckBox subclass, so value handling is unchanged).
            w = ToggleSwitch()
            w.setChecked(bool(default))
            w.stateChanged.connect(self.changed.emit)
        elif spec.get("presets"):
            w = Dropdown(editable=True)          # type a value or pick a preset
            w.setFont(mono_font(13, 500) if numeric else self._sans_input_font())
            for p in spec["presets"]:
                w.addItem(str(p["label"]))
            if default is not None:
                lbl = preset_label_for_value(spec, fmt_value(default))
                w.setCurrentText(lbl if lbl is not None else fmt_value(default))
            else:
                w.setCurrentText("")
            ph = "pick a preset or type a value"
            rng = range_hint(spec)
            if rng:
                ph += f" ({rng})"
            if w.lineEdit() is not None:
                w.lineEdit().setPlaceholderText(ph)
            w.currentTextChanged.connect(self.changed.emit)
            self._guard_scroll(w)
        elif _use_spinbox(spec) and default is not None:
            # A stepper only when there's a sensible default to step from. A numeric field
            # with NO default falls through to an empty text box below, so it reads as
            # "nothing entered" instead of a misleading 0.
            w = _make_spinbox(spec)
            w.setSuffix("")                       # the unit lives in its own chip now
            w.setButtonSymbols(w.ButtonSymbols.NoButtons)
            w.setFont(mono_font(13, 500))
            if isinstance(w, _AchievableSpin):
                sn = self._power_snappers()
                if sn:
                    w.set_snappers(*sn)
                    w.setValue(sn[0](float(w.value())))   # land the default on the grid
            w.valueChanged.connect(self.changed.emit)
            self._guard_scroll(w)
        elif spec.get("choices"):
            w = Dropdown(editable=False)         # pick-only
            w.setFont(self._sans_input_font())
            labels = spec.get("choice_labels") or {}
            for c in spec["choices"]:
                tok = str(c)
                w.addItem(labels.get(tok, tok), tok)   # show label, carry token as data
            idx = choice_default_index(spec, default)
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.currentTextChanged.connect(self.changed.emit)
            self._guard_scroll(w)
        else:
            w = QLineEdit()
            w.setFont(mono_font(13, 500) if numeric else self._sans_input_font())
            if default is not None:
                w.setText(fmt_value(default))
            if numeric:                           # empty = nothing entered; show the range
                rng = range_hint(spec)
                unit = f" {spec['unit']}" if spec.get("unit") else ""
                ph = (f"empty — allowed {rng}{unit}" if rng else "empty — no value")
            else:
                ph = spec.get("type") or "text"
                if spec.get("required"):
                    ph += " (required)"
            w.setPlaceholderText(ph)
            w.textChanged.connect(self.changed.emit)
        if spec.get("help"):
            w.setToolTip(spec["help"])
        return w

    def _guard_scroll(self, w: QWidget) -> None:
        """Make a stepper/dropdown ignore the mouse wheel unless it's focused, and never
        grab focus just by being scrolled over (StrongFocus drops WheelFocus)."""
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.installEventFilter(self._wheel_guard)

    @staticmethod
    def _sans_input_font():
        from PyQt6.QtGui import QFont
        f = QFont("IBM Plex Sans")
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.Medium)
        return f
