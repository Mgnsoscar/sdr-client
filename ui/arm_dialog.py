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
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .duration_spin import DurationSpinBox
from .param_form import fmt_duration
from .theme import Palette
from .widgets import fit_dialog_to_screen


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
                 min_floor_s: float, skew_note: str = "", parent=None, *,
                 accept_label: str = "Arm", title: str = "Arm", body_note: str = "",
                 show_stop: bool = True, max_hold_default_s: Optional[float] = None,
                 status_provider: Optional[Callable[[], str]] = None):
        """The same timing picker serves arming a run and Proceeding a HOLDING one
        (docs/sequence-hold-step.md §6.3). Extra knobs, all keyword-only and defaulted so
        existing callers are unchanged:
          accept_label / title  — button + window text ("Proceed" while holding).
          body_note             — a muted paragraph under the heading (arm messaging).
          show_stop             — hide the stop-time section (a hold-aware arm is
                                  open-ended; a proceed's off-air is derived on the agent).
          max_hold_default_s    — show the max-hold deadman field (arm a hold-aware run);
                                  read back via max_hold_s() (0 = unlimited).
          status_provider       — a callable rendered live each tick (elapsed / held /
                                  remaining, for the Proceed dialog)."""
        super().__init__(parent)
        self._safety = max(0.0, safety_lead_s)      # now + this = earliest valid T0
        self._min_floor = max(0.0, min_floor_s)     # hard minimum (0 = none derivable)
        self._default_dur = max(self._min_floor, default_duration_s, 1.0)
        self._status_provider = status_provider
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        # Accept focus on a background click, so clicking anywhere outside the
        # duration field pulls focus off it and commits what was typed (the spinbox
        # only commits on focus-out / Enter). Without this, clicking empty space
        # leaves focus in the field and the entry stays uncommitted.
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # The stacked sections live in a scroll area so that on a short / DPI-scaled viewport the
        # Arm/Cancel footer (pinned to `root` below, OUTSIDE the scroll) stays reachable — the body
        # scrolls instead of pushing the buttons off-screen.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.Shape.NoFrame)
        _scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _body = QWidget()
        outer = QVBoxLayout(_body)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        _scroll.setWidget(_body)
        root.addWidget(_scroll, 1)

        head = QLabel(heading)
        head.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        head.setWordWrap(True)
        outer.addWidget(head)

        if body_note:
            note = QLabel(body_note)
            note.setWordWrap(True)
            note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
            outer.addWidget(note)

        # Live status line (Proceed: elapsed run time / time held / remaining allowance).
        self._status_line = QLabel("")
        self._status_line.setWordWrap(True)
        self._status_line.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {Palette.ARMED};")
        self._status_line.setVisible(status_provider is not None)
        outer.addWidget(self._status_line)

        # Live wall clock, so the operator can compare "now" to the on-air time they
        # set without glancing away. Kept visually quiet (small, muted, a ⏱ marker)
        # so it can't be mistaken for the on-air time below.
        self._now = QLabel()
        self._now.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._now)

        # ── On-air time ─────────────────────────────────────────────────────
        self._on_air = QLabel()
        self._on_air.setStyleSheet(
            f"font-size: 24px; font-weight: 700; color: {Palette.ACCENT};")
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

        # ── Max-hold deadman (arm a hold-aware run) ─────────────────────────
        # A held run is on-air indefinitely; a deadman auto-aborts if it stays HOLDING
        # past this. Unchecked = unlimited (0). See docs/sequence-hold-step.md §5.6.
        self._maxhold_section = QWidget()
        if max_hold_default_s is not None:
            mh = QVBoxLayout(self._maxhold_section)
            mh.setContentsMargins(0, 0, 0, 0)
            mh_line = QFrame(); mh_line.setFrameShape(QFrame.Shape.HLine)
            mh_line.setStyleSheet(f"color: {Palette.BORDER};")
            mh.addWidget(mh_line)
            self._maxhold_on = QCheckBox("Auto-stop if held longer than")
            self._maxhold_on.setChecked(float(max_hold_default_s) > 0)
            self._maxhold_on.toggled.connect(self._sync_maxhold)
            self._maxhold = DurationSpinBox(bare_unit="m")
            self._maxhold.setRange(1.0, 100000.0)
            self._maxhold.setValue(round(max(60.0, float(max_hold_default_s) or 1800.0)))
            mh_row = QHBoxLayout(); mh_row.setSpacing(8)
            mh_row.addWidget(self._maxhold_on)
            mh_row.addWidget(self._maxhold)
            mh_row.addStretch(1)
            mh.addLayout(mh_row)
            self._maxhold_hint = QLabel("Unchecked = no limit (held until you Proceed or Stop).")
            self._maxhold_hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            mh.addWidget(self._maxhold_hint)
        else:
            self._maxhold_on = None
            self._maxhold = None
        self._maxhold_section.setVisible(max_hold_default_s is not None)
        outer.addWidget(self._maxhold_section)

        # ── Stop time ───────────────────────────────────────────────────────
        self._stop_section = QWidget()
        stop_outer = QVBoxLayout(self._stop_section)
        stop_outer.setContentsMargins(0, 0, 0, 0)
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {Palette.BORDER};")
        stop_outer.addWidget(line)

        self._stop_on = QCheckBox("Set a stop time (otherwise runs until stopped)")
        self._stop_on.toggled.connect(self._sync_stop)
        stop_outer.addWidget(self._stop_on)

        stop_row = QGridLayout(); stop_row.setHorizontalSpacing(8); stop_row.setVerticalSpacing(4)
        self._dur_lbl = QLabel("Run for")
        # A run duration reads a bare number as minutes (5 → 5 min); add s/m/h to be
        # explicit (30s, 2m, 1h).
        self._dur = DurationSpinBox(bare_unit="m")
        self._dur.setToolTip("A plain number is minutes (5 → 5 min). "
                             "Add a unit to be explicit: 30s, 2m, 1h.")
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
        stop_outer.addLayout(stop_row)
        self._stop_section.setVisible(show_stop)
        outer.addWidget(self._stop_section)

        if skew_note:
            warn = QLabel(skew_note.strip())
            warn.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
            warn.setWordWrap(True)
            outer.addWidget(warn)

        buttons = QDialogButtonBox()
        arm = buttons.addButton(accept_label, QDialogButtonBox.ButtonRole.AcceptRole)
        arm.setObjectName("primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        # Footer pinned OUTSIDE the scroll area so Arm/Cancel are always on-screen.
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 8, 18, 12)
        footer.addWidget(buttons)
        root.addLayout(footer)

        # Start at the next whole minute at or after the floor.
        self._t0 = _ceil_to(self._floor(), 60)
        self._auto_note = False
        self._sync_stop()
        self._sync_maxhold()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)
        self._render()

        # Open at the content's natural height (so a short arm has no empty scroll space), but
        # never taller than the screen — the body then scrolls and the pinned footer stays on
        # a short / DPI-scaled viewport.
        want_h = max(420, _body.sizeHint().height() + 64)   # + footer & margins
        fit_dialog_to_screen(self, 480, want_h)

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
        if self._status_provider is not None:
            try:
                self._status_line.setText(self._status_provider() or "")
            except Exception:                            # noqa: BLE001 — status is best-effort
                self._status_line.setText("")
        self._now.setText(f"⏱ now  {datetime.now().astimezone().strftime('%H:%M:%S')}")
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

    def _sync_maxhold(self) -> None:
        if self._maxhold is None:
            return
        self._maxhold.setEnabled(self._maxhold_on.isChecked())

    # ── Results ──────────────────────────────────────────────────────────────

    def on_air_at(self) -> datetime:
        return self._effective_t0()

    def stop_duration_s(self) -> Optional[float]:
        return round(self._dur.value(), 1) if self._stop_on.isChecked() else None

    def max_hold_s(self) -> float:
        """The operator-set max-hold deadman (seconds); 0 = unlimited. 0 when no
        max-hold field was shown (a non-hold arm)."""
        if self._maxhold is None:
            return 0.0
        return round(self._maxhold.value(), 1) if self._maxhold_on.isChecked() else 0.0
