"""
TimelineEditor — a visual, drag-and-drop editor for a sequence's steps.

A sequence choreographs tasks around ONE on-air window. It stores every step
relative to one of two anchors (see agent/sequence_runner.py):

    anchor="start"  →  ON-AIR (T0).   offset ≤ 0 = warm-up (before RF), 0 = on-air.
    anchor="stop"   →  OFF-AIR.        offset ≥ 0 = cool-down (after RF), 0 = off-air.

Each step also carries an action — ▶ start (launch a task) or ⏹ stop (kill it).

Because the length of the on-air window is NOT part of the sequence (it's chosen
later, at arm time), the gap between the ON-AIR and OFF-AIR anchors is drawn as a
fixed, not-to-scale band. Each side (warm-up / cool-down) IS to scale, at a fixed
pixels-per-second, so dragging a pill left/right maps linearly to its offset.

Interaction:
  - Drag a pill horizontally to change its offset (snapped to whole seconds).
  - Drag it across the midpoint between the anchors to re-anchor it (ON-AIR ↔
    OFF-AIR); the offset is then measured from the anchor it lands nearest.
  - Click a pill (without dragging) to edit task / action / exact offset, or remove
    it, in a small popup.
  - "+ On-air task" / "+ Off-air task" add a new pill on that anchor at offset 0.

The widget is pure UI over an internal list of `_TLStep`; `steps()` converts them
to `api.models.SequenceStep` for saving. No network I/O happens here.
"""
from __future__ import annotations

import itertools
import shlex
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from .theme import Palette


# ── Geometry constants ──────────────────────────────────────────────────────
SCALE = 3.0            # pixels per second in the warm-up / cool-down zones
MIDDLE_GAP = 120       # px between the ON-AIR and OFF-AIR anchors (NOT to scale)
EDGE_PAD = 70          # px of empty space beyond the furthest pill on each side
HEADROOM_S = 30.0      # seconds of extra drag room kept beyond the furthest pill
MIN_SIDE_S = 60.0      # each side is at least this many seconds wide
SNAP_S = 1.0           # drag snaps offsets to this granularity (seconds)

PILL_W = 190           # uniform pill width (keeps lane math deterministic)
PILL_H = 32
LANE_VGAP = 8          # vertical gap between stacked lanes
PILLS_TOP = 26         # y of the first lane
BASELINE_FROM_BOTTOM = 46   # baseline sits this far above the canvas bottom
TICK_S = 30            # draw a faint tick + label every this many seconds
DRAG_THRESHOLD = 4     # px of movement before a press counts as a drag


# ── Pure offset ⇄ pixel mapping (unit-testable, no Qt state) ─────────────────

def offset_to_center_x(anchor: str, offset_s: float,
                       on_air_x: float, off_air_x: float) -> float:
    """Pixel x of a pill's centre for a given anchor + signed offset."""
    base = on_air_x if anchor == "start" else off_air_x
    return base + offset_s * SCALE


def center_x_to_anchor_offset(center_x: float, on_air_x: float, off_air_x: float,
                              snap: bool = True) -> Tuple[str, float]:
    """
    Inverse map: which anchor a pill at center_x belongs to, and its offset from
    that anchor. The split point is the midpoint between the two anchors — left of
    it is ON-AIR (start), right is OFF-AIR (stop).
    """
    midpoint = (on_air_x + off_air_x) / 2.0
    if center_x < midpoint:
        anchor, base = "start", on_air_x
    else:
        anchor, base = "stop", off_air_x
    offset = (center_x - base) / SCALE
    if snap:
        offset = round(offset / SNAP_S) * SNAP_S
    # Normalise -0.0 → 0.0 so labels never read "-0s".
    if offset == 0:
        offset = 0.0
    return anchor, offset


def _fmt_offset(offset_s: float) -> str:
    """'-120s', '+5s', '0s' — an integer if it's whole, else one decimal."""
    if offset_s == int(offset_s):
        n = int(offset_s)
    else:
        n = round(offset_s, 1)
    if n > 0:
        return f"+{n}s"
    if n < 0:
        return f"{n}s"
    return "0s"


