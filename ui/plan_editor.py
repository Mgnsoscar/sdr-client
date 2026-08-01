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

  PlanEditorDialog
      The whole plan: name, description, and a list of items. Persists via the
      caller's PlanStore.

Everything runs off the DataHub's run_async / task_done pattern; the modal exec
loops still pump those queued signals, so async results arrive while a dialog is
open. The sequences of every unit are fetched once by PlanEditorDialog and handed
down, so the item dialog opens instantly.
"""
from __future__ import annotations

import shlex
from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from . import timeline_model as tlm
from .param_form import ParamForm
from .qt_adapter import DataHub
from .theme import Palette


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
    parameters. Returns a PlanItem via .result_item on accept."""

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

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
        self.result_item = m.PlanItem(
            hostname=hostname, unit_label=label,
            sequence_id=seq.id, sequence_name=seq.name or seq.id,
            overrides=overrides)
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass


# ── The whole plan ───────────────────────────────────────────────────────────

class _ItemRow(QFrame):
    """A row summarising one plan item in the plan editor."""

    def __init__(self, item: m.PlanItem, on_edit, on_remove):
        super().__init__()
        self.item = item
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        box = QVBoxLayout(); box.setSpacing(2)
        title = QLabel(f"<b>{item.unit_label or item.hostname}</b>  ·  {item.sequence_name}")
        title.setStyleSheet(f"font-size: 13px; color: {Palette.TEXT};")
        box.addWidget(title)
        n = len(item.overrides)
        sub = QLabel(f"{n} parameter override(s)" if n else "runs as defined")
        sub.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        box.addWidget(sub)
        lay.addLayout(box, stretch=1)

        edit = QPushButton("Edit"); edit.setFixedWidth(60)
        edit.clicked.connect(lambda: on_edit(item))
        lay.addWidget(edit)
        rm = QPushButton("Remove"); rm.setFixedWidth(72)
        rm.clicked.connect(lambda: on_remove(item))
        lay.addWidget(rm)


class PlanEditorDialog(QDialog):
    """Create or edit a plan: name, description, and a list of unit+sequence items.
    Fetches every unit's sequences once so item dialogs open instantly. On accept,
    the finished Plan is available as .result_plan (the caller persists it)."""

    def __init__(self, hub: DataHub, plan: Optional[m.Plan] = None, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._editing = plan is not None
        self._items: List[m.PlanItem] = list(plan.items) if plan else []
        self._plan_id = plan.id if plan else None
        self._seqs_by_host: Dict[str, List[m.Sequence]] = {}
        self._seqs_loaded = False
        self.result_plan: Optional[m.Plan] = None

        self.setWindowTitle("Edit plan" if self._editing else "New plan")
        self.setMinimumSize(620, 500)
        self._build()
        if plan is not None:
            self._name.setText(plan.name)
            self._desc.setText(plan.description)

        self._hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load_sequences()
        self._rebuild_items()

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

        head = QHBoxLayout()
        lbl = QLabel("Sequences in this plan")
        lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {Palette.TEXT};")
        head.addWidget(lbl)
        head.addStretch(1)
        self._add_btn = QPushButton("Add item…")
        self._add_btn.setObjectName("primary")
        self._add_btn.clicked.connect(self._on_add)
        head.addWidget(self._add_btn)
        outer.addLayout(head)

        self._items_host = QWidget()
        self._items_lay = QVBoxLayout(self._items_host)
        self._items_lay.setContentsMargins(0, 0, 0, 0)
        self._items_lay.setSpacing(8)
        self._items_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        iscroll = QScrollArea()
        iscroll.setWidgetResizable(True)
        iscroll.setWidget(self._items_host)
        iscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(iscroll, stretch=1)

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
        total = sum(len(v) for v in by_host.values())
        self._status.setText(
            f"{len(by_host)} unit(s), {total} sequence(s) available"
            if by_host else "no units reachable")

    # ── Items ──────────────────────────────────────────────────────────────────

    def _rebuild_items(self) -> None:
        while self._items_lay.count():
            w = self._items_lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if not self._items:
            empty = QLabel("No sequences yet. Click “Add item…” to add one from a unit.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            self._items_lay.addWidget(empty)
            return
        for item in self._items:
            self._items_lay.addWidget(_ItemRow(item, self._on_edit_item, self._on_remove_item))

    def _on_add(self) -> None:
        if not self._seqs_loaded:
            self._status.setText("still loading units — try again in a moment")
            return
        dlg = PlanItemDialog(self._hub, self._seqs_by_host, parent=self)
        if dlg.exec() and dlg.result_item is not None:
            self._items.append(dlg.result_item)
            self._rebuild_items()

    def _on_edit_item(self, item: m.PlanItem) -> None:
        dlg = PlanItemDialog(self._hub, self._seqs_by_host, item=item, parent=self)
        if dlg.exec() and dlg.result_item is not None:
            idx = self._items.index(item)
            self._items[idx] = dlg.result_item
            self._rebuild_items()

    def _on_remove_item(self, item: m.PlanItem) -> None:
        self._items = [i for i in self._items if i is not item]
        self._rebuild_items()

    # ── Save ─────────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._status.setText("plan name is required")
            return
        if not self._items:
            self._status.setText("add at least one sequence")
            return
        from state import new_plan_id
        self.result_plan = m.Plan(
            id=self._plan_id or new_plan_id(),
            name=name,
            description=self._desc.text().strip(),
            items=self._items,
        )
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
