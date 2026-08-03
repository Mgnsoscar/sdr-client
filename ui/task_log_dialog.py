"""
TaskLogDialog — live view of a single task's log, in its own window.

Opened from the Logs button on a task row. The agent follows the task's log file
over the task log-stream WebSocket (recent backlog first, then live). A stopped
task still has its last recorded log, so this shows that too, clearly labelled.

Like SequenceLogDialog, it uses its OWN LogTailer (not the DataHub's shared one),
so several log windows can be open at once and none disturbs another. The tailer's
callbacks fire on a background thread; they're marshalled to the GUI thread via Qt
signals before touching the view.
"""
from __future__ import annotations

from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .qt_adapter import DataHub
from .theme import Palette
from state.log_tail import LogTailer

from PyQt6.QtCore import pyqtSignal

MAX_BLOCKS = 5000


class TaskLogDialog(QDialog):
    _text = pyqtSignal(str)
    _status = pyqtSignal(bool, str)

    def __init__(self, hub: DataHub, hostname: str, task_name: str,
                 running: bool = False, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.task_name = task_name
        self._running = running
        self._tailer = LogTailer()

        self.setWindowTitle(f"Task log — {task_name}")
        self.setMinimumSize(760, 460)
        self._build()

        # Marshal background-thread callbacks onto the GUI thread.
        self._text.connect(self._append)
        self._status.connect(self._on_status)
        self._tailer.set_callbacks(
            on_text=lambda chunk: self._text.emit(chunk),
            on_status=lambda ok, detail: self._status.emit(ok, detail),
        )
        self.finished.connect(lambda _=0: self._tailer.stop())
        self._start()

    # ── Construction ─────────────────────────────────────────────────────────

    def _unit_name(self) -> str:
        """The unit's display label (falls back to its id if not resolvable)."""
        try:
            return self.hub.fleet.get(self.hostname).label or self.hostname
        except Exception:  # noqa: BLE001 — unknown host, etc.
            return self.hostname

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel(f"{self.task_name}  ·  {self._unit_name()}")
        title.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        row.addWidget(title)
        self._status_lbl = QLabel("connecting…")
        self._status_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status_lbl)
        row.addStretch(1)
        self._autoscroll = QPushButton("Autoscroll: on")
        self._autoscroll.setCheckable(True)
        self._autoscroll.setChecked(True)
        self._autoscroll.toggled.connect(self._on_autoscroll_toggled)
        row.addWidget(self._autoscroll)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self._view.clear())
        row.addWidget(clear)
        outer.addLayout(row)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(MAX_BLOCKS)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas"); mono.setStyleHint(QFont.StyleHint.Monospace); mono.setPointSize(10)
        self._view.setFont(mono)
        self._view.setStyleSheet(
            f"QPlainTextEdit {{ background: #1E2530; color: #D6DCE5; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 8px; padding: 8px; }}")
        outer.addWidget(self._view, stretch=1)

    # ── Tailing ──────────────────────────────────────────────────────────────

    def _start(self) -> None:
        # A stopped task still has its last recorded log — say so, so it isn't
        # mistaken for live output.
        if not self._running:
            self._append(
                f"— {self.task_name} is not running; showing the last recorded "
                f"log (may be from a previous run) —\n")
        try:
            url = self.hub.fleet.get(self.hostname).log_stream_url(
                self.task_name, lines=500)
        except Exception as exc:  # noqa: BLE001 — unknown host, etc.
            self._on_status(False, str(exc))
            return
        self._tailer.start(url)

    def _append(self, chunk: str) -> None:
        # Insert via a detached cursor (never self._view.moveCursor, which forces
        # the viewport to follow and would scroll even with autoscroll off). Only
        # scroll when autoscroll is on and the user hadn't scrolled up to read back.
        bar = self._view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        if self._autoscroll.isChecked() and at_bottom:
            bar.setValue(bar.maximum())

    def _on_autoscroll_toggled(self, on: bool) -> None:
        self._autoscroll.setText(f"Autoscroll: {'on' if on else 'off'}")
        if on:
            bar = self._view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _on_status(self, connected: bool, detail: str) -> None:
        if connected:
            self._status_lbl.setText("● live" if self._running else "● connected")
            self._status_lbl.setStyleSheet(
                f"font-size: 11px; color: {Palette.ONLINE};")
        else:
            self._status_lbl.setText(detail or "disconnected")
            self._status_lbl.setStyleSheet(
                f"font-size: 11px; color: {Palette.TEXT_FAINT};")