# ── Internal step model ──────────────────────────────────────────────────────

_ids = itertools.count(1)


@dataclass
class _TLStep:
    anchor: str          # "start" (on-air) | "stop" (off-air)
    offset_s: float
    action: str          # "start" | "stop"
    task_name: str
    args: List[str] = field(default_factory=list)   # extra CLI args on start
    uid: int = 0

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_ids)


# ── One draggable pill ───────────────────────────────────────────────────────

class _TaskPill(QWidget):
    """A draggable chip representing one step on the timeline."""

    def __init__(self, step: _TLStep, canvas: "_TimelineCanvas"):
        super().__init__(canvas)
        self.step = step
        self._canvas = canvas
        self.setFixedSize(PILL_W, PILL_H)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setObjectName("pill")
        # A plain QWidget won't paint a stylesheet background (rounded chip) unless
        # it's flagged as styled — QFrame would, but we want the id selector here.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._drag_origin_x = 0
        self._start_geom_x = 0
        self._dragging = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 0, 6, 0)
        lay.setSpacing(6)
        self._glyph = QLabel()
        self._glyph.setFixedWidth(12)
        lay.addWidget(self._glyph)
        self._name = QLabel()
        self._name.setStyleSheet("background: transparent;")
        lay.addWidget(self._name, stretch=1)
        self._off = QLabel()
        self._off.setStyleSheet(
            f"background: transparent; color: {Palette.TEXT_FAINT}; font-size: 10px;")
        lay.addWidget(self._off)
        self.refresh()

    # ── Appearance ───────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Re-render glyph, name, offset and colour from the current step."""
        start = self.step.action == "start"
        fg, bg = (Palette.ONLINE, Palette.ONLINE_SOFT) if start else (Palette.IDLE, Palette.IDLE_SOFT)
        known = self._canvas.task_known(self.step.task_name)
        border = fg if known else Palette.CRASH
        self.setStyleSheet(
            f"#pill {{ background: {bg}; border: 1px solid {border}; border-radius: 16px; }}"
        )
        self._glyph.setText("▶" if start else "⏹")
        self._glyph.setStyleSheet(f"background: transparent; color: {fg}; font-size: 12px;")
        name = self.step.task_name or "(no task)"
        # Args only apply to a start; show them inline so per-value steps read apart.
        argstr = " ".join(self.step.args) if (start and self.step.args) else ""
        label = f"{name} {argstr}".strip()
        self._name.setStyleSheet(
            f"background: transparent; color: {Palette.TEXT if known else Palette.CRASH}; "
            f"font-size: 12px; font-weight: 600;")
        fm = self._name.fontMetrics()
        self._name.setText(fm.elidedText(label, Qt.TextElideMode.ElideRight, 128))
        self.set_offset_display(self.step.offset_s)
        tip = f"{'start' if start else 'stop'} {label} · {_fmt_offset(self.step.offset_s)} " \
              f"from {'on-air' if self.step.anchor == 'start' else 'off-air'}"
        if not known:
            tip += "  ⚠ task not found on this unit"
        self.setToolTip(tip)

    def set_offset_display(self, offset_s: float) -> None:
        """Update just the offset caption (used live during a drag)."""
        self._off.setText(_fmt_offset(offset_s))

    # ── Mouse: click to edit, drag to move ───────────────────────────────────

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_origin_x = e.globalPosition().toPoint().x()
            self._start_geom_x = self.x()
            self._dragging = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.raise_()

    def mouseMoveEvent(self, e):  # noqa: N802
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        dx = e.globalPosition().toPoint().x() - self._drag_origin_x
        if not self._dragging and abs(dx) < DRAG_THRESHOLD:
            return
        self._dragging = True
        new_x = self._canvas.clamp_pill_x(self._start_geom_x + dx)
        self.move(new_x, self.y())
        self._canvas.pill_dragging(self)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if self._dragging:
            self._dragging = False
            self._canvas.pill_drag_committed(self)
        else:
            self._canvas.edit_pill(self)


# ── The scrollable canvas that lays out and paints the timeline ──────────────

