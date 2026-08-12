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

from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import QDoubleSpinBox

from .param_form import fmt_duration

# One number followed by an h/min/s unit word. Repeated across a string. Unit
# alternatives are longest-first (so 'min' wins over 'm') and a trailing (?![a-z])
# stops a short unit from matching a prefix of a longer word ('m' inside 'min').
_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(hours|hour|hrs|hr|h|minutes|minute|mins|min|m|seconds|second|secs|sec|s)"
    r"(?![a-z])",
    re.IGNORECASE)


def parse_duration(text: str) -> Optional[float]:
    """Parse a human duration string into seconds, or None if it can't be read."""
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

    # Bare number → seconds.
    try:
        secs = float(t)
    except ValueError:
        return None
    return -secs if neg else secs


class DurationSpinBox(QDoubleSpinBox):
    """A QDoubleSpinBox valued in seconds, displayed/entered as h/min/s."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDecimals(1)
        self.setSingleStep(1.0)
        self.setRange(-100000.0, 100000.0)
        self.setKeyboardTracking(False)     # commit text only on Enter / focus-out
        self.setMinimumWidth(96)

    # value() stays in seconds; only the text representation is humanized.
    def textFromValue(self, v: float) -> str:
        return fmt_duration(v, compact=True)

    def valueFromText(self, text: str) -> float:
        v = parse_duration(text)
        return v if v is not None else self.value()

    def validate(self, text: str, pos: int):
        # Let a partial entry stand while typing; only commit-time parsing is strict.
        if text.strip() in ("", "+", "-") or parse_duration(text) is not None:
            return (QValidator.State.Acceptable, text, pos)
        return (QValidator.State.Intermediate, text, pos)
