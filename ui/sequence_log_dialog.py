"""
SequenceLogDialog — live view of a whole sequence run's log.

The agent writes one log per sequence run that interleaves the annotated
choreography (armed / ON AIR / each step / OFF AIR / outcome) with each step's
program output. This dialog tails that log over the sequence log-stream
WebSocket.

It uses its OWN LogTailer (not the DataHub's single shared one), so opening a
sequence log never disturbs a task tail running in the Logs tab. The tailer's
callbacks fire on a background thread; they're marshalled to the GUI thread via
Qt signals before touching the view.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from api import models as m
from .qt_adapter import DataHub
from .theme import Palette
from state.log_tail import LogTailer

MAX_BLOCKS = 5000


class SequenceLogDialog(QDialog):
    _text = pyqtSignal(str)
    _status = pyqtSignal(bool, str)

    def __init__(self, hub: DataHub, hostname: str, sequence: m.Sequence, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.sequence = sequence
        self._tailer = LogTailer()

        self.setWindowTitle(f"Sequence log — {sequence.name or sequence.id}")
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

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel(f"{self.sequence.name or self.sequence.id}  ·  {self.hostname}")
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
        try:
            url = self.hub.fleet.get(self.hostname).sequence_log_stream_url(
                self.sequence.id, lines=500)
        except Exception as exc:  # unknown host, etc.
            self._on_status(False, str(exc))
            return
        self._tailer.start(url)

    def _append(self, chunk: str) -> None:
        # Remember whether the user was at the bottom BEFORE appending. Insert via a
        # detached cursor (not self._view.moveCursor, which forces the viewport to
        # follow the cursor and so scrolls even with autoscroll off). Only scroll
        # when autoscroll is on and the user hadn't scrolled up to read history.
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
            self._status_lbl.setText("● live")
            self._status_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.ONLINE};")
        else:
            self._status_lbl.setText(detail or "disconnected")
            self._status_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
