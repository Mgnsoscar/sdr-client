"""
SequenceEditorDialog — create or edit a sequence for one unit.

Name + description on top, a visual drag-and-drop TimelineEditor as the body.
On open it fetches the unit's task list (so the timeline's task pickers are
populated). Creating pre-seeds the simplest valid sequence — one on-air step and
one off-air step — so the operator starts from something sensible; editing loads
the existing sequence's steps onto the timeline instead.

Client-side validation mirrors the agent's rules (≥1 on-air + ≥1 off-air step,
every step has a known task), so mistakes surface instantly instead of coming
back as a 400. Save builds a CreateSequenceRequest and calls create_sequence (or
update_sequence when editing), which the agent stores in sequences.json.

Network calls go through the DataHub's run_async and return on the shared
task_done signal, filtered here to this host + operations. The modal exec loop
still processes those queued signals, so results arrive while the dialog is open.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from .qt_adapter import DataHub
from .scope_selector import ScopeSelector
from .theme import Palette
from .timeline_editor import TimelineEditor


class SequenceEditorDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str,
                 sequence: Optional[m.Sequence] = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self._sequence = sequence            # None -> create, else edit
        self._editing = sequence is not None
        self._saving = False

        self.setWindowTitle("Edit sequence" if self._editing else "New sequence")
        self.setMinimumSize(780, 460)
        self._build()
        if self._editing:
            self._name.setText(sequence.name)
            self._desc.setText(sequence.description)
            self._timeline.set_steps(sequence.steps)
            if self._scope is not None:
                self._scope.set_from_types(getattr(sequence, "types", []) or [])
        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("unique sequence name")
        self._name.textChanged.connect(lambda _=0: self._revalidate())
        form.addRow("Name *", self._name)
        self._desc = QLineEdit()
        self._desc.setPlaceholderText("optional")
        form.addRow("Description", self._desc)

        # Library-only: which unit types this sequence targets. A live unit already
        # holds only its own sequences, so scope is meaningless there.
        self._scope: Optional[ScopeSelector] = None
        if self.hostname == LIBRARY_HOST:
            self._scope = ScopeSelector()
            form.addRow("Applies to", self._scope)
        outer.addLayout(form)

        self._timeline = TimelineEditor()
        self._timeline.changed.connect(self._revalidate)
        self._timeline.set_context(self.hub, self.hostname)
        outer.addWidget(self._timeline, stretch=1)

        self._status = QLabel("loading tasks…")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self.hub.run_async(
            f"seqdlg_tasks:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_tasks(),
        )
        # tasks.yaml gives each task's command, from which the step editor derives
        # the task's script (for its parameter form) and default arg values.
        self.hub.run_async(
            f"seqdlg_yaml:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml(),
        )

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("seqdlg_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "seqdlg_save":
            self._saving = False
            self._buttons.setEnabled(True)
            if isinstance(result, Exception):
                self._set_status(f"save failed: {result}", error=True)
            else:
                self.accept()
            return

        if op == "seqdlg_yaml":
            self._timeline.set_task_commands(self._parse_task_commands(result))
            return

        if op == "seqdlg_tasks":
            if isinstance(result, Exception):
                self._set_status(f"could not load tasks: {result}", error=True)
                self._timeline.set_tasks([])
                return
            names = [t.name for t in result] if isinstance(result, list) else []
            self._timeline.set_tasks(names)
            self._revalidate()

    # ── Validation / save ────────────────────────────────────────────────────

    def _revalidate(self) -> None:
        err = self._current_error()
        if err:
            self._set_status(err, warn=True)
        else:
            self._set_status("ready to save")

    def _current_error(self) -> str | None:
        if not self._name.text().strip():
            return "sequence name is required"
        return self._timeline.validate()

    def _on_save(self) -> None:
        if self._saving:
            return
        err = self._current_error()
        if err:
            self._set_status(err, error=True)
            return
        req = m.CreateSequenceRequest(
            name=self._name.text().strip(),
            description=self._desc.text().strip(),
            steps=self._timeline.steps(),
            types=self._scope.types() if self._scope is not None else [],
        )
        self._saving = True
        self._buttons.setEnabled(False)
        self._set_status("saving…")
        if self._editing:
            seq_id = self._sequence.id
            self.hub.run_async(
                f"seqdlg_save:{self.hostname}:{req.name}",
                lambda: self.hub.fleet.get(self.hostname).update_sequence(seq_id, req),
            )
        else:
            self.hub.run_async(
                f"seqdlg_save:{self.hostname}:{req.name}",
                lambda: self.hub.fleet.get(self.hostname).create_sequence(req),
            )

    @staticmethod
    def _parse_task_commands(result) -> Dict[str, List[str]]:
        """task_name -> command list, parsed from a tasks.yaml document."""
        if not isinstance(result, str) or not result.strip():
            return {}
        try:
            doc = yaml.safe_load(result) or {}
        except yaml.YAMLError:
            return {}
        out: Dict[str, List[str]] = {}
        for entry in (doc.get("tasks") or []):
            name = entry.get("name")
            cmd = entry.get("command")
            if name and isinstance(cmd, list):
                out[name] = list(cmd)
        return out

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _set_status(self, text: str, error: bool = False, warn: bool = False) -> None:
        color = Palette.CRASH if error else (Palette.ARMED if warn else Palette.TEXT_FAINT)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    def _disconnect(self) -> None:
        try:
            self.hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
