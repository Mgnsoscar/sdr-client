"""
UnitDetail — the drill-in view for a single unit.

Opened when a UnitCard is clicked. Layout: a header (back button + unit name +
live status) above a row of sub-tabs:

    Tasks  |  Logs  |  Sequences  |  Scripts

This module builds the shell + the Tasks panel (the core control surface: each
task with start/stop and live state). The other three panels are
placeholders, filled in subsequent steps.

Data:
  - Task state is fed from the poller's fast snapshot (on_fast_update), so the
    list stays live without the detail view polling on its own.
  - Actions (start/stop) run through the DataHub's run_async so they don't
    block the UI; results refresh the row.
"""
from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStackedWidget,
    QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from .qt_adapter import DataHub
from .logs_panel import LogsPanel
from .theme import Palette
from .widgets import StatusPill


# ── A single task row ────────────────────────────────────────────────────────

class _TaskRow(QFrame):
    """One task: name, state pill, and start/stop buttons."""

    def __init__(self, hostname: str, task: m.ProcessStatus, hub: DataHub):
        super().__init__()
        self.hostname = hostname
        self.task_name = task.name
        self.hub = hub
        self.setObjectName("card")
        self._build(task)

    def _build(self, task: m.ProcessStatus) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        # Name + description
        namebox = QVBoxLayout()
        namebox.setSpacing(1)
        self._name = QLabel(task.name)
        self._name.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        namebox.addWidget(self._name)
        if task.description:
            desc = QLabel(task.description)
            desc.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            namebox.addWidget(desc)
        lay.addLayout(namebox)
        lay.addStretch(1)

        # PID / exit info (small, muted)
        self._info = QLabel("")
        self._info.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        lay.addWidget(self._info)

        # State pill
        self._pill = StatusPill(task.state.value, task.state.value)
        lay.addWidget(self._pill)

        # Buttons
        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        for b in (self._start, self._stop):
            b.setFixedWidth(72)
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(self._on_stop)
        lay.addWidget(self._start)
        lay.addWidget(self._stop)

        self.update_status(task)

    def update_status(self, task: m.ProcessStatus) -> None:
        st = task.state
        self._pill.set_status(st.value, st.value)

        # Info line
        if st == m.ProcessState.RUNNING and task.pid:
            self._info.setText(f"pid {task.pid}")
        elif st == m.ProcessState.CRASHED:
            code = task.exit_code if task.exit_code is not None else "?"
            self._info.setText(f"exit {code}")
        elif task.restart_count:
            self._info.setText(f"{task.restart_count} restarts")
        else:
            self._info.setText("")

        # Enable/disable buttons by state
        running = st in (m.ProcessState.RUNNING, m.ProcessState.STARTING)
        self._start.setEnabled(not running)
        self._stop.setEnabled(running or st == m.ProcessState.CRASHED)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _busy(self, label: str) -> None:
        for b in (self._start, self._stop):
            b.setEnabled(False)
        self._info.setText(label)

    def _on_start(self) -> None:
        self._busy("starting…")
        self.hub.run_async(
            f"task_start:{self.hostname}:{self.task_name}",
            lambda: self.hub.fleet.get(self.hostname).start_task(self.task_name),
        )

    def _on_stop(self) -> None:
        self._busy("stopping…")
        self.hub.run_async(
            f"task_stop:{self.hostname}:{self.task_name}",
            lambda: self.hub.fleet.get(self.hostname).stop_task(self.task_name),
        )


# ── Tasks panel ──────────────────────────────────────────────────────────────

class _TasksPanel(QWidget):
    """Scrollable list of task rows for one unit."""

    def __init__(self, hostname: str, hub: DataHub):
        super().__init__()
        self.hostname = hostname
        self.hub = hub
        self._rows: Dict[str, _TaskRow] = {}
        self._known: list[str] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._list.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        lay.addWidget(scroll)

        self._empty = QLabel("No tasks, or unit not yet reached.")
        self._empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
        self._list.addWidget(self._empty)

    def update_tasks(self, tasks: list[m.ProcessStatus]) -> None:
        names = [t.name for t in tasks]
        # Build rows on first sight / when the set changes
        if names != self._known:
            # Clear and rebuild (task sets rarely change, so this is cheap)
            while self._list.count():
                item = self._list.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            self._rows.clear()
            if not tasks:
                self._empty = QLabel("No tasks on this unit.")
                self._empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                self._list.addWidget(self._empty)
            for t in tasks:
                row = _TaskRow(self.hostname, t, self.hub)
                self._rows[t.name] = row
                self._list.addWidget(row)
            self._known = names
        else:
            # Same set — just update each row's state
            for t in tasks:
                row = self._rows.get(t.name)
                if row is not None:
                    row.update_status(t)


