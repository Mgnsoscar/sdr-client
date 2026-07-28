"""
LogsPanel — the Logs sub-tab of the unit detail view.

Pick a task, see its recent log backlog, then watch it stream live. Uses the
DataHub's LogTailer (one active tail at a time) via log_text / log_status signals.

Behaviour:
  - Choosing a task (or clicking Tail) opens the WebSocket tail for that task.
  - Text appends live; autoscroll keeps the newest visible unless the user has
    scrolled up to read history (then autoscroll pauses until they return to
    the bottom).
  - Switching task, clicking Stop, or leaving the unit detail closes the tail.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from api import models as m
from .qt_adapter import DataHub
from .theme import Palette


class LogsPanel(QWidget):
    MAX_BLOCKS = 5000   # cap the log view so it can't grow unbounded

    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._current_task: Optional[str] = None
        self._tailing = False
        self._task_states: dict = {}       # task_name -> ProcessState (from fast poll)
        self._connected = False            # is the tail WebSocket currently up?
        self._connect_detail = ""
        self._build()

        # Subscribe to the hub's log signals. These are shared (one tailer), so the
        # panel only appends when it's the one that started the active tail.
        self.hub.log_text.connect(self._on_log_text)
        self.hub.log_status.connect(self._on_log_status)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        # Controls row
        row = QHBoxLayout()
        row.setSpacing(8)

        row.addWidget(QLabel("Task:"))
        self._task_combo = QComboBox()
        self._task_combo.setMinimumWidth(180)
        self._task_combo.currentTextChanged.connect(self._on_task_changed)
        row.addWidget(self._task_combo)

        self._tail_btn = QPushButton("Tail")
        self._tail_btn.setObjectName("primary")
        self._tail_btn.clicked.connect(self._toggle_tail)
        row.addWidget(self._tail_btn)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)

        row.addStretch(1)

        self._autoscroll = QPushButton("Autoscroll: on")
        self._autoscroll.setCheckable(True)
        self._autoscroll.setChecked(True)
        self._autoscroll.toggled.connect(self._on_autoscroll_toggled)
        row.addWidget(self._autoscroll)

        self._clear = QPushButton("Clear")
        self._clear.clicked.connect(self._clear_view)
        row.addWidget(self._clear)

        outer.addLayout(row)

        # Log view — monospace, read-only
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(self.MAX_BLOCKS)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._view.setFont(mono)
        self._view.setStyleSheet(
            f"QPlainTextEdit {{ background: #1E2530; color: #D6DCE5; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 8px; padding: 8px; }}"
        )
        outer.addWidget(self._view, stretch=1)

    # ── Population ─────────────────────────────────────────────────────────────

    def set_tasks(self, tasks: List[m.ProcessStatus]) -> None:
        """Populate the task selector and remember each task's state, so the panel
        can show whether tailed output is live or just a stopped task's last
        recorded log. Called on every fast poll update."""
        self._task_states = {t.name: t.state for t in tasks}
        names = [t.name for t in tasks]
        current = self._task_combo.currentText()
        # Only rebuild the combo if the set changed, to avoid disrupting selection.
        existing = [self._task_combo.itemText(i) for i in range(self._task_combo.count())]
        if names != existing:
            self._task_combo.blockSignals(True)
            self._task_combo.clear()
            self._task_combo.addItems(names)
            if current in names:
                self._task_combo.setCurrentText(current)
            self._task_combo.blockSignals(False)
        # A task may have started/stopped while tailing — refresh the indicator.
        if self._tailing:
            self._refresh_status()

    # ── Tail control ───────────────────────────────────────────────────────────

    def _on_task_changed(self, task_name: str) -> None:
        # If we were tailing, switch the tail to the newly selected task.
        if self._tailing and task_name:
            self._start_tail(task_name)

    def _toggle_tail(self) -> None:
        if self._tailing:
            self._stop_tail()
        else:
            task = self._task_combo.currentText()
            if task:
                self._start_tail(task)

    def _start_tail(self, task_name: str) -> None:
        self._current_task = task_name
        self._tailing = True
        self._tail_btn.setText("Stop")
        self._view.clear()
        if self._task_states.get(task_name) == m.ProcessState.RUNNING:
            self._append(f"— tailing {task_name} on {self.hostname} (running) —\n")
        else:
            self._append(
                f"— {task_name} on {self.hostname} is not running; showing the "
                f"last recorded log (may be from a previous run) —\n"
            )
        self.hub.start_log_tail(self.hostname, task_name, lines=200)

    def _stop_tail(self) -> None:
        self._tailing = False
        self._connected = False
        self._tail_btn.setText("Tail")
        self.hub.stop_log_tail()
        self._status.setText("stopped")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

    def on_leave(self) -> None:
        """Called when the user navigates away from this unit — close the tail."""
        if self._tailing:
            self._stop_tail()

    # ── Signal handlers ────────────────────────────────────────────────────────

    def _on_log_text(self, chunk: str) -> None:
        if not self._tailing:
            return
        self._append(chunk)

    def _on_log_status(self, connected: bool, detail: str) -> None:
        if not self._tailing:
            return
        self._connected = connected
        self._connect_detail = detail
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Reflect both the stream connection and whether the task is running, so a
        stopped task's backlog is never mistaken for live output."""
        if not self._tailing:
            return
        if not self._connected:
            self._status.setText(f"○ {self._connect_detail or 'disconnected'}")
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        elif self._task_states.get(self._current_task) == m.ProcessState.RUNNING:
            self._status.setText("● live")
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.ONLINE};")
        else:
            self._status.setText("○ not running · last recorded log")
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")

    # ── View helpers ───────────────────────────────────────────────────────────

    def _append(self, text: str) -> None:
        # Remember whether the user is at the bottom before appending.
        bar = self._view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4

        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)

        if self._autoscroll.isChecked() and at_bottom:
            bar.setValue(bar.maximum())

    def _on_autoscroll_toggled(self, on: bool) -> None:
        self._autoscroll.setText(f"Autoscroll: {'on' if on else 'off'}")
        if on:
            bar = self._view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _clear_view(self) -> None:
        self._view.clear()