class _TimelineCanvas(QWidget):
    """Owns the pills, paints the axis/anchors, and does all layout math."""

    changed = pyqtSignal()   # emitted whenever the step set changes

    def __init__(self, editor: "TimelineEditor"):
        super().__init__()
        self._editor = editor
        self._pills: List[_TaskPill] = []
        self._on_air_x = float(EDGE_PAD)
        self._off_air_x = float(EDGE_PAD + MIDDLE_GAP)
        self.setMouseTracking(False)
        self.relayout()

    # ── Task-name knowledge (for validity styling / dropdowns) ───────────────

    def task_known(self, name: str) -> bool:
        tasks = self._editor.available_tasks()
        # If we don't yet know the unit's tasks, don't flag anything as unknown.
        return (not tasks) or (name in tasks)

    # ── Step management ──────────────────────────────────────────────────────

    def add_step(self, step: _TLStep, edit: bool = False) -> None:
        pill = _TaskPill(step, self)
        self._pills.append(pill)
        pill.show()
        self.relayout()
        self.changed.emit()
        if edit:
            self.edit_pill(pill)

    def remove_pill(self, pill: _TaskPill) -> None:
        if pill in self._pills:
            self._pills.remove(pill)
            pill.setParent(None)
            pill.deleteLater()
            self.relayout()
            self.changed.emit()

    def steps(self) -> List[_TLStep]:
        return [p.step for p in self._pills]

    def clear(self) -> None:
        for p in self._pills:
            p.setParent(None)
            p.deleteLater()
        self._pills = []
        self.relayout()
        self.changed.emit()

    def refresh_pills(self) -> None:
        for p in self._pills:
            p.refresh()

    # ── Layout ───────────────────────────────────────────────────────────────

    def relayout(self) -> None:
        """Recompute anchor positions, canvas size, and every pill's geometry."""
        steps = [p.step for p in self._pills]
        left_s = MIN_SIDE_S
        right_s = MIN_SIDE_S
        for s in steps:
            if s.anchor == "start" and s.offset_s < 0:
                left_s = max(left_s, -s.offset_s)
            if s.anchor == "stop" and s.offset_s > 0:
                right_s = max(right_s, s.offset_s)
        left_s += HEADROOM_S
        right_s += HEADROOM_S

        self._on_air_x = EDGE_PAD + left_s * SCALE
        self._off_air_x = self._on_air_x + MIDDLE_GAP
        width = int(self._off_air_x + right_s * SCALE + EDGE_PAD)

        lanes = self._assign_lanes(self._pills)
        n_lanes = (max(lanes.values()) + 1) if lanes else 1
        height = PILLS_TOP + n_lanes * (PILL_H + LANE_VGAP) + BASELINE_FROM_BOTTOM
        height = max(height, 200)

        self.setFixedSize(width, height)
        for pill in self._pills:
            cx = offset_to_center_x(pill.step.anchor, pill.step.offset_s,
                                    self._on_air_x, self._off_air_x)
            lane = lanes.get(pill.step.uid, 0)
            y = PILLS_TOP + lane * (PILL_H + LANE_VGAP)
            pill.move(int(cx - PILL_W / 2), y)
            pill.set_offset_display(pill.step.offset_s)
        self.update()

    def _assign_lanes(self, pills: List[_TaskPill]) -> dict:
        """Greedy lane packing so pills at similar x don't overlap. Keyed by uid."""
        placed: List[List[Tuple[float, float]]] = []   # per lane: list of (left, right)
        lane_of: dict = {}
        ordered = sorted(
            pills,
            key=lambda p: offset_to_center_x(p.step.anchor, p.step.offset_s,
                                             self._on_air_x, self._off_air_x),
        )
        for p in ordered:
            cx = offset_to_center_x(p.step.anchor, p.step.offset_s,
                                    self._on_air_x, self._off_air_x)
            left, right = cx - PILL_W / 2, cx + PILL_W / 2
            for lane_idx, spans in enumerate(placed):
                if all(right + LANE_VGAP <= l or left >= r + LANE_VGAP for (l, r) in spans):
                    spans.append((left, right))
                    lane_of[p.step.uid] = lane_idx
                    break
            else:
                placed.append([(left, right)])
                lane_of[p.step.uid] = len(placed) - 1
        return lane_of

    # ── Drag helpers (called by pills) ───────────────────────────────────────

    def clamp_pill_x(self, x: float) -> int:
        """Keep a pill's top-left x within the canvas."""
        return int(max(0, min(x, self.width() - PILL_W)))

    def pill_dragging(self, pill: _TaskPill) -> None:
        """Live feedback while dragging: show the provisional offset."""
        cx = pill.x() + PILL_W / 2
        _anchor, offset = center_x_to_anchor_offset(cx, self._on_air_x, self._off_air_x)
        pill.set_offset_display(offset)

    def pill_drag_committed(self, pill: _TaskPill) -> None:
        """On release: snap the pill to its new anchor + offset and relayout."""
        cx = pill.x() + PILL_W / 2
        anchor, offset = center_x_to_anchor_offset(cx, self._on_air_x, self._off_air_x)
        pill.step.anchor = anchor
        pill.step.offset_s = offset
        pill.refresh()
        self.relayout()
        self.changed.emit()

    def edit_pill(self, pill: _TaskPill) -> None:
        dlg = _PillEditor(pill.step, self._editor, self)
        result = dlg.exec()
        if result == _PillEditor.REMOVE:
            self.remove_pill(pill)
            return
        if result == QDialog.DialogCode.Accepted:
            pill.refresh()
            self.relayout()
            self.changed.emit()

    # ── Painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        baseline = self.height() - BASELINE_FROM_BOTTOM
        on_x, off_x = int(self._on_air_x), int(self._off_air_x)

        # Baseline
        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 2))
        p.drawLine(EDGE_PAD // 2, baseline, self.width() - EDGE_PAD // 2, baseline)

        # Ticks + second labels in each zone
        tick_font = QFont(); tick_font.setPointSize(8)
        p.setFont(tick_font)
        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 1))
        self._paint_ticks(p, baseline, on_x, negative=True)    # warm-up (left of on-air)
        self._paint_ticks(p, baseline, off_x, negative=False)  # cool-down (right of off-air)

        # Anchor lines + labels
        self._paint_anchor(p, on_x, baseline, "ON-AIR", Palette.ONLINE)
        self._paint_anchor(p, off_x, baseline, "OFF-AIR", Palette.CRASH)

        # Zone captions
        cap_font = QFont(); cap_font.setPointSize(9); cap_font.setItalic(True)
        p.setFont(cap_font)
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(EDGE_PAD, 16, max(0, on_x - EDGE_PAD), 14,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "warm-up")
        p.drawText(on_x, baseline + 26, off_x - on_x, 14,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), "· on air ·")
        p.drawText(off_x, 16, max(0, self.width() - off_x - EDGE_PAD), 14,
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), "cool-down")
        p.end()

    def _paint_ticks(self, p: QPainter, baseline: int, anchor_x: int, negative: bool) -> None:
        step_px = TICK_S * SCALE
        i = 1
        while True:
            x = anchor_x - i * step_px if negative else anchor_x + i * step_px
            if negative and x < EDGE_PAD // 2:
                break
            if not negative and x > self.width() - EDGE_PAD // 2:
                break
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(int(x), baseline - 4, int(x), baseline + 4)
            p.setPen(QColor(Palette.TEXT_FAINT))
            label = f"{'-' if negative else '+'}{TICK_S * i}s"
            p.drawText(int(x) - 18, baseline + 6, 36, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)
            i += 1

    def _paint_anchor(self, p: QPainter, x: int, baseline: int, label: str, color: str) -> None:
        p.setPen(QPen(QColor(color), 2))
        p.drawLine(x, PILLS_TOP - 8, x, baseline + 6)
        badge_font = QFont(); badge_font.setPointSize(9); badge_font.setBold(True)
        p.setFont(badge_font)
        p.setPen(QColor(color))
        p.drawText(x - 60, baseline + 8, 120, 16,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)


