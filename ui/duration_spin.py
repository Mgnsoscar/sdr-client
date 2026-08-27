"""
DurationSpinBox — a spinbox whose *value* is a number of seconds but which shows
and accepts human durations like '1m 30s', '2 min', '1h 5m', or plain '90'.

It subclasses QDoubleSpinBox and keeps value()/setValue()/valueChanged in seconds,
so it's a drop-in replacement for the raw '… s' offset spinboxes: every caller that
reads .value() as seconds keeps working unchanged. Only the on-screen text and the
text a user can type change.

Accepted input (case-insensitive, optional leading +/-):
    90              → 90 s        (a bare number is seconds)
    90s / 90 sec    → 90 s
    1m30s / 1m 30s  → 90 s
    2 min           → 120 s
    1h5m            → 3900 s
    1:30            → 90 s        (mm:ss)
    1:02:03         → 3723 s      (hh:mm:ss)
"""
from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QValidator
from PyQt6.QtWidgets import QDoubleSpinBox

from .param_form import fmt_duration
from .theme import Palette

# One number followed by an h/min/s unit word. Repeated across a string. Unit
# alternatives are longest-first (so 'min' wins over 'm') and a trailing (?![a-z])
# stops a short unit from matching a prefix of a longer word ('m' inside 'min').
_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours|hour|hrs|hr|h|minutes|minute|mins|min|m|seconds|second|secs|sec|s)"
    r"(?![a-z])",
    re.IGNORECASE)


def parse_duration(text: str, bare_unit: str = "s") -> Optional[float]:
    """Parse a human duration string into seconds, or None if it can't be read.

    A number with an explicit unit ('5m', '30s', '1:30') is always taken at face
    value. A bare number ('5') is interpreted in `bare_unit` — 's' (default) reads
    it as seconds; 'm' as minutes; 'h' as hours — so a field can default to a unit
    that suits it (e.g. a run duration defaulting to minutes)."""
    if text is None:
        return None
    t = text.strip().lower()
    if not t:
        return None
    neg = False
    if t[0] in "+-":
        neg = t[0] == "-"
        t = t[1:].strip()
    if not t:
        return None

    # Colon form: mm:ss or hh:mm:ss.
    if ":" in t:
        parts = t.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            secs = nums[0] * 60 + nums[1]
        elif len(nums) == 3:
            secs = nums[0] * 3600 + nums[1] * 60 + nums[2]
        else:
            return None
        return -secs if neg else secs

    # Unit-token form: sum every "<n><unit>" and reject any leftover junk.
    matches = _TOKEN_RE.findall(t)
    if matches:
        if _TOKEN_RE.sub("", t).strip():
            return None
        total = 0.0
        for val, unit in matches:
            u = unit[0]
            total += float(val) * (3600 if u == "h" else 60 if u == "m" else 1)
        return -total if neg else total

    # Bare number → the field's default unit (seconds unless told otherwise).
    try:
        n = float(t)
    except ValueError:
        return None
    scale = 3600 if bare_unit == "h" else 60 if bare_unit == "m" else 1
    secs = n * scale
    return -secs if neg else secs


class DurationSpinBox(QDoubleSpinBox):
    """A QDoubleSpinBox valued in seconds, displayed/entered as h/min/s.

    Instead of Qt's native (and, under the editor stylesheet, hidden) up/down
    buttons, it paints its own stacked up/down chevrons in a tinted chip on the
    right — matching the ``Dropdown`` widget's chevron so the two read as the
    same design language — and steps by ``singleStep`` seconds when clicked."""

    _ARROW_W = 26.0                          # chip width; matches Dropdown._DROP_W

    def __init__(self, parent=None, bare_unit: str = "s"):
        super().__init__(parent)
        # How a unitless entry is read: "s" seconds, "m" minutes, "h" hours. An
        # entry with an explicit unit ('30s', '2m') always overrides this.
        self._bare_unit = bare_unit
        self.setDecimals(1)
        self.setSingleStep(5.0)             # arrow / wheel step = 5 seconds
        self.setRange(-100000.0, 100000.0)
        self.setKeyboardTracking(False)     # commit text only on Enter / focus-out
        self.setMinimumWidth(96)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    def _arrow_rect(self) -> QRectF:
        """The clickable chip on the right holding both chevrons."""
        return QRectF(self.width() - self._ARROW_W, 0.0, self._ARROW_W, float(self.height()))

    def mousePressEvent(self, ev) -> None:
        # A click in the chevron chip steps the value; the top half increments,
        # the bottom half decrements. Clicks elsewhere fall through to the editor.
        if self.isEnabled() and self._arrow_rect().contains(float(ev.position().x()), float(ev.position().y())):
            if ev.position().y() < self.height() / 2.0:
                self.stepUp()
            else:
                self.stepDown()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)               # frame + text
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        h = float(self.height())
        cx = w - self._ARROW_W / 2.0
        chip = QRectF(w - self._ARROW_W + 3, (h - 24) / 2, self._ARROW_W - 9, 24)
        chip_path = QPainterPath()
        chip_path.addRoundedRect(chip, 7, 7)
        p.fillPath(chip_path, QColor(Palette.ACCENT_SOFT))
        pen = QPen(QColor(Palette.ACCENT_INK), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        uy = h / 2.0 - 4.5                    # up chevron, top half
        up = QPainterPath()
        up.moveTo(cx - 4, uy + 2)
        up.lineTo(cx, uy - 2.5)
        up.lineTo(cx + 4, uy + 2)
        p.drawPath(up)
        dy = h / 2.0 + 4.5                    # down chevron, bottom half
        down = QPainterPath()
        down.moveTo(cx - 4, dy - 2)
        down.lineTo(cx, dy + 2.5)
        down.lineTo(cx + 4, dy - 2)
        p.drawPath(down)
        p.end()

    # value() stays in seconds; only the text representation is humanized.
    def textFromValue(self, v: float) -> str:
        return fmt_duration(v, compact=True)

    def valueFromText(self, text: str) -> float:
        v = parse_duration(text, self._bare_unit)
        return v if v is not None else self.value()

    def validate(self, text: str, pos: int):
        # Let a partial entry stand while typing; only commit-time parsing is strict.
        if text.strip() in ("", "+", "-") or parse_duration(text, self._bare_unit) is not None:
            return (QValidator.State.Acceptable, text, pos)
        return (QValidator.State.Intermediate, text, pos)
