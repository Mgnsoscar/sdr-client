"""
Plan editor — build a cross-unit plan and its per-step parameter overrides.

Dialogs, leaf-first:

  PlanItemDialog
      One plan sequence: pick a unit + a source sequence to copy, place it in the
      plan's on-air window, and edit the copy's steps — task timing AND parameters
      — with the full sequence TimelineEditor (the same editor as the unit's
      Sequences tab). The edited steps are the plan's own copy; the unit's stored
      sequence is untouched. Returns a PlanItem whose .steps hold the copy.

  PlanTimelineEditor (+ _PlanCanvas, reusing the sequence timeline canvas)
      A visual timeline of the plan: each sequence is a duration bar with an
      on-air handle (offset from the plan's ON-AIR / T0) and an off-air handle
      (offset from the plan's OFF-AIR / T_end). Placement is relative — absolute
      times come later, when a plan is scheduled. Drag to place; click to edit
      which sequence + its overrides (via PlanItemDialog).

  PlanEditorDialog
      The whole plan: name, description, and the sequence timeline. Persists via
      the caller's PlanStore.

Everything runs off the DataHub's run_async / task_done pattern; the modal exec
loops still pump those queued signals, so async results arrive while a dialog is
open. The sequences of every unit are fetched once by PlanEditorDialog and handed
down, so the item dialog opens instantly.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from . import timeline_model as tlm
from .duration_spin import DurationSpinBox
from .param_form import fmt_duration
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_editor import _TimelineCanvas, TimelineEditor, DRAG_THRESHOLD, LANES_TOP


def _parse_task_commands(yaml_text) -> Dict[str, List[str]]:
    """task_name -> command list, parsed from a tasks.yaml document."""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return {}
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}
    out: Dict[str, List[str]] = {}
    for entry in (doc.get("tasks") or []):
        name = entry.get("name")
        cmd = entry.get("command")
        if name and isinstance(cmd, list):
            out[name] = list(cmd)
    return out


class PlanItemDialog(QDialog):
    """Add or edit one sequence in a plan. Pick the unit and a source sequence to
    copy, place it in the plan's on-air window (on/off-air offsets), and edit the
    copy's steps — task timing AND parameters — with the full sequence timeline
    (the same editor as the unit's Sequences tab). The edited steps are the plan's
    OWN copy: the unit's stored sequence is never touched. Returns a PlanItem whose
    .steps hold the copy. Remove is offered when editing (result code REMOVE)."""

    REMOVE = 2   # custom result code (distinct from Accepted=1 / Rejected=0)

    def __init__(self, hub: DataHub, sequences_by_host: Dict[str, List[m.Sequence]],
                 item: Optional[m.PlanItem] = None, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._seqs = sequences_by_host
        self._item = item
        self.result_item: Optional[m.PlanItem] = None
        self._guard = True   # suppress reseeding while combos are set programmatically

        self.setWindowTitle("Edit plan sequence" if item else "Add plan sequence")
        self.setMinimumSize(840, 640)
        self._build()

        self._hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())

        self._populate_units()
        if item is not None:
            self._on_air.setValue(item.on_air_offset_s)
            self._off_air.setValue(item.off_air_offset_s)
            self._select_combos(item.hostname, item.sequence_id)
            if item.steps:                       # an existing plan-local copy
                self._timeline.set_steps(item.steps)
            else:                                # legacy item — seed from the source
                self._seed_from_source(legacy_overrides=item.overrides)
            self._load_unit_meta(item.hostname)
        else:                                    # new — copy the first unit's first seq
            self._on_unit_changed()
            self._seed_from_source()
        self._guard = False

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        self._unit = QComboBox()
        self._unit.currentIndexChanged.connect(lambda _=0: self._on_unit_changed())
        form.addRow("Unit", self._unit)
        self._seq = QComboBox()
        self._seq.currentIndexChanged.connect(lambda _=0: self._on_source_changed())
        self._seq.setToolTip("The sequence to copy into this plan. Picking one loads "
                             "its steps below; editing them changes only this plan.")
        form.addRow("Copy of sequence", self._seq)

        # Placement within the plan's on-air window (same values as dragging the
        # bar's handles). On-air is measured forward from ON-AIR (≥ 0); off-air is
        # measured back from OFF-AIR (≤ 0).
        self._on_air = DurationSpinBox()
        self._on_air.setRange(0.0, 100000.0)      # on-air offset is measured forward (≥ 0)
        form.addRow("On-air — after ON-AIR", self._on_air)
        self._off_air = DurationSpinBox()
        self._off_air.setRange(-100000.0, 0.0)    # off-air offset is measured back (≤ 0)
        form.addRow("Off-air — before OFF-AIR", self._off_air)
        outer.addLayout(form)

        # The full sequence timeline over the plan-local step copy.
        self._timeline = TimelineEditor()
        self._timeline.changed.connect(self._refresh_status)
        outer.addWidget(self._timeline, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        if self._item is not None:
            remove = QPushButton("Remove")
            remove.setStyleSheet(f"color: {Palette.CRASH};")
            self._buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: self.done(self.REMOVE))
        self._buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    # ── Unit / source selection ────────────────────────────────────────────────

    def _populate_units(self) -> None:
        self._unit.blockSignals(True)
        for hostname in self._seqs:
            try:
                label = self._hub.fleet.get(hostname).label
            except KeyError:
                label = hostname
            self._unit.addItem(f"{label}", hostname)
        self._unit.blockSignals(False)

    def _select_combos(self, hostname: str, sequence_id: str) -> None:
        """Set the unit + source combos to an existing item without triggering a
        reseed (we drive the timeline explicitly)."""
        self._unit.blockSignals(True)
        idx = self._unit.findData(hostname)
        if idx < 0:   # unit not currently in the fleet — add a stub entry
            self._unit.addItem((self._item.unit_label if self._item else "") or hostname,
                               hostname)
            idx = self._unit.findData(hostname)
        self._unit.setCurrentIndex(idx)
        self._unit.blockSignals(False)

        self._seq.blockSignals(True)
        self._seq.clear()
        for s in self._seqs.get(hostname, []):
            self._seq.addItem(s.name or s.id, s.id)
        i = self._seq.findData(sequence_id)
        if i < 0:     # the source sequence is no longer on the unit
            self._seq.addItem(f"{(self._item.sequence_name if self._item else '') or sequence_id} "
                              f"(missing)", sequence_id)
            i = self._seq.findData(sequence_id)
        self._seq.setCurrentIndex(i)
        self._seq.blockSignals(False)

    def _current_hostname(self) -> str:
        return self._unit.currentData() or ""

    def _on_unit_changed(self) -> None:
        hostname = self._current_hostname()
        self._seq.blockSignals(True)
        self._seq.clear()
        for s in self._seqs.get(hostname, []):
            self._seq.addItem(s.name or s.id, s.id)
        self._seq.blockSignals(False)
        self._load_unit_meta(hostname)
        if not self._guard:
            # A user-driven unit change picks a new source, so reseed from it (the
            # previous unit's tasks won't exist here).
            self._seed_from_source()

    def _on_source_changed(self) -> None:
        if not self._guard:
            self._seed_from_source()

    def _current_source(self) -> Optional[m.Sequence]:
        sid = self._seq.currentData()
        for s in self._seqs.get(self._current_hostname(), []):
            if s.id == sid:
                return s
        return None

    def _load_unit_meta(self, hostname: str) -> None:
        """Feed the step editor from the shared LIBRARY, not the selected unit:
        every unit runs the same library tasks/scripts (they differ only in the
        parameters this plan sets), so the task list, their commands, and each
        script's parameter schema all come from the library — which means a plan
        can be authored with no unit connected. The selected unit only decides
        which unit this parameterized copy will be armed on later."""
        self._timeline.set_context(self._hub, LIBRARY_HOST)
        try:
            lib = self._hub.fleet.get(LIBRARY_HOST)
        except KeyError:
            self._timeline.set_tasks([])
            self._timeline.set_task_commands({})
            return
        try:
            names = [t.name for t in lib.list_tasks()]
        except Exception:  # noqa: BLE001 — an empty library still authors
            names = []
        self._timeline.set_tasks(names)
        try:
            cmds = _parse_task_commands(lib.get_tasks_yaml())
        except Exception:  # noqa: BLE001
            cmds = {}
        self._timeline.set_task_commands(cmds)

    def _seed_from_source(self, legacy_overrides: Optional[List[m.StepOverride]] = None) -> None:
        """Load the timeline with a fresh COPY of the selected source sequence's
        steps (never the source objects themselves). Legacy per-arg overrides, if
        given, are applied onto the copy so an old plan migrates cleanly."""
        seq = self._current_source()
        if seq is None:
            self._timeline.set_steps([])
            self._refresh_status()
            return
        steps = [s.model_copy(deep=True) for s in seq.steps]
        for ov in (legacy_overrides or []):
            if 0 <= ov.index < len(steps):
                steps[ov.index].args = list(ov.args)
                steps[ov.index].replace_args = ov.replace_args
        self._timeline.set_steps(steps)
        self._refresh_status()

    def _refresh_status(self) -> None:
        err = self._timeline.validate()
        if err:
            self._status.setText(err)
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
        else:
            n = len(self._timeline.steps())
            self._status.setText(f"{n} step(s) — ready")
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

    # ── Result routing ─────────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        parts = label.split(":", 1)
        if len(parts) != 2 or parts[1] != self._current_hostname():
            return
        if parts[0] == "plantasks":
            names = [t.name for t in result] if isinstance(result, list) else []
            self._timeline.set_tasks(names)
        elif parts[0] == "planyaml":
            self._timeline.set_task_commands(
                {} if isinstance(result, Exception) else _parse_task_commands(result))

    # ── Save ─────────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        sid = self._seq.currentData()
        if not sid:
            self._status.setText("pick a source sequence")
            return
        err = self._timeline.validate()
        if err:
            self._status.setText(err)
            return
        hostname = self._current_hostname()
        try:
            label = self._hub.fleet.get(hostname).label
        except KeyError:
            label = self._item.unit_label if self._item else hostname
        src = self._current_source()
        seq_name = src.name if src is not None else (
            self._item.sequence_name if self._item else self._seq.currentText())
        self.result_item = m.PlanItem(
            hostname=hostname, unit_label=label,
            sequence_id=sid, sequence_name=seq_name or sid,
            steps=self._timeline.steps(), overrides=[],
            on_air_offset_s=round(self._on_air.value(), 1),
            off_air_offset_s=round(self._off_air.value(), 1))
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass


# ── The plan timeline (sequences placed on the on-air / off-air anchors) ──────

_bar_ids = itertools.count(1)


@dataclass
class PlanBar:
    """One sequence placed on the plan timeline. Its on-air (start_offset, from the
    plan's ON-AIR anchor) and off-air (stop_offset, from the plan's OFF-AIR anchor)
    are RELATIVE placements — the absolute times are set when a plan is scheduled.

    The geometry attributes (kind/start_offset/stop_offset/uid/args/task_name) match
    a timeline_model bar so the shared canvas paints and drags it unchanged; args is
    always empty (parameter overrides live inside the sequence, edited in the
    dialog), which keeps the canvas's caret / arg-panel machinery inert."""
    hostname: str
    unit_label: str
    sequence_id: str
    sequence_name: str
    steps: List[m.SequenceStep] = field(default_factory=list)   # plan-local copy
    overrides: List[m.StepOverride] = field(default_factory=list)  # legacy (steps-less)
    start_offset: float = 0.0     # on-air offset, from plan T0     (anchor="start")
    stop_offset: float = 0.0      # off-air offset, from plan T_end (anchor="stop")
    uid: int = 0
    kind: str = "bar"
    args: list = field(default_factory=list)   # unused; keeps the canvas caret inert
    task_name: str = ""                         # the canvas paints this as the label

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_bar_ids)
        self.task_name = f"{self.unit_label or self.hostname} · " \
                         f"{self.sequence_name or self.sequence_id}"


