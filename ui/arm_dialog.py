"""
ArmDialog — pick a shared on-air time (and optional stop) before arming.

Shared by the Plans tab (arm a whole plan across units) and a unit's Sequences
tab (arm one sequence). Built for fast-paced testing: quick-select the next whole
or half minute, nudge ±30 s / ±1 min, or tick "as soon as possible" to arm at the
earliest valid instant. The selection auto-advances so it can never expire while
the operator adjusts it, and the stop duration clamps to a derivable minimum.

The dialog is clock-agnostic: it returns an absolute laptop-UTC on-air time via
on_air_at() and an optional run duration via stop_duration_s(). The caller decides
how to translate that to each unit's clock when arming.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from .duration_spin import DurationSpinBox
from .param_form import fmt_duration
from .theme import Palette


def _ceil_to(dt: datetime, step_s: int) -> datetime:
    """Smallest wall-clock instant ≥ dt that lands on a whole `step_s`-second grid
    (step 60 → next whole minute, step 30 → next :00/:30), microseconds dropped."""
    base = dt.replace(microsecond=0)
    if base < dt:
        base += timedelta(seconds=1)
    secs = base.hour * 3600 + base.minute * 60 + base.second
    rem = secs % step_s
    if rem:
        base += timedelta(seconds=step_s - rem)
    return base


class ArmDialog(QDialog):
    """Choose an on-air time and optional stop for an arm. See module docstring."""

    GRID_S = 30   # every selectable on-air time sits on a 30-second grid

    def __init__(self, heading: str, safety_lead_s: float, default_duration_s: float,
                 min_floor_s: float, skew_note: str = "", parent=None):
        super().__init__(parent)
        self._safety = max(0.0, safety_lead_s)      # now + this = earliest valid T0
        self._min_floor = max(0.0, min_floor_s)     # hard minimum (0 = none derivable)
        self._default_dur = max(self._min_floor, default_duration_s, 1.0)
        self.setWindowTitle("Arm")
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        head = QLabel(heading)
        head.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        head.setWordWrap(True)
        outer.addWidget(head)

        # ── On-air time ─────────────────────────────────────────────────────
        self._on_air = QLabel()
        self._on_air.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {Palette.TEXT};")
        outer.addWidget(self._on_air)
        self._countdown = QLabel()
        self._countdown.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._countdown)

        # ASAP: go on air at the earliest valid instant (computed when you press Arm),
        # instead of a chosen grid time. Disables the picker below while ticked.
        self._asap = QCheckBox("As soon as possible (earliest valid time)")
        self._asap.toggled.connect(self._sync_asap)
        outer.addWidget(self._asap)

        self._time_ctrls: List[QWidget] = []   # picker widgets greyed out in ASAP mode
        quick = QHBoxLayout(); quick.setSpacing(6)
        b_min = QPushButton("Next minute  :00")
        b_half = QPushButton("Next ½ min  :30")
        b_min.clicked.connect(lambda: self._quick(60))
        b_half.clicked.connect(lambda: self._quick(30))
        for b in (b_min, b_half):
            quick.addWidget(b); self._time_ctrls.append(b)
        outer.addLayout(quick)

        nudge = QHBoxLayout(); nudge.setSpacing(6)
        for label, delta in (("− 1 min", -60), ("− 30 s", -30), ("+ 30 s", 30), ("+ 1 min", 60)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            nudge.addWidget(b); self._time_ctrls.append(b)
        outer.addLayout(nudge)

        self._floor_note = QLabel()
        self._floor_note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        self._floor_note.setWordWrap(True)
        outer.addWidget(self._floor_note)

        # ── Stop time ───────────────────────────────────────────────────────
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {Palette.BORDER};")
        outer.addWidget(line)

        self._stop_on = QCheckBox("Set a stop time (otherwise runs until stopped)")
        self._stop_on.toggled.connect(self._sync_stop)
        outer.addWidget(self._stop_on)

        stop_row = QGridLayout(); stop_row.setHorizontalSpacing(8); stop_row.setVerticalSpacing(4)
        self._dur_lbl = QLabel("Run for")
        self._dur = DurationSpinBox()
        # A derivable minimum is a hard floor: the spinbox clamps to it, so a too-short
        # duration can't be entered (no round-trip through an arm-time error).
        self._dur.setRange(max(1.0, self._min_floor), 100000.0)
        self._dur.setValue(round(self._default_dur))
        self._dur.valueChanged.connect(lambda _=0: self._render())
        stop_row.addWidget(self._dur_lbl, 0, 0)
        stop_row.addWidget(self._dur, 0, 1)
        self._min_hint = QLabel()
        self._min_hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        if self._min_floor > 0:
            self._min_hint.setText(
                f"minimum {fmt_duration(round(self._min_floor))} for all steps to fit")
        stop_row.addWidget(self._min_hint, 1, 0, 1, 2)
        self._stop_at = QLabel()
        self._stop_at.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        stop_row.addWidget(self._stop_at, 2, 0, 1, 2)
        outer.addLayout(stop_row)

        if skew_note:
            warn = QLabel(skew_note.strip())
            warn.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
            warn.setWordWrap(True)
            outer.addWidget(warn)

        buttons = QDialogButtonBox()
        arm = buttons.addButton("Arm", QDialogButtonBox.ButtonRole.AcceptRole)
        arm.setObjectName("primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Start at the next whole minute at or after the floor.
        self._t0 = _ceil_to(self._floor(), 60)
        self._auto_note = False
        self._sync_stop()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)
        self._render()

    # ── Selection helpers ────────────────────────────────────────────────────

    def _floor(self) -> datetime:
        """Earliest instant an on-air time is still valid (warm-up + margin ahead)."""
        return datetime.now(timezone.utc) + timedelta(seconds=self._safety)

    def _min_slot(self) -> datetime:
        """The next grid slot at or after the floor — the earliest selectable T0."""
        return _ceil_to(self._floor(), self.GRID_S)

    def _quick(self, step_s: int) -> None:
        self._t0 = _ceil_to(self._floor(), step_s)
        self._auto_note = False
        self._render()

    def _nudge(self, delta_s: int) -> None:
        self._t0 = max(self._t0 + timedelta(seconds=delta_s), self._min_slot())
        self._auto_note = False
        self._render()

    def _effective_t0(self) -> datetime:
        """The on-air instant that will actually be used: the earliest valid time in
        ASAP mode (computed fresh), otherwise the operator's chosen grid time."""
        return self._floor() if self._asap.isChecked() else self._t0

    def _tick(self) -> None:
        # Never let a chosen time expire: if real time has caught up, hop to the next
        # slot. (ASAP has no fixed choice — it always tracks the live floor.)
        if not self._asap.isChecked():
            floor_slot = self._min_slot()
            if self._t0 < floor_slot:
                self._t0 = floor_slot
                self._auto_note = True
        self._render()

    def _render(self) -> None:
        eff = self._effective_t0()
        if self._asap.isChecked():
            self._on_air.setText("on air as soon as possible")
        else:
            self._on_air.setText(eff.astimezone().strftime("on air at  %H:%M:%S"))
        secs = max(0.0, (eff - datetime.now(timezone.utc)).total_seconds())
        note = "  ·  slot advanced (previous time passed)" if self._auto_note else ""
        self._countdown.setText(
            f"in {fmt_duration(round(secs))} (~{eff.astimezone().strftime('%H:%M:%S')} local){note}"
            if self._asap.isChecked()
            else f"in {fmt_duration(round(secs))}{note}")
        self._floor_note.setText(
            f"Earliest valid on-air is ~{fmt_duration(round(self._safety))} from now "
            f"(warm-up + margin). Times snap to the {self.GRID_S}s grid.")
        if self._stop_on.isChecked():
            stop_local = (eff + timedelta(seconds=self._dur.value())).astimezone()
            self._stop_at.setText(f"→ stops at {stop_local.strftime('%H:%M:%S')} local "
                                  f"({fmt_duration(round(self._dur.value()))} on air)")
        else:
            self._stop_at.setText("Runs open-ended until manually stopped.")

    def _sync_asap(self) -> None:
        asap = self._asap.isChecked()
        for w in self._time_ctrls:
            w.setEnabled(not asap)
        self._floor_note.setVisible(not asap)
        if not asap:   # returning to manual: make sure the choice is still valid
            self._t0 = max(self._t0, self._min_slot())
            self._auto_note = False
        self._render()

    def _sync_stop(self) -> None:
        on = self._stop_on.isChecked()
        for w in (self._dur_lbl, self._dur, self._min_hint):
            w.setEnabled(on)
        self._render()

    # ── Results ──────────────────────────────────────────────────────────────

    def on_air_at(self) -> datetime:
        return self._effective_t0()

    def stop_duration_s(self) -> Optional[float]:
        return round(self._dur.value(), 1) if self._stop_on.isChecked() else None
