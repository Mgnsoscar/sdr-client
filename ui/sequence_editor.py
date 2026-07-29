"""
SequenceEditorDialog — create a new sequence for one unit.

Name + description on top, a visual drag-and-drop TimelineEditor as the body.
On open it fetches the unit's task list (so the timeline's task pickers are
populated) and pre-seeds the simplest valid sequence — one on-air step and one
off-air step — so the operator starts from something sensible.

Client-side validation mirrors the agent's rules (≥1 on-air + ≥1 off-air step,
every step has a known task), so mistakes surface instantly instead of coming
back as a 400. Save builds a CreateSequenceRequest and calls create_sequence,
which the agent stores in sequences.json.

Network calls go through the DataHub's run_async and return on the shared
task_done signal, filtered here to this host + operations. The modal exec loop
still processes those queued signals, so results arrive while the dialog is open.
"""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from api import models as m
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_editor import TimelineEditor


class SequenceEditorDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self._saving = False
        self._seeded = False

        self.setWindowTitle("New sequence")
        self.setMinimumSize(780, 460)
        self._build()
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
        outer.addLayout(form)

        self._timeline = TimelineEditor()
        self._timeline.changed.connect(self._revalidate)
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

        if op == "seqdlg_tasks":
            if isinstance(result, Exception):
                self._set_status(f"could not load tasks: {result}", error=True)
                self._timeline.set_tasks([])
                return
            names = [t.name for t in result] if isinstance(result, list) else []
            self._timeline.set_tasks(names)
            if names and not self._seeded:
                self._timeline.seed_default()
                self._seeded = True
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
        )
        self._saving = True
        self._buttons.setEnabled(False)
        self._set_status("saving…")
        self.hub.run_async(
            f"seqdlg_save:{self.hostname}:{req.name}",
            lambda: self.hub.fleet.get(self.hostname).create_sequence(req),
        )

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