def _bar_from_item(item: m.PlanItem, uid: int = 0) -> PlanBar:
    # Sequences live inside the plan's on-air window: on-air at/after ON-AIR
    # (offset ≥ 0), off-air at/before OFF-AIR (offset ≤ 0). Clamp on load in case
    # an older plan placed a bar outside it.
    return PlanBar(
        hostname=item.hostname, unit_label=item.unit_label,
        sequence_id=item.sequence_id, sequence_name=item.sequence_name,
        steps=list(item.steps), overrides=list(item.overrides),
        start_offset=max(0.0, item.on_air_offset_s),
        stop_offset=min(0.0, item.off_air_offset_s), uid=uid)


def _item_from_bar(bar: PlanBar) -> m.PlanItem:
    return m.PlanItem(
        hostname=bar.hostname, unit_label=bar.unit_label,
        sequence_id=bar.sequence_id, sequence_name=bar.sequence_name,
        steps=list(bar.steps), overrides=list(bar.overrides),
        on_air_offset_s=bar.start_offset, off_air_offset_s=bar.stop_offset)


class _PlanCanvas(_TimelineCanvas):
    """The sequence timeline canvas, reused for plan bars — but showing ONLY the
    on-air window. A bar is a whole sequence, edited via PlanItemDialog; sequences
    are confined to the window (on-air ≥ plan ON-AIR, off-air ≤ plan OFF-AIR), and
    the warm-up / cool-down zones are dropped."""

    def add_new(self, kind: str = "bar") -> None:
        self._editor.add_sequence()

    def edit_item(self, item) -> None:
        self._editor.edit_sequence(item)

    # ── Window-only geometry (ON-AIR at the left, OFF-AIR at the right) ────────

    def _compute_anchors(self):
        eff = self._eff()
        max_on = max((it.start_offset for it in self._items if it.start_offset > 0),
                     default=0.0)
        max_off = max((-it.stop_offset for it in self._items if it.stop_offset < 0),
                      default=0.0)
        band = max(tlm.MIDDLE_GAP * self._zoom,
                   (max_on + max_off) * eff + tlm.BAND_PAD * self._zoom)
        on_air_x = float(tlm.EDGE_PAD)
        off_air_x = on_air_x + band
        width = int(off_air_x + tlm.EDGE_PAD)
        return on_air_x, off_air_x, width

    # ── Drag, confined to the window ───────────────────────────────────────────

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        if self._drag is None:
            hit = self._hit(pos.x(), pos.y())
            if hit and hit[1] in ("bar_start", "bar_stop"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hit:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag["part"] not in ("bar_start", "bar_stop", "bar_body"):
            return
        if not self._drag["moved"] and abs(pos.x() - self._drag["press_x"]) < DRAG_THRESHOLD:
            return
        self._drag["moved"] = True
        it, part = self._drag["item"], self._drag["part"]
        x = pos.x()
        eff = self._eff()
        mid = tlm.midpoint(self._on, self._off)
        max_start = (mid - self._on) / eff      # on-air handle can't cross the middle
        min_stop = (mid - self._off) / eff      # off-air handle can't cross the middle
        if part == "bar_start":
            v = tlm.resolve_bar_start(x, self._on, self._off, self._zoom)
            it.start_offset = min(max(0.0, v), max_start)   # in [ON-AIR, middle]
        elif part == "bar_stop":
            v = tlm.resolve_bar_stop(x, self._on, self._off, self._zoom)
            it.stop_offset = max(min(0.0, v), min_stop)     # in [middle, OFF-AIR]
        elif part == "bar_body":
            ds = tlm._snap((x - self._drag["press_x"]) / eff)
            s0, p0 = self._drag["start0"], self._drag["stop0"]
            # Rigid shift: keep on-air in [0, middle] and off-air in [middle, 0].
            lo = max(-s0, min_stop - p0)
            hi = min(max_start - s0, -p0)
            ds = max(lo, min(hi, ds)) if lo <= hi else 0.0
            it.start_offset = s0 + ds
            it.stop_offset = p0 + ds
        self._live_relayout(it)

    # ── Window-only painting (no warm-up / cool-down) ─────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        baseline = int(self._baseline)
        on_x, off_x = int(self._on), int(self._off)

        # The on-air window is the whole stage.
        p.fillRect(on_x, LANES_TOP - 10, off_x - on_x, baseline - (LANES_TOP - 10),
                   QColor(Palette.ONLINE_SOFT))
        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 2))
        p.drawLine(on_x, baseline, off_x, baseline)

        tick_font = QFont(); tick_font.setPointSize(8)
        p.setFont(tick_font)
        self._paint_window_ticks(p, baseline, on_x, off_x)

        self._paint_anchor(p, on_x, baseline, "ON-AIR", Palette.ONLINE)
        self._paint_anchor(p, off_x, baseline, "OFF-AIR", Palette.CRASH)

        cap_font = QFont(); cap_font.setPointSize(9); cap_font.setItalic(True)
        p.setFont(cap_font)
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(on_x, baseline + 26, off_x - on_x, 14,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                   "· on-air window ·")

        for it in self._items:
            self._paint_bar(p, it)
        p.end()

    def _paint_window_ticks(self, p, baseline, on_x, off_x):
        """Ticks inside the window: seconds from ON-AIR on the left half, seconds
        before OFF-AIR on the right half (they meet, without overlapping, at the
        middle)."""
        eff = self._eff()
        tick_s = self._tick_interval()
        step = tick_s * eff
        mid = (on_x + off_x) / 2.0
        i = 1
        while on_x + i * step < mid:
            x = on_x + i * step
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(int(x), baseline - 4, int(x), baseline + 4)
            p.setPen(QColor(Palette.TEXT_FAINT))
            p.drawText(int(x) - 27, baseline + 6, 54, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       fmt_duration(tick_s * i, signed=True, compact=True))
            i += 1
        i = 1
        while off_x - i * step > mid:
            x = off_x - i * step
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(int(x), baseline - 4, int(x), baseline + 4)
            p.setPen(QColor(Palette.TEXT_FAINT))
            p.drawText(int(x) - 27, baseline + 6, 54, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       fmt_duration(-(tick_s * i), signed=True, compact=True))
            i += 1


