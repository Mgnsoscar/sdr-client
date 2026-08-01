"""
Plan editor — build a cross-unit plan and its per-step parameter overrides.

Three dialogs, leaf-first:

  StepOverrideDialog
      Edit ONE step's parameters, reusing the shared ParamForm (task → script →
      GET /scripts/{script}/params → pre-fill from the step's current args). Used
      to capture a StepOverride without touching the stored sequence.

  PlanItemDialog
      One plan item: pick a unit, pick one of its sequences, then optionally
      override individual start/run steps' parameters. Returns a PlanItem.

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
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from . import timeline_model as tlm
from .param_form import ParamForm
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_editor import _TimelineCanvas, DRAG_THRESHOLD, LANES_TOP


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


def _action_of(step) -> str:
    return step.action.value if hasattr(step.action, "value") else str(step.action)


def _step_glyph(step) -> str:
    a = _action_of(step)
    return "▶" if a == "start" else ("⚡" if a == "run" else "⏹")


# ── One step's parameter override ────────────────────────────────────────────

class StepOverrideDialog(QDialog):
    """Edit a single step's args via the full parameter form, pre-filled from the
    step's current args. Returns the new args on accept (empty list is valid)."""

    def __init__(self, hub: DataHub, hostname: str, task_name: str,
                 command: List[str], current_args: List[str],
                 param_cache: Dict[str, list], parent=None):
        super().__init__(parent)
        self._hub = hub
        self._hostname = hostname
        self._task_name = task_name
        self._cache = param_cache
        self._pending_prefill = list(current_args)
        self.result_args: Optional[List[str]] = None

        self._script, _defaults = tlm.script_of_command(command)

        self.setWindowTitle(f"Override — {task_name}")
        self.setMinimumWidth(440)
        self._build()

        self._hub.task_done.connect(self._on_params)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load_params()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        head = QLabel(f"Parameters for <b>{self._task_name}</b>")
        head.setStyleSheet(f"font-size: 13px; color: {Palette.TEXT};")
        outer.addWidget(head)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._form = ParamForm()
        pscroll = QScrollArea()
        pscroll.setWidgetResizable(True)
        pscroll.setWidget(self._form)
        pscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        pscroll.setMinimumHeight(150)
        pscroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        outer.addWidget(pscroll, stretch=1)

        self._extra = QLineEdit()
        self._extra.setPlaceholderText("extra args not covered by the form (optional)")
        eform = QFormLayout(); eform.setContentsMargins(0, 0, 0, 0)
        eform.addRow("Extra args", self._extra)
        outer.addLayout(eform)

        hint = QLabel("These parameters apply only to this plan — the unit's sequence "
                      "is left unchanged.")
        hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ── Params load ──────────────────────────────────────────────────────────

    def _load_params(self) -> None:
        if not self._script:
            self._form.set_params([])
            self._apply_prefill()
            self._status.setText("this task has no script parameter schema — use extra args")
            return
        if self._script in self._cache:
            self._build_form()
            return
        self._status.setText(f"loading parameters for {self._script}…")
        self._form.set_params([])
        self._hub.run_async(
            f"planparam:{self._hostname}:{self._script}",
            lambda s=self._script: self._hub.fleet.get(self._hostname).get_script_params(s),
        )

    def _on_params(self, label: str, result) -> None:
        if not label.startswith("planparam:"):
            return
        parts = label.split(":", 2)
        if len(parts) < 3 or parts[1] != self._hostname or parts[2] != self._script:
            return
        if isinstance(result, Exception):
            self._status.setText(f"could not load parameters: {result}")
            return
        self._cache[self._script] = (result or {}).get("params", [])
        self._build_form()

    def _build_form(self) -> None:
        specs = self._cache.get(self._script, [])
        self._form.set_params(specs)
        self._status.setText("" if specs else "this script declares no parameters — use extra args")
        self._apply_prefill()

    def _apply_prefill(self) -> None:
        extra = self._form.set_values(self._pending_prefill)
        self._extra.setText(" ".join(shlex.quote(e) for e in extra) if extra else "")

    # ── Save ─────────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        err = self._form.validate()
        if err:
            self._status.setText(err)
            return
        args = self._form.build_args()
        raw = self._extra.text().strip()
        if raw:
            try:
                args = args + shlex.split(raw)
            except ValueError:
                args = args + raw.split()
        self.result_args = args
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_params)
        except (TypeError, RuntimeError):
            pass


