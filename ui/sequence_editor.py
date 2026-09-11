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
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from .qt_adapter import DataHub
from .scope_selector import ScopeSelector
from .theme import Palette
from .timeline_editor import TimelineEditor, task_signals_from_yaml
from .timeline_model import step_anchor_supported


class SequenceEditorDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str,
                 sequence: Optional[m.Sequence] = None, default_types=None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self._sequence = sequence            # None -> create, else edit
        self._editing = sequence is not None
        self._default_types = list(default_types) if default_types else None
        self._saving = False

        self.setWindowTitle("Edit sequence" if self._editing else "New sequence")
        self.setMinimumSize(780, 460)
        self._build()
        if self._editing:
            self._name.setText(sequence.name)
            self._desc.setPlainText(sequence.description)
            self._timeline.set_steps(sequence.steps)
            if self._scope is not None:
                self._scope.set_from_types(getattr(sequence, "types", []) or [])
        elif self._scope is not None and self._default_types is not None:
            # New sequence opened from a unit-type view — default its scope to that type.
            self._scope.set_from_types(self._default_types)
        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        from .dialog_style import editor_qss
        self.setStyleSheet(editor_qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        # ── Header: SEQUENCE NAME + description · scope · actions · validity pill ──
        header = QHBoxLayout(); header.setSpacing(14)
        left = QVBoxLayout(); left.setSpacing(3)
        cap = QLabel("SEQUENCE NAME")
        cap.setStyleSheet(f"font-size:10px; font-weight:700; letter-spacing:0.7px; "
                          f"color:{Palette.TEXT_FAINT};")
        left.addWidget(cap)
        self._name = QLineEdit()
        self._name.setPlaceholderText("unique sequence name")
        self._name.setStyleSheet(
            f"QLineEdit {{ font-size:18px; font-weight:600; color:{Palette.TEXT}; "
            f"border:1px solid transparent; border-radius:8px; padding:5px 8px; background:transparent; }}"
            f"QLineEdit:hover {{ background:{Palette.SURFACE_ALT}; }}"
            f"QLineEdit:focus {{ background:#FFFFFF; border-color:{Palette.ACCENT}; }}")
        self._name.textChanged.connect(lambda _=0: self._revalidate())
        left.addWidget(self._name)
        from .desc_widget import description_editor
        self._desc = description_editor()
        try:
            self._desc.setMaximumHeight(30)
        except Exception:  # noqa: BLE001
            pass
        left.addWidget(self._desc)
        header.addLayout(left, stretch=1)

        right = QVBoxLayout(); right.setSpacing(9)
        self._ready_pill = QLabel("checking…")
        self._ready_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self._ready_pill, alignment=Qt.AlignmentFlag.AlignRight)
        actions = QHBoxLayout(); actions.setSpacing(8)
        # Library-only scope selector; on a live unit, a static unit chip instead.
        self._scope: Optional[ScopeSelector] = None
        if self.hostname == LIBRARY_HOST:
            self._scope = ScopeSelector()
            actions.addWidget(self._scope)
        else:
            chip = QLabel(f"🛰  {self.hostname}")
            chip.setStyleSheet(
                f"background:{Palette.INSET}; border:1px solid {Palette.BORDER}; border-radius:999px; "
                f"padding:5px 11px; color:{Palette.TEXT_MUTED}; font-size:12px;")
            actions.addWidget(chip)
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(
            f"QPushButton {{ background:#FFFFFF; border:1px solid {Palette.BORDER_STRONG}; "
            f"border-radius:8px; padding:7px 16px; font-weight:600; color:{Palette.TEXT}; }}"
            f"QPushButton:hover {{ background:{Palette.SURFACE_ALT}; }}")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save sequence")
        save.setStyleSheet(
            f"QPushButton {{ background:{Palette.ACCENT}; border:none; border-radius:8px; "
            f"padding:7px 18px; font-weight:700; color:#FFFFFF; }}"
            f"QPushButton:hover {{ background:#25597E; }}"
            f"QPushButton:disabled {{ background:{Palette.BORDER_STRONG}; color:#FFFFFF; }}")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._on_save)
        actions.addWidget(cancel); actions.addWidget(save)
        self._buttons = QWidget(); self._buttons.setLayout(actions)   # enable/disable as a group
        right.addWidget(self._buttons, alignment=Qt.AlignmentFlag.AlignRight)
        header.addLayout(right)
        outer.addLayout(header)
        self._set_ready("checking", "checking…")

        self._timeline = TimelineEditor()
        self._timeline.changed.connect(self._revalidate)
        self._timeline.set_context(self.hub, self.hostname)
        # This sequence runs on this unit, so absolute power is bounded by its
        # calibration (relative otherwise).
        self._timeline.set_calibration(self.hub, self.hostname)
        outer.addWidget(self._timeline, stretch=1)

        self._status = QLabel("loading tasks…")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

    def _set_ready(self, kind: str, text: str) -> None:
        """Style the validity pill: ready (green) / warn (amber) / err (red) / checking (grey)."""
        colors = {
            "ready": (Palette.ONLINE, Palette.ONLINE_SOFT),
            "warn": (Palette.ARMED, Palette.ARMED_SOFT),
            "err": (Palette.CRASH, Palette.CRASH_SOFT),
            "checking": (Palette.IDLE, Palette.IDLE_SOFT),
        }
        fg, bg = colors.get(kind, colors["checking"])
        self._ready_pill.setText(text)
        self._ready_pill.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:999px; padding:4px 12px; "
            f"font-size:11.5px; font-weight:600;")

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
            self._timeline.set_task_signals(task_signals_from_yaml(result))
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
            self._set_ready("warn", "Needs a fix")
        else:
            self._set_status("ready to save")
            self._set_ready("ready", "Ready")

    def _current_error(self) -> str | None:
        if not self._name.text().strip():
            return "sequence name is required"
        return self._timeline.validate()

    def _step_anchor_block(self) -> Optional[str]:
        """A safety gate (like the Hold/calibration gates): block saving a step-anchored
        sequence to a UNIT whose agent can't resolve anchor="step" (< 1.24.0) — it would be
        rejected or mis-fire. The library holds only a definition, so it's never blocked; a
        unit we can't resolve/check is left to the agent's own validate() backstop."""
        steps = self._timeline.steps()
        if not any(getattr(s, "anchor", "") == "step" for s in steps):
            return None
        if self.hostname == LIBRARY_HOST:
            return None
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001 — undiscovered unit → let the agent be the backstop
            return None
        if step_anchor_supported(client):
            return None
        return ("this sequence anchors a step to another step, which needs a newer agent "
                "(≥ 1.24.0). Update the unit’s agent, or re-anchor those steps to "
                "on-air / off-air.")

    def _on_save(self) -> None:
        if self._saving:
            return
        err = self._current_error() or self._step_anchor_block()
        if err:
            self._set_status(err, error=True)
            return
        req = m.CreateSequenceRequest(
            name=self._name.text().strip(),
            description=self._desc.toPlainText().strip(),
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