class PlanTimelineEditor(QWidget):
    """Toolbar (+ Sequence, zoom) above the shared timeline canvas, showing whole
    sequences as duration bars anchored to the plan's on-air / off-air. Placement
    is by dragging; clicking a bar edits which sequence + its overrides."""

    changed = pyqtSignal()

    def __init__(self, hub: DataHub, sequences_by_host: Dict[str, List[m.Sequence]],
                 parent=None):
        super().__init__(parent)
        self._hub = hub
        self._seqs = sequences_by_host

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        self._add = QPushButton("+ Sequence")
        self._add.setToolTip("Add a sequence from any unit to this plan")
        self._add.clicked.connect(lambda: self._canvas.add_new())
        bar.addWidget(self._add)
        bar.addStretch(1)
        self._hint = QLabel("Drag handles to place each sequence · click to edit")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        self._zoom_btn = QPushButton("100%")
        self._zoom_btn.setFixedWidth(52)
        self._zoom_btn.setFlat(True)
        self._zoom_btn.setToolTip("Horizontal zoom — Ctrl+scroll or pinch. Click to reset.")
        self._zoom_btn.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        self._zoom_btn.clicked.connect(lambda: self._canvas.reset_zoom())
        bar.addWidget(self._zoom_btn)
        outer.addLayout(bar)

        self._canvas = _PlanCanvas(self)
        self._canvas.changed.connect(self.changed.emit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._canvas)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(240)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        self._canvas.set_scroll_area(scroll)
        outer.addWidget(scroll, stretch=1)

    # Hooks the shared canvas expects from its "editor".
    def available_tasks(self) -> List[str]:
        return []   # bars are sequences, always drawn as known

    def _sync_zoom(self) -> None:
        self._zoom_btn.setText(f"{round(self._canvas._zoom * 100)}%")

    # ── Bars ⇄ plan items ──────────────────────────────────────────────────────

    def set_sequences(self, sequences_by_host: Dict[str, List[m.Sequence]]) -> None:
        self._seqs = sequences_by_host

    def set_items(self, items: List[m.PlanItem]) -> None:
        self._canvas.set_items([_bar_from_item(it) for it in items])

    def items(self) -> List[m.PlanItem]:
        return [_item_from_bar(b) for b in self._canvas.items()]

    def is_empty(self) -> bool:
        return not self._canvas.items()

    # ── Add / edit a sequence bar ──────────────────────────────────────────────

    def add_sequence(self) -> None:
        if not self._seqs:
            self._hint.setText("no units configured — add units in units.yaml first")
            return
        dlg = PlanItemDialog(self._hub, self._seqs, parent=self.window())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self._canvas.add_item(_bar_from_item(dlg.result_item))

    def edit_sequence(self, bar: PlanBar) -> None:
        dlg = PlanItemDialog(self._hub, self._seqs, item=_item_from_bar(bar),
                             parent=self.window())
        r = dlg.exec()
        if r == PlanItemDialog.REMOVE:
            self._canvas.remove_item(bar.uid)
        elif r == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self._canvas.replace_item(bar.uid, _bar_from_item(dlg.result_item, uid=bar.uid))