# ── One plan item (unit + sequence + overrides) ──────────────────────────────

class _StepRow(QFrame):
    """A row for one sequence step inside the item dialog. Start/run steps get an
    Override / Reset control; stop steps are shown muted."""

    def __init__(self, index: int, step, overridable: bool,
                 on_override, on_reset):
        super().__init__()
        self.index = index
        self._on_override = on_override
        self._on_reset = on_reset
        self.setObjectName("card")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self._label = QLabel()
        self._label.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT};")
        self._label.setWordWrap(True)
        lay.addWidget(self._label, stretch=1)

        self._state = QLabel("")
        self._state.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        lay.addWidget(self._state)

        if overridable:
            self._btn = QPushButton("Override…")
            self._btn.setFixedWidth(94)
            self._btn.clicked.connect(lambda: self._on_override(self.index))
            lay.addWidget(self._btn)
            self._reset = QPushButton("Reset")
            self._reset.setFixedWidth(60)
            self._reset.clicked.connect(lambda: self._on_reset(self.index))
            lay.addWidget(self._reset)
        else:
            self._btn = self._reset = None

        self._step = step
        self._overridable = overridable

    def set_state(self, base_args: List[str], override_args: Optional[List[str]]) -> None:
        glyph = _step_glyph(self._step)
        name = self._step.task_name
        shown = override_args if override_args is not None else base_args
        argstr = " ".join(shown) if shown else "—"
        self._label.setText(f"{glyph} <b>{name}</b>  <span style='color:{Palette.TEXT_MUTED}'>"
                            f"{argstr}</span>")
        if not self._overridable:
            self._state.setText("no args")
            return
        if override_args is not None:
            self._state.setText("overridden")
            self._state.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
            self._reset.setEnabled(True)
        else:
            self._state.setText("default")
            self._state.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
            self._reset.setEnabled(False)


