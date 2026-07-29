"""
SequencesPanel — the Sequences sub-tab of the unit detail view.

Lists the sequences stored on this unit (GET /sequences), each shown with a short
timeline summary, and lets you create a new one (opens SequenceEditorDialog) or
delete an existing one. Editing an existing sequence and arming runs come later;
this first pass is create / list / delete.

All network calls go through the DataHub's run_async (off the GUI thread); their
results arrive on the shared task_done signal, filtered here to this host + ops.

Operation labels (parsed back in _on_task_done):
    seq_list:<host>
    seq_delete:<host>:<id>
"""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import models as m
from .qt_adapter import DataHub
from .sequence_editor import SequenceEditorDialog
from .theme import Palette


def _fmt_offset(offset_s: float) -> str:
    n = int(offset_s) if float(offset_s).is_integer() else round(offset_s, 1)
    return f"+{n}s" if n > 0 else (f"{n}s" if n < 0 else "0s")


def summarize(seq: m.Sequence) -> str:
    """A one-line 'on-air: … · off-air: …' digest of a sequence's steps."""
    def glyph(action) -> str:
        a = action.value if hasattr(action, "value") else str(action)
        return "▶" if a == "start" else "⏹"

    on = sorted((s for s in seq.steps if s.anchor == "start"), key=lambda s: s.offset_s)
    off = sorted((s for s in seq.steps if s.anchor == "stop"), key=lambda s: s.offset_s)
    on_txt = ", ".join(f"{glyph(s.action)} {s.task_name} {_fmt_offset(s.offset_s)}" for s in on)
    off_txt = ", ".join(f"{glyph(s.action)} {s.task_name} {_fmt_offset(s.offset_s)}" for s in off)
    parts = []
    if on_txt:
        parts.append(f"on-air: {on_txt}")
    if off_txt:
        parts.append(f"off-air: {off_txt}")
    return "  ·  ".join(parts) if parts else "no steps"


class _SequenceRow(QFrame):
    """One sequence: name, summary, and a Delete button."""

    def __init__(self, seq: m.Sequence, on_delete):
        super().__init__()
        self.seq = seq
        self.setObjectName("card")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout()
        box.setSpacing(2)
        header = QLabel(seq.name or seq.id)
        header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(header)
        if seq.description:
            desc = QLabel(seq.description)
            desc.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(desc)
        summary = QLabel(f"{len(seq.steps)} step(s)  ·  {summarize(seq)}")
        summary.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        summary.setWordWrap(True)
        box.addWidget(summary)
        lay.addLayout(box, stretch=1)

        self._delete = QPushButton("Delete")
        self._delete.setFixedWidth(70)
        self._delete.clicked.connect(lambda: on_delete(seq))
        lay.addWidget(self._delete, alignment=Qt.AlignmentFlag.AlignTop)


class SequencesPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._build()
        self.hub.task_done.connect(self._on_task_done)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        self._new_btn = QPushButton("New sequence")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self._on_new)
        row.addWidget(self._new_btn)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)
        row.addStretch(1)
        outer.addLayout(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._list.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        outer.addWidget(scroll, stretch=1)

    # ── Shown / refresh ──────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"seq_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequences(),
        )

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        dlg = SequenceEditorDialog(self.hub, self.hostname, parent=self.window())
        if dlg.exec():
            self._refresh()

    def _on_delete(self, seq: m.Sequence) -> None:
        resp = QMessageBox.question(
            self, "Delete sequence",
            f"Delete sequence '{seq.name or seq.id}' from {self.hostname}?\n"
            f"This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"deleting {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_delete:{self.hostname}:{seq.id}",
            lambda: self.hub.fleet.get(self.hostname).delete_sequence(seq.id),
        )

    # ── Result routing ───────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("seq_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "seq_list":
            if isinstance(result, Exception):
                self._set_status(f"error: {result}", error=True)
                self._populate([])
                return
            self._populate(result if isinstance(result, list) else [])
        elif op == "seq_delete":
            if isinstance(result, Exception):
                self._set_status(f"delete failed: {result}", error=True)
                return
            self._refresh()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _populate(self, sequences: List[m.Sequence]) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not sequences:
            empty = QLabel("No sequences on this unit yet. Click “New sequence” to create one.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            self._list.addWidget(empty)
            self._set_status("")
            return

        for seq in sequences:
            self._list.addWidget(_SequenceRow(seq, self._on_delete))
        self._set_status(f"{len(sequences)} sequence(s)")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
