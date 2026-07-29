"""
Alert / activity feed — the persistent strip at the bottom of the window.

Always visible (collapsible). Receives every webhook event from the units and
shows them newest-first. Crashes/aborts are styled red and trigger attention
(the window can flash / play a sound — wired by the main window). Lifecycle
events (started/stopped/modified) are quiet log lines.

This doubles as the operation's audit trail: what transmitted, when, on which
unit.
"""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from api import models as m
from .theme import Palette

# Event types we treat as "needs attention" (red + optional sound/flash).
_ALERT_TYPES = {"crash", "event_aborted", "sequence_aborted"}


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _describe(ev) -> tuple[str, bool]:
    """
    Turn a received event (model or dict) into (line_text, is_alert).
    Keeps the operator's language: 'on air', 'crashed', etc.
    """
    if isinstance(ev, m.CrashEvent):
        return (f"{ev.unit_id} · {ev.task_name} CRASHED "
                f"(exit {ev.exit_code})", True)

    if isinstance(ev, m.EventWebhook):
        verb = {
            "event_started":  "event started",
            "event_stopped":  "event stopped",
            "event_aborted":  "event ABORTED",
            "event_modified": "event changed",
        }.get(ev.type, ev.type)
        extra = f" — {ev.detail}" if ev.detail else ""
        return (f"{ev.unit_id} · {ev.task_name}: {verb}{extra}",
                ev.type in _ALERT_TYPES)

    if isinstance(ev, m.SequenceWebhook):
        verb = {
            "sequence_started":  "started",     # first step fired (warm-up begins)
            "sequence_on_air":   "on air",      # on_air_at (T0) reached — RF live
            "sequence_step":     "step fired",
            "sequence_off_air":  "off air",     # on_air_end (T_end) reached — RF off
            "sequence_stopped":  "complete",    # every step (incl. cool-down) has fired
            "sequence_aborted":  "ABORTED",
            "sequence_modified": "window changed",
        }.get(ev.type, ev.type)
        extra = f" — {ev.detail}" if ev.detail else ""
        return (f"{ev.unit_id} · {ev.sequence_name}: {verb}{extra}",
                ev.type in _ALERT_TYPES)

    if isinstance(ev, m.TaskEvent):
        verb = {"task_started": "task started", "task_stopped": "task stopped",
                "task_restarted": "task restarted"}.get(ev.type, ev.type)
        return (f"{ev.unit_id} · {ev.task_name}: {verb}", False)

    # Unknown / raw dict — show its type and unit if present
    if isinstance(ev, dict):
        return (f"{ev.get('unit_id','?')} · {ev.get('type','event')}",
                ev.get("type", "") in _ALERT_TYPES)

    return (str(ev), False)


class AlertFeed(QWidget):
    """Persistent bottom strip showing the activity/alert stream."""

    # Emitted when an alert-level event arrives, so the main window can react
    # (sound, flash, etc.). Carries the descriptive line.
    alert_raised = pyqtSignal(str)

    MAX_ITEMS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("alertstrip")
        self._collapsed = False
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(6)

        # Header row: title + count + clear + collapse toggle
        header = QHBoxLayout()
        self._title = QLabel("ACTIVITY")
        self._title.setObjectName("alertHeader")
        header.addWidget(self._title)

        self._count = QLabel("")
        self._count.setObjectName("alertHeader")
        header.addWidget(self._count)
        header.addStretch(1)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self.clear)
        header.addWidget(self._clear_btn)

        self._toggle_btn = QPushButton("Hide")
        self._toggle_btn.clicked.connect(self._toggle)
        header.addWidget(self._toggle_btn)

        outer.addLayout(header)

        self._list = QListWidget()
        self._list.setObjectName("alertList")
        self._list.setMaximumHeight(150)
        outer.addWidget(self._list)

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_event(self, ev) -> None:
        """Add one received event to the feed (newest at top)."""
        line, is_alert = _describe(ev)
        text = f"{_now_hms()}  ·  {line}"

        item = QListWidgetItem(text)
        if is_alert:
            item.setForeground(Qt.GlobalColor.red)
        self._list.insertItem(0, item)

        # Trim
        while self._list.count() > self.MAX_ITEMS:
            self._list.takeItem(self._list.count() - 1)

        self._count.setText(f"· {self._list.count()}")

        if is_alert:
            self.alert_raised.emit(line)

    def clear(self) -> None:
        self._list.clear()
        self._count.setText("")

    # ── Collapse ─────────────────────────────────────────────────────────────

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._list.setVisible(not self._collapsed)
        self._toggle_btn.setText("Show" if self._collapsed else "Hide")

    def expand(self) -> None:
        """Force-expand (e.g. when an alert arrives while collapsed)."""
        if self._collapsed:
            self._toggle()