# ── Detail view shell ────────────────────────────────────────────────────────

class UnitDetail(QWidget):
    """Header + sub-tabs for one unit. Tasks panel built; others placeholder."""

    SUBTABS = ["Tasks", "Logs", "Sequences", "Scripts"]

    def __init__(self, fleet: Fleet, hub: DataHub, on_back, parent=None):
        super().__init__(parent)
        self.fleet = fleet
        self.hub = hub
        self._on_back = on_back
        self.hostname: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        # Header
        header = QHBoxLayout()
        back = QPushButton("← Units")
        back.setFixedWidth(90)
        back.clicked.connect(self._handle_back)
        header.addWidget(back)

        self._title = QLabel("")
        self._title.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {Palette.TEXT};")
        header.addWidget(self._title)

        self._status = StatusPill("unknown", "unknown")
        header.addWidget(self._status)
        header.addStretch(1)
        outer.addLayout(header)

        # Sub-tab buttons
        subbar = QHBoxLayout()
        subbar.setSpacing(6)
        self._subtab_buttons: list[QPushButton] = []
        for i, name in enumerate(self.SUBTABS):
            b = QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.clicked.connect(lambda _c, idx=i: self._select_subtab(idx))
            subbar.addWidget(b)
            self._subtab_buttons.append(b)
        subbar.addStretch(1)
        outer.addLayout(subbar)

        # Sub-tab content
        self._sub_stack = QStackedWidget()
        self._tasks_panel: Optional[_TasksPanel] = None  # built per-unit in set_unit
        self._logs_panel: Optional["LogsPanel"] = None   # built per-unit in set_unit
        self._placeholders: Dict[str, QWidget] = {}
        outer.addWidget(self._sub_stack, stretch=1)

    def _placeholder(self, text: str) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: 13px; color: {Palette.TEXT_FAINT};")
        l.addWidget(lbl)
        return w

    # ── Populate for a specific unit ────────────────────────────────────────────

    def set_unit(self, hostname: str) -> None:
        # Close any live tail from a previously-viewed unit.
        if self._logs_panel is not None:
            self._logs_panel.on_leave()

        self.hostname = hostname
        client = self.fleet.get(hostname)
        self._title.setText(client.unit_id)

        # Rebuild the sub-stack for this unit
        while self._sub_stack.count():
            w = self._sub_stack.widget(0)
            self._sub_stack.removeWidget(w)
            w.deleteLater()

        self._tasks_panel = _TasksPanel(hostname, self.hub)
        self._logs_panel = LogsPanel(hostname, self.hub)
        self._sub_stack.addWidget(self._tasks_panel)                       # 0 Tasks
        self._sub_stack.addWidget(self._logs_panel)                        # 1 Logs
        self._sub_stack.addWidget(self._placeholder("Sequences & runs — coming next."))  # 2
        self._sub_stack.addWidget(self._placeholder("Scripts & tasks.yaml — coming next."))  # 3
        self._select_subtab(0)

    def _select_subtab(self, idx: int) -> None:
        self._sub_stack.setCurrentIndex(idx)
        for i, b in enumerate(self._subtab_buttons):
            b.setChecked(i == idx)

    def _handle_back(self) -> None:
        # Close any live log tail before leaving the unit.
        if self._logs_panel is not None:
            self._logs_panel.on_leave()
        self._on_back()

    # ── Live updates routed from the Units tab ───────────────────────────────────

    def on_fast_update(self, snap) -> None:
        if self.hostname is None:
            return
        # Task list for this unit
        tasksv = snap.tasks.get(self.hostname)
        if isinstance(tasksv, list) and self._tasks_panel is not None:
            self._tasks_panel.update_tasks(tasksv)
            # Keep the Logs panel's task selector in sync with available tasks.
            if self._logs_panel is not None:
                self._logs_panel.set_tasks(tasksv)
        # Header status from system reachability
        sysv = snap.system.get(self.hostname)
        if isinstance(sysv, m.SystemHealth):
            self._status.set_status("online", "online")
        elif isinstance(sysv, Exception):
            self._status.set_status("offline", "offline")

    def on_stream_status(self, hostname: str, connected: bool) -> None:
        if hostname == self.hostname:
            self._status.set_status(
                "online" if connected else "offline",
                "online" if connected else "offline",
            )