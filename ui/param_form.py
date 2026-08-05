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
from typing import Dict, List, Optional

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


def num_or_none(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


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
        self._form = QFormLayout(self)
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setSpacing(6)

    # ── Build ────────────────────────────────────────────────────────────────

    def set_params(self, specs: List[dict]) -> None:
        """Rebuild the form for a parameter schema (clears existing widgets)."""
        while self._form.count():
            item = self._form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._widgets.clear()

        if not specs:
            note = QLabel("This script declares no parameters.")
            note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            self._form.addRow(note)
        for spec in specs:
            widget = self._widget_for(spec)
            self._widgets[spec["dest"]] = (widget, spec)
            self._form.addRow(self._label_for(spec), widget)
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
                    w.setCurrentText(val)
                elif isinstance(w, QLineEdit):
                    w.setText(val)
                i += 2
            else:
                i += 1
        return extra

    def build_args(self) -> List[str]:
        """The CLI args produced by the current widget values (params only)."""
        out: List[str] = []
        for dest, (w, spec) in self._widgets.items():
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

    def validate(self) -> Optional[str]:
        """Return an error message if any value is missing/bad/out-of-range."""
        missing, bad_type, bad_range = [], [], []
        for dest, (w, spec) in self._widgets.items():
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
                    num = int(val, 0) if spec["type"] == "int" else float(val)
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