class PlanItemDialog(QDialog):
    """Pick a unit + one of its sequences, and optionally override start/run steps'
    parameters. Returns a PlanItem via .result_item on accept. When editing an
    existing item a Remove button is offered (result code REMOVE). The item's
    timeline placement (on/off-air offsets) is carried through unchanged."""

    REMOVE = 2   # custom result code (distinct from Accepted=1 / Rejected=0)

    def __init__(self, hub: DataHub, sequences_by_host: Dict[str, List[m.Sequence]],
                 item: Optional[m.PlanItem] = None, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._seqs = sequences_by_host
        self._item = item
        self.result_item: Optional[m.PlanItem] = None

        # Per-selected-unit state.
        self._task_commands: Dict[str, List[str]] = {}
        self._commands_ready = False
        self._param_cache: Dict[str, list] = {}
        self._overrides: Dict[int, m.StepOverride] = {}
        self._rows: Dict[int, _StepRow] = {}

        self.setWindowTitle("Edit plan item" if item else "Add plan item")
        self.setMinimumSize(560, 460)
        self._build()

        self._hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())

        self._populate_units()
        if item is not None:
            self._overrides = {ov.index: ov for ov in item.overrides}
            self._select_existing(item)
        else:
            self._on_unit_changed()

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
        self._seq.currentIndexChanged.connect(lambda _=0: self._on_seq_changed())
        form.addRow("Sequence", self._seq)
        outer.addLayout(form)

        lbl = QLabel("Steps")
        lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(lbl)

        self._steps_host = QWidget()
        self._steps_lay = QVBoxLayout(self._steps_host)
        self._steps_lay.setContentsMargins(0, 0, 0, 0)
        self._steps_lay.setSpacing(6)
        self._steps_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        sscroll = QScrollArea()
        sscroll.setWidgetResizable(True)
        sscroll.setWidget(self._steps_host)
        sscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(sscroll, stretch=1)

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

    # ── Unit / sequence selection ──────────────────────────────────────────────

    def _populate_units(self) -> None:
        self._unit.blockSignals(True)
        for hostname, seqs in self._seqs.items():
            try:
                label = self._hub.fleet.get(hostname).unit_id
            except KeyError:
                label = hostname
            self._unit.addItem(f"{label}", hostname)
        self._unit.blockSignals(False)

    def _select_existing(self, item: m.PlanItem) -> None:
        idx = self._unit.findData(item.hostname)
        if idx < 0:  # unit not currently in the fleet — add a stub entry
            self._unit.addItem(item.unit_label or item.hostname, item.hostname)
            idx = self._unit.findData(item.hostname)
        # Set the unit without firing _on_unit_changed (no preselect) — we drive it
        # explicitly below so the item's own sequence is selected exactly once.
        self._unit.blockSignals(True)
        self._unit.setCurrentIndex(idx)
        self._unit.blockSignals(False)
        self._on_unit_changed(preselect_seq=item.sequence_id)

    def _current_hostname(self) -> str:
        return self._unit.currentData() or ""

    def _on_unit_changed(self, preselect_seq: Optional[str] = None) -> None:
        hostname = self._current_hostname()
        # Reset per-unit caches; a new unit means new tasks/scripts.
        self._task_commands = {}
        self._commands_ready = False
        self._param_cache = {}

        self._seq.blockSignals(True)
        self._seq.clear()
        for s in self._seqs.get(hostname, []):
            self._seq.addItem(s.name or s.id, s.id)
        self._seq.blockSignals(False)

        if preselect_seq is not None:
            i = self._seq.findData(preselect_seq)
            if i < 0:  # the plan references a sequence no longer on the unit
                self._seq.addItem(f"{self._item.sequence_name or preselect_seq} (missing)",
                                  preselect_seq)
                i = self._seq.findData(preselect_seq)
            self._seq.setCurrentIndex(i)

        # Load the unit's task commands so the override editor can resolve scripts.
        if hostname:
            self._status.setText("loading task info…")
            self._hub.run_async(
                f"planyaml:{hostname}",
                lambda: self._hub.fleet.get(hostname).get_tasks_yaml())
        self._on_seq_changed()

    def _current_sequence(self) -> Optional[m.Sequence]:
        sid = self._seq.currentData()
        for s in self._seqs.get(self._current_hostname(), []):
            if s.id == sid:
                return s
        return None

    def _on_seq_changed(self) -> None:
        # Rebuild the step rows for the newly-selected sequence.
        while self._steps_lay.count():
            w = self._steps_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self._rows = {}

        seq = self._current_sequence()
        if seq is None:
            hint = QLabel("This unit has no sequences." if not self._seqs.get(
                self._current_hostname()) else "Pick a sequence.")
            hint.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            self._steps_lay.addWidget(hint)
            self._refresh_status()
            return

        for i, step in enumerate(seq.steps):
            overridable = _action_of(step) in ("start", "run")
            row = _StepRow(i, step, overridable, self._open_override, self._reset_override)
            self._rows[i] = row
            self._steps_lay.addWidget(row)
            self._sync_row(i)
        self._refresh_status()

    def _sync_row(self, index: int) -> None:
        seq = self._current_sequence()
        if seq is None or index not in self._rows:
            return
        base = list(seq.steps[index].args or [])
        ov = self._overrides.get(index)
        self._rows[index].set_state(base, ov.args if ov is not None else None)

    def _refresh_status(self) -> None:
        n = len(self._overrides)
        self._status.setText(f"{n} step(s) overridden" if n else "no overrides — runs as defined")

    # ── Overrides ──────────────────────────────────────────────────────────────

    def _open_override(self, index: int) -> None:
        if not self._commands_ready:
            self._status.setText("still loading task info — try again in a moment")
            return
        seq = self._current_sequence()
        if seq is None:
            return
        step = seq.steps[index]
        command = self._task_commands.get(step.task_name)
        if not command:
            self._status.setText(
                f"task '{step.task_name}' not found on this unit — cannot edit parameters")
            return
        ov = self._overrides.get(index)
        current = list(ov.args) if ov is not None else list(step.args or [])
        dlg = StepOverrideDialog(self._hub, self._current_hostname(), step.task_name,
                                 command, current, self._param_cache, parent=self)
        if dlg.exec() and dlg.result_args is not None:
            self._overrides[index] = m.StepOverride(
                index=index, args=dlg.result_args, replace_args=True)
            self._sync_row(index)
            self._refresh_status()

    def _reset_override(self, index: int) -> None:
        if self._overrides.pop(index, None) is not None:
            self._sync_row(index)
            self._refresh_status()

    # ── Result routing ─────────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        if label.startswith("planyaml:"):
            hostname = label.split(":", 1)[1]
            if hostname != self._current_hostname():
                return
            self._task_commands = ({} if isinstance(result, Exception)
                                   else _parse_task_commands(result))
            self._commands_ready = True
            self._refresh_status()

    # ── Save ─────────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        seq = self._current_sequence()
        if seq is None:
            self._status.setText("pick a sequence first")
            return
        hostname = self._current_hostname()
        try:
            label = self._hub.fleet.get(hostname).unit_id
        except KeyError:
            label = self._item.unit_label if self._item else hostname
        # Keep only overrides that still address a step in the chosen sequence.
        overrides = [self._overrides[i] for i in sorted(self._overrides)
                     if i < len(seq.steps)]
        # Carry the timeline placement through unchanged — it's set by dragging the
        # bar, not in this dialog.
        on_off = (self._item.on_air_offset_s, self._item.off_air_offset_s) \
            if self._item is not None else (0.0, 0.0)
        self.result_item = m.PlanItem(
            hostname=hostname, unit_label=label,
            sequence_id=seq.id, sequence_name=seq.name or seq.id,
            overrides=overrides,
            on_air_offset_s=on_off[0], off_air_offset_s=on_off[1])
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
    overrides: List[m.StepOverride] = field(default_factory=list)
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
        overrides=list(item.overrides),
        start_offset=max(0.0, item.on_air_offset_s),
        stop_offset=min(0.0, item.off_air_offset_s), uid=uid)