# ── Small modal editor for one pill ──────────────────────────────────────────

class _PillEditor(QDialog):
    """Edit a single step: task, action, anchor, offset, args — or remove it."""

    REMOVE = 2   # custom result code (distinct from Accepted=1 / Rejected=0)

    def __init__(self, step: _TLStep, editor: "TimelineEditor", parent=None):
        super().__init__(parent)
        self._step = step
        self._editor = editor
        self.setWindowTitle("Edit step")
        self.setMinimumWidth(360)

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        # Task row: dropdown + an inline "New…" that creates a task on the unit.
        self._task = QComboBox()
        tasks = editor.available_tasks()
        if tasks:
            self._task.addItems(tasks)
        # Keep the current task selectable even if it isn't in the unit's list.
        if step.task_name and self._task.findText(step.task_name) < 0:
            self._task.addItem(step.task_name)
        if step.task_name:
            self._task.setCurrentText(step.task_name)
        self._new_task = QPushButton("New…")
        self._new_task.setFixedWidth(56)
        self._new_task.setToolTip("Create a new task on this unit without leaving the sequence")
        self._new_task.clicked.connect(self._on_new_task)
        task_row = QHBoxLayout()
        task_row.setContentsMargins(0, 0, 0, 0)
        task_row.setSpacing(6)
        task_row.addWidget(self._task, stretch=1)
        task_row.addWidget(self._new_task)
        task_host = QWidget()
        task_host.setLayout(task_row)
        form.addRow("Task", task_host)

        self._action = QComboBox()
        self._action.addItem("▶ start", "start")
        self._action.addItem("⏹ stop", "stop")
        self._action.setCurrentIndex(0 if step.action == "start" else 1)
        self._action.currentIndexChanged.connect(self._sync_args_enabled)
        form.addRow("Action", self._action)

        self._anchor = QComboBox()
        self._anchor.addItem("on-air (T0)", "start")
        self._anchor.addItem("off-air", "stop")
        self._anchor.setCurrentIndex(0 if step.anchor == "start" else 1)
        form.addRow("Anchor", self._anchor)

        self._offset = QDoubleSpinBox()
        self._offset.setRange(-100000.0, 100000.0)
        self._offset.setDecimals(1)
        self._offset.setSingleStep(1.0)
        self._offset.setSuffix(" s")
        self._offset.setValue(float(step.offset_s))
        form.addRow("Offset", self._offset)

        # Per-step arguments appended to the task's command on start (e.g. --gain 20),
        # so one task can be reused with different values.
        self._args = QLineEdit(shlex.join(step.args) if step.args else "")
        self._args.setPlaceholderText("e.g. --gain 20   (appended to the task's command)")
        form.addRow("Arguments", self._args)

        outer.addLayout(form)

        hint = QLabel("Negative offset = before the anchor; positive = after.  "
                      "Arguments apply to a start action only.")
        hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        buttons = QDialogButtonBox()
        remove = QPushButton("Remove")
        remove.setStyleSheet(f"color: {Palette.CRASH};")
        buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        remove.clicked.connect(lambda: self.done(self.REMOVE))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._sync_args_enabled()

    def _sync_args_enabled(self) -> None:
        self._args.setEnabled(self._action.currentData() == "start")

    def _on_new_task(self) -> None:
        name = self._editor.create_task_interactively()
        if name:
            if self._task.findText(name) < 0:
                self._task.addItem(name)
            self._task.setCurrentText(name)

    def _accept(self) -> None:
        self._step.task_name = self._task.currentText().strip()
        self._step.action = self._action.currentData()
        self._step.anchor = self._anchor.currentData()
        self._step.offset_s = round(self._offset.value(), 1)
        raw = self._args.text().strip()
        if self._step.action == "start" and raw:
            try:
                self._step.args = shlex.split(raw)
            except ValueError:
                self._step.args = raw.split()
        else:
            self._step.args = []
        self.accept()