# ── The whole plan ───────────────────────────────────────────────────────────

class PlanEditorDialog(QDialog):
    """Create or edit a plan: name, description, and a list of unit+sequence items.
    Fetches every unit's sequences once so item dialogs open instantly. On accept,
    the finished Plan is available as .result_plan (the caller persists it)."""

    def __init__(self, hub: DataHub, plan: Optional[m.Plan] = None, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._editing = plan is not None
        self._plan_id = plan.id if plan else None
        self._seqs_by_host: Dict[str, List[m.Sequence]] = {}
        self._seqs_loaded = False
        self.result_plan: Optional[m.Plan] = None

        self.setWindowTitle("Edit plan" if self._editing else "New plan")
        self.setMinimumSize(820, 560)
        self._build()
        if plan is not None:
            self._name.setText(plan.name)
            self._desc.setText(plan.description)
            # Bars render straight from the stored items (they carry cached unit /
            # sequence labels), so the timeline is populated before sequences load.
            self._timeline.set_items(plan.items)

        self._hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load_sequences()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("unique plan name")
        form.addRow("Name *", self._name)
        self._desc = QLineEdit()
        self._desc.setPlaceholderText("optional")
        form.addRow("Description", self._desc)
        outer.addLayout(form)

        self._timeline = PlanTimelineEditor(self._hub, self._seqs_by_host)
        outer.addWidget(self._timeline, stretch=1)

        self._status = QLabel("loading units…")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    # ── Load sequences from the shared library ─────────────────────────────────

    def _load_sequences(self) -> None:
        """Seed the item picker from the LIBRARY, not from live units. Every unit
        runs the same shared sequences (they differ only in the parameters a plan
        sets), so the source-sequence list is unit-independent and the same library
        set is offered for every configured unit. This is a local read, so a plan
        can be built with no unit connected. The unit list is the fleet's configured
        units (from units.yaml) — present whether or not they're reachable."""
        try:
            lib_seqs = self._hub.fleet.get(LIBRARY_HOST).list_sequences()
        except Exception:  # noqa: BLE001 — no library ⇒ author with none
            lib_seqs = []
        hosts = self._hub.fleet.hostnames()
        # Offer each unit only the sequences its type would actually receive (shared
        # sequences plus ones scoped to its kind) — a plan can't arm a sequence a
        # unit was never deployed.
        def _for(host: str) -> List[m.Sequence]:
            try:
                utype = self._hub.fleet.get(host).unit_type
            except Exception:  # noqa: BLE001
                utype = m.DEFAULT_UNIT_TYPE
            return [s for s in lib_seqs if m.applies_to_type(s.types, utype)]
        by_host: Dict[str, List[m.Sequence]] = {h: _for(h) for h in hosts}
        self._seqs_loaded = True
        self._seqs_by_host = by_host
        self._timeline.set_sequences(by_host)
        n_seq = len(lib_seqs)
        if not hosts:
            self._status.setText(f"{n_seq} library sequence(s) · no units configured")
        elif n_seq == 0:
            self._status.setText(f"{len(hosts)} unit(s) · no sequences in the library — "
                                 f"create one in the Library first")
        else:
            self._status.setText(
                f"{len(hosts)} unit(s) · {n_seq} library sequence(s) available")

    def _on_task_done(self, label: str, result) -> None:
        # The library seed is synchronous now; nothing async to route here. Kept
        # for the connect()/disconnect() symmetry the dialog relies on.
        return

    # ── Save ─────────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._status.setText("plan name is required")
            return
        if self._timeline.is_empty():
            self._status.setText("add at least one sequence")
            return
        from state import new_plan_id
        self.result_plan = m.Plan(
            id=self._plan_id or new_plan_id(),
            name=name,
            description=self._desc.text().strip(),
            items=self._timeline.items(),
        )
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