def _item_from_bar(bar: PlanBar) -> m.PlanItem:
    return m.PlanItem(
        hostname=bar.hostname, unit_label=bar.unit_label,
        sequence_id=bar.sequence_id, sequence_name=bar.sequence_name,
        overrides=list(bar.overrides),
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
            p.drawText(int(x) - 18, baseline + 6, 36, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       f"+{tick_s * i}s")
            i += 1
        i = 1
        while off_x - i * step > mid:
            x = off_x - i * step
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(int(x), baseline - 4, int(x), baseline + 4)
            p.setPen(QColor(Palette.TEXT_FAINT))
            p.drawText(int(x) - 18, baseline + 6, 36, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       f"-{tick_s * i}s")
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
            self._hint.setText("still loading units — try again in a moment")
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

    # ── Load sequences across the fleet ────────────────────────────────────────

    def _load_sequences(self) -> None:
        self._hub.run_async("planseqs", lambda: self._hub.fleet.sequences_all())

    def _on_task_done(self, label: str, result) -> None:
        if label != "planseqs":
            return
        self._seqs_loaded = True
        by_host: Dict[str, List[m.Sequence]] = {}
        if isinstance(result, dict):
            for host, val in result.items():
                by_host[host] = val if isinstance(val, list) else []
        self._seqs_by_host = by_host
        self._timeline.set_sequences(by_host)
        total = sum(len(v) for v in by_host.values())
        self._status.setText(
            f"{len(by_host)} unit(s), {total} sequence(s) available"
            if by_host else "no units reachable")

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