# ── Public editor: toolbar + scrollable canvas ───────────────────────────────

class TimelineEditor(QWidget):
    """Toolbar (add buttons) above a horizontally-scrollable timeline canvas."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: List[str] = []
        # Optional hook set by the host dialog: opens a task editor and returns the
        # new task's name (or None). Lets a step create a task inline.
        self._task_creator: Optional[Callable[[], Optional[str]]] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        self._add_on = QPushButton("+ On-air task")
        self._add_off = QPushButton("+ Off-air task")
        self._add_on.clicked.connect(lambda: self._add("start"))
        self._add_off.clicked.connect(lambda: self._add("stop"))
        bar.addWidget(self._add_on)
        bar.addWidget(self._add_off)
        bar.addStretch(1)
        self._hint = QLabel("Drag pills to set timing · click a pill to edit")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        outer.addLayout(bar)

        self._canvas = _TimelineCanvas(self)
        self._canvas.changed.connect(self.changed.emit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        scroll.setWidget(self._canvas)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(240)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        outer.addWidget(scroll, stretch=1)

    # ── Task list (populated once the unit's tasks are fetched) ──────────────

    def set_tasks(self, names: List[str]) -> None:
        self._tasks = list(names)
        # Add buttons stay enabled even with no tasks: a step can create one inline
        # via the "New…" button in its editor.
        if not self._tasks:
            self._hint.setText("no tasks yet — add a step and use “New…” to create one")
        else:
            self._hint.setText("Drag pills to set timing · click a pill to edit")
        self._canvas.refresh_pills()

    def available_tasks(self) -> List[str]:
        return self._tasks

    def set_task_creator(self, fn: Callable[[], Optional[str]]) -> None:
        self._task_creator = fn

    def add_task(self, name: str) -> None:
        """Register a newly-created task name so pickers and validation see it."""
        if name and name not in self._tasks:
            self._tasks.append(name)
            self._canvas.refresh_pills()

    def create_task_interactively(self) -> Optional[str]:
        """Open the host's task editor; on success register + return the new name."""
        if self._task_creator is None:
            return None
        name = self._task_creator()
        if name:
            self.add_task(name)
        return name

    # ── Add / load / read steps ──────────────────────────────────────────────

    def _add(self, anchor: str) -> None:
        default_task = self._tasks[0] if self._tasks else ""
        # A sensible default action per anchor: on-air launches, off-air stops.
        action = "start" if anchor == "start" else "stop"
        self._canvas.add_step(
            _TLStep(anchor=anchor, offset_s=0.0, action=action, task_name=default_task),
            edit=True,
        )

    def set_steps(self, steps: List[m.SequenceStep]) -> None:
        self._canvas.clear()
        for s in steps:
            action = s.action.value if hasattr(s.action, "value") else str(s.action)
            self._canvas.add_step(
                _TLStep(anchor=s.anchor, offset_s=float(s.offset_s),
                        action=action, task_name=s.task_name,
                        args=list(getattr(s, "args", []) or [])))

    def seed_default(self) -> None:
        """Pre-populate the simplest valid sequence: one on-air, one off-air step."""
        t = self._tasks[0] if self._tasks else ""
        self._canvas.add_step(_TLStep("start", 0.0, "start", t))
        self._canvas.add_step(_TLStep("stop", 0.0, "stop", t))

    def steps(self) -> List[m.SequenceStep]:
        out: List[m.SequenceStep] = []
        for s in self._canvas.steps():
            out.append(m.SequenceStep(
                anchor=s.anchor,
                offset_s=s.offset_s,
                action=m.StepAction(s.action),
                task_name=s.task_name,
                args=list(s.args) if s.action == "start" else [],
            ))
        return out

    # ── Validation (mirrors the agent's _validate_steps) ─────────────────────

    def validate(self) -> Optional[str]:
        """Return an error string if invalid, else None."""
        steps = self._canvas.steps()
        if not steps:
            return "add at least one on-air and one off-air step"
        if any(not s.task_name for s in steps):
            return "every step needs a task selected"
        if self._tasks:
            unknown = sorted({s.task_name for s in steps if s.task_name not in self._tasks})
            if unknown:
                return "unknown task(s): " + ", ".join(unknown)
        if not any(s.anchor == "start" for s in steps):
            return "needs at least one on-air (start-anchored) step"
        if not any(s.anchor == "stop" for s in steps):
            return "needs at least one off-air (stop-anchored) step"
        return None
