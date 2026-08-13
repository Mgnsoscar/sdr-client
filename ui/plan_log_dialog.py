"""
PlanLogDialog — live view of every sequence's log in a plan, one tab per unit.

A plan arms sequences across several units at one shared on-air time, so its log
is naturally several logs. This dialog opens a tab per plan item, each tailing
that unit's sequence log over its own WebSocket — the same stream the per-unit
Sequences tab shows, gathered in one window so a plan's coordinated run can be
watched across units at once.

Each pane owns its OWN LogTailer (like SequenceLogDialog / TaskLogDialog), so
opening a plan log never disturbs another log window. Tailer callbacks fire on a
background thread and are marshalled to the GUI thread via Qt signals.
"""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from api import models as m
from .qt_adapter import DataHub
from .theme import Palette
from state.log_tail import LogTailer

MAX_BLOCKS = 5000


class _UnitSeqLogPane(QWidget):
    """One unit's sequence-log view: status + autoscroll/clear + a tailing view."""

    _text = pyqtSignal(str)
    _status = pyqtSignal(bool, str)

    def __init__(self, hub: DataHub, hostname: str, item: m.PlanItem, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.item = item
        self._tailer = LogTailer()
        self._build()
        self._text.connect(self._append)
        self._status.connect(self._on_status)
        self._tailer.set_callbacks(
            on_text=lambda chunk: self._text.emit(chunk),
            on_status=lambda ok, detail: self._status.emit(ok, detail),
        )

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        row = QHBoxLayout(); row.setSpacing(8)
        title = QLabel(f"{self.item.sequence_name or self.item.sequence_id}  ·  {self._unit_name()}")
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

    def _unit_name(self) -> str:
        try:
            return self.hub.fleet.get(self.hostname).label or self.hostname
        except Exception:  # noqa: BLE001 — unknown host, etc.
            return self.item.unit_label or self.hostname

    # ── Tailing ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        try:
            url = self.hub.fleet.get(self.hostname).sequence_log_stream_url(
                self.item.sequence_id, lines=500)
        except Exception as exc:  # noqa: BLE001 — unknown host, etc.
            self._on_status(False, str(exc))
            return
        self._tailer.start(url)

    def stop(self) -> None:
        self._tailer.stop()

    def _append(self, chunk: str) -> None:
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


class PlanLogDialog(QDialog):
    """Live logs for every sequence in a plan, one tab per unit."""

    def __init__(self, hub: DataHub, plan: m.Plan, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.plan = plan
        self._panes: List[_UnitSeqLogPane] = []

        self.setWindowTitle(f"Plan log — {plan.name or plan.id}")
        self.setMinimumSize(820, 520)
        self._build()

        self.finished.connect(lambda _=0: [p.stop() for p in self._panes])
        for p in self._panes:
            p.start()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        head = QLabel(f"{self.plan.name or self.plan.id}  ·  {len(self.plan.items)} sequence(s)")
        head.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(head)

        if not self.plan.items:
            empty = QLabel("This plan has no sequences to log.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            outer.addWidget(empty)
            return

        tabs = QTabWidget()
        for item in self.plan.items:
            pane = _UnitSeqLogPane(self.hub, item.hostname, item)
            self._panes.append(pane)
            tabs.addTab(pane, pane._unit_name())
        outer.addWidget(tabs, stretch=1)
