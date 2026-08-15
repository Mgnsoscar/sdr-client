"""
UnitDetail — the drill-in view for a single unit.

Opened when a UnitCard is clicked. Layout: a header (back button + unit name +
live status) above a row of sub-tabs:

    Tasks  |  Sequences

Each task row carries a Logs button that opens the task's log in its own window
(like a sequence's log), so logs aren't confined to a single shared tab.

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
    QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from .agent_update_dialog import AgentUpdateDialog
from .live_tune_dialog import LiveTuneDialog
from .qt_adapter import DataHub
from .run_task_dialog import RunTaskDialog
from .sequences_panel import SequencesPanel
from .task_log_dialog import TaskLogDialog
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

        # Buttons — run controls only. Task definitions are edited in the Library
        # and deployed; the unit card just runs what's deployed.
        self._logs = QPushButton("Log")
        self._logs.setToolTip("Open this task's log in a window")
        self._logs.clicked.connect(self._on_logs)
        self._run = QPushButton("Run…")
        self._run.setToolTip("Start this task once with different parameters "
                             "(doesn't change the deployed definition)")
        self._run.clicked.connect(self._on_run)
        self._tune = QPushButton("Tune…")
        self._tune.setToolTip("Adjust the script's live parameters while it runs")
        self._tune.clicked.connect(self._on_tune)
        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        for b in (self._tune, self._run, self._start, self._stop, self._logs):
            b.setFixedWidth(72)
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(self._on_stop)
        for b in (self._tune, self._run, self._start, self._stop, self._logs):
            lay.addWidget(b)

        self.update_status(task)

    def update_status(self, task: m.ProcessStatus) -> None:
        st = task.state
        self._state = st
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
        # Live tuning only makes sense against a running process.
        self._tune.setEnabled(st == m.ProcessState.RUNNING)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _busy(self, label: str) -> None:
        for b in (self._start, self._stop):
            b.setEnabled(False)
        self._info.setText(label)

    def set_error(self, msg: str) -> None:
        """Show an action error and re-enable the buttons (the next poll will
        settle the exact enabled-state)."""
        self._info.setText(msg)
        for b in (self._start, self._stop):
            b.setEnabled(True)

    def _on_logs(self) -> None:
        running = getattr(self, "_state", None) == m.ProcessState.RUNNING
        dlg = TaskLogDialog(self.hub, self.hostname, self.task_name,
                            running=running, parent=self.window())
        dlg.show()

    def _on_run(self) -> None:
        running = getattr(self, "_state", None) in (
            m.ProcessState.RUNNING, m.ProcessState.STARTING)
        dlg = RunTaskDialog(self.hub, self.hostname, self.task_name,
                            running=running, parent=self.window())
        dlg.exec()

    def _on_tune(self) -> None:
        dlg = LiveTuneDialog(self.hub, self.hostname, self.task_name,
                             parent=self.window())
        dlg.exec()

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
        self._known: list = []   # [(name, description), ...] of the built rows
        # Update a row the instant its start/stop/restart returns, instead of
        # waiting for the next poll tick.
        self.hub.task_done.connect(self._on_task_action_done)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # No authoring controls: task definitions are managed in the Library and
        # deployed to units. The unit card only runs what's deployed here.
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
        # Track (name, description) — not just names — so an edit that changes a
        # task's description (or the task set) triggers a rebuild, while a mere
        # state change just updates the existing rows in place.
        names = [(t.name, t.description) for t in tasks]
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
                self._empty = QLabel("No tasks deployed to this unit. "
                                     "Add them in the Library and deploy.")
                self._empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                self._empty.setWordWrap(True)
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

    def _on_task_action_done(self, label: str, result) -> None:
        """Reflect a start/stop/restart result on its row immediately, rather than
        waiting for the next poll tick."""
        parts = label.split(":", 2)   # "task_<verb>:<host>:<task>" (task may hold ':')
        if len(parts) != 3 or parts[1] != self.hostname:
            return
        op = parts[0]
        if op not in ("task_start", "task_stop", "task_restart"):
            return
        name = parts[2]
        row = self._rows.get(name)
        if row is None:
            return
        if isinstance(result, m.ProcessStatus):
            row.update_status(result)
        elif isinstance(result, Exception):
            row.set_error(str(result))


# ── Detail view shell ────────────────────────────────────────────────────────

class UnitDetail(QWidget):
    """Header + sub-tabs for one unit. Tasks panel built; others placeholder."""

    SUBTABS = ["Tasks", "Sequences"]

    def __init__(self, fleet: Fleet, hub: DataHub, on_back,
                 on_edit=None, on_remove=None, parent=None):
        super().__init__(parent)
        self.fleet = fleet
        self.hub = hub
        self._on_back = on_back
        self._on_edit = on_edit
        self._on_remove = on_remove
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

        self._update_btn = QPushButton("Update agent…")
        self._update_btn.setToolTip("Push the agent version this client ships to the unit")
        self._update_btn.clicked.connect(self._open_update)
        header.addWidget(self._update_btn)

        # Manage this unit's identity/addresses (delegated to the Units tab).
        self._edit_btn = QPushButton("Edit unit…")
        self._edit_btn.setToolTip("Change this unit's name, addresses, or API key")
        self._edit_btn.clicked.connect(
            lambda: self._on_edit(self.hostname) if (self._on_edit and self.hostname) else None)
        header.addWidget(self._edit_btn)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setToolTip("Forget this unit on this PC")
        self._remove_btn.clicked.connect(
            lambda: self._on_remove(self.hostname) if (self._on_remove and self.hostname) else None)
        header.addWidget(self._remove_btn)
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
        self._sequences_panel: Optional["SequencesPanel"] = None  # built per-unit in set_unit
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
        self.hostname = hostname
        client = self.fleet.get(hostname)
        self._title.setText(client.label)

        # Rebuild the sub-stack for this unit
        while self._sub_stack.count():
            w = self._sub_stack.widget(0)
            self._sub_stack.removeWidget(w)
            w.deleteLater()

        self._tasks_panel = _TasksPanel(hostname, self.hub)
        # Run-only: the unit card runs deployed tasks/sequences; it never edits
        # definitions (that's the Library's job). Task logs open in their own
        # window from each task row (see _TaskRow.Logs).
        self._sequences_panel = SequencesPanel(hostname, self.hub,
                                               can_edit=False, can_run=True)
        self._sub_stack.addWidget(self._tasks_panel)                       # 0 Tasks
        self._sub_stack.addWidget(self._sequences_panel)                   # 1 Sequences
        self._select_subtab(0)

        # Pull fresh data now so the task list / status appear immediately, rather
        # than blank until the next poll tick (up to fast_interval_s away). Scope
        # it to this unit so opening a card doesn't stall on connect-timeouts to
        # other, unreachable units.
        self.hub.refresh_now(hostname)

    def _select_subtab(self, idx: int) -> None:
        self._sub_stack.setCurrentIndex(idx)
        for i, b in enumerate(self._subtab_buttons):
            b.setChecked(i == idx)
        # Let a panel refresh itself when it becomes visible (e.g. Scripts fetches
        # its list on first show rather than being polled).
        w = self._sub_stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()

    def _handle_back(self) -> None:
        self._on_back()

    def _open_update(self) -> None:
        if not self.hostname:
            return
        AgentUpdateDialog(self.hub, self.hostname, parent=self.window()).exec()

    # ── Live updates routed from the Units tab ───────────────────────────────────

    def on_fast_update(self, snap) -> None:
        if self.hostname is None:
            return
        # Task list for this unit
        tasksv = snap.tasks.get(self.hostname)
        if isinstance(tasksv, list) and self._tasks_panel is not None:
            self._tasks_panel.update_tasks(tasksv)
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