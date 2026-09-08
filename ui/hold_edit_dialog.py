"""
HoldEditDialog — edit a HOLDING run's post-hold (window-B) steps before proceeding.

Edit-while-holding (docs/sequence-hold-step.md §6.4): while a run is parked at its Hold, the
operator can retarget the down-ramp / cool-down (e.g. aim the down-ramp at the −50 dBm where the
receiver actually lost lock) and then Proceed with the revised steps. This hosts the same timeline
editor used to author a sequence, loaded with the running sequence; only the steps AFTER the Hold
take effect — window A has already run — which the banner states plainly. OK returns the edited
FULL step list via ``result_steps`` (the caller sends it as ``ProceedRequest.steps``; the agent
re-extracts window B from it and ignores the already-fired window A).
"""
from __future__ import annotations

from typing import Dict, List, Optional

import yaml

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)

from api import models as m
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_editor import TimelineEditor, task_signals_from_yaml


class HoldEditDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, sequence: m.Sequence, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self._sequence = sequence
        self.result_steps: Optional[List[m.SequenceStep]] = None

        self.setWindowTitle("Edit post-hold steps")
        self.setMinimumSize(780, 480)
        self._build()
        self._timeline.set_steps(sequence.steps)
        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    def _build(self) -> None:
        from .dialog_style import editor_qss
        self.setStyleSheet(editor_qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        note = QLabel(
            "Edit the steps AFTER the Hold (the down-ramp and cool-down). Window A has already "
            "run and is left as-is — only the post-hold steps you change here take effect when "
            "you Proceed.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size: 11px; color: {Palette.ACCENT_INK}; background: {Palette.ACCENT_SOFT}; "
            f"border: 1px solid #cfe0ee; border-radius: 8px; padding: 7px 10px;")
        outer.addWidget(note)

        self._timeline = TimelineEditor()
        self._timeline.set_context(self.hub, self.hostname)
        # The run's own unit governs absolute power (calibration bounds).
        self._timeline.set_calibration(self.hub, self.hostname)
        outer.addWidget(self._timeline, stretch=1)

        self._status = QLabel("loading tasks…")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        apply_btn = self._buttons.addButton("Use these steps", QDialogButtonBox.ButtonRole.AcceptRole)
        apply_btn.setObjectName("primary")
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    # ── Loading (mirrors SequenceEditorDialog) ───────────────────────────────

    def _load(self) -> None:
        self.hub.run_async(
            f"holdedit_tasks:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_tasks())
        self.hub.run_async(
            f"holdedit_yaml:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml())

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("holdedit_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]
        if op == "holdedit_yaml":
            self._timeline.set_task_commands(self._parse_task_commands(result))
            self._timeline.set_task_signals(task_signals_from_yaml(result))
        elif op == "holdedit_tasks":
            names = [t.name for t in result] if isinstance(result, list) else []
            self._timeline.set_tasks(names)
            self._set_status("ready — edit the post-hold steps, then Use these steps")

    @staticmethod
    def _parse_task_commands(result) -> Dict[str, List[str]]:
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

    # ── Accept ────────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        err = self._timeline.validate()
        if err:
            self._set_status(err, error=True)
            return
        # The Hold must survive the edit — otherwise this isn't a window-B revision.
        if not self._timeline.has_hold():
            self._set_status("the Hold was removed — keep it to edit the post-hold window",
                             error=True)
            return
        self.result_steps = self._timeline.steps()
        self.accept()

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    def _disconnect(self) -> None:
        try:
            self.hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
