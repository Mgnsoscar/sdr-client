"""
UnitDetail — the drill-in view for a single unit.

Opened when a UnitCard is clicked. Layout: a header (back button + unit name +
live status) above a row of sub-tabs:

    Tasks  |  Logs  |  Sequences  |  Scripts

This module builds the shell + the Tasks, Logs, and Scripts panels. The
Sequences panel is still a placeholder, filled in a subsequent step.

Data:
  - Task state is fed from the poller's fast snapshot (on_fast_update), so the
    list stays live without the detail view polling on its own.
  - Actions (start/stop) run through the DataHub's run_async so they don't
    block the UI; results refresh the row.
"""
from __future__ import annotations

from typing import Dict, Optional

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from .qt_adapter import DataHub
from .logs_panel import LogsPanel
from .scripts_panel import ScriptsPanel
from .sequences_panel import SequencesPanel
from .task_editor import TaskEditorDialog
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
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        for b in (self._start, self._stop):
            b.setFixedWidth(72)
        for b in (self._edit, self._delete):
            b.setFixedWidth(62)
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(self._on_stop)
        self._edit.clicked.connect(self._on_edit)
        self._delete.clicked.connect(self._on_delete)
        for b in (self._start, self._stop, self._edit, self._delete):
            lay.addWidget(b)

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
        # A running task can't be deleted (the agent refuses); edit is allowed anytime.
        self._delete.setEnabled(not running)
        self._edit.setEnabled(True)

    # ── Actions ────────────────────────────────────────────────────────────────

    def _busy(self, label: str) -> None:
        for b in (self._start, self._stop, self._edit, self._delete):
            b.setEnabled(False)
        self._info.setText(label)

    def set_error(self, msg: str) -> None:
        """Show an action error and re-enable the buttons (the next poll will
        settle the exact enabled-state)."""
        self._info.setText(msg)
        for b in (self._start, self._stop, self._edit, self._delete):
            b.setEnabled(True)

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

    def _on_edit(self) -> None:
        TaskEditorDialog(self.hub, self.hostname,
                         existing_name=self.task_name, parent=self.window()).exec()

    def _on_delete(self) -> None:
        resp = QMessageBox.question(
            self, "Delete task",
            f"Delete task '{self.task_name}' from {self.hostname}?\n"
            f"This removes it from tasks.yaml on the unit.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self._busy("deleting…")
        self.hub.run_async(
            f"task_delete:{self.hostname}:{self.task_name}",
            lambda: self.hub.fleet.get(self.hostname).delete_task(self.task_name),
        )


# ── Tasks panel ──────────────────────────────────────────────────────────────

def _import_task_set(client, tasks):
    """Create each task via the client (skipping name conflicts). Worker thread."""
    out = []
    for t in tasks:
        name = t.get("name", "?")
        try:
            client.create_task(dict(t))
            out.append((name, None))
        except Exception as exc:  # noqa: BLE001 — AgentError etc., reported per task
            out.append((name, str(exc)))
    return out


class _TasksPanel(QWidget):
    """Scrollable list of task rows for one unit."""

    def __init__(self, hostname: str, hub: DataHub):
        super().__init__()
        self.hostname = hostname
        self.hub = hub
        self._rows: Dict[str, _TaskRow] = {}
        self._known: list = []   # [(name, description), ...] of the built rows
        self._export_path: Optional[str] = None
        self.hub.task_done.connect(self._on_io_done)
        # Update a row the instant its start/stop/restart returns, instead of
        # waiting for the next poll tick.
        self.hub.task_done.connect(self._on_task_action_done)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        header = QHBoxLayout()
        header.addStretch(1)
        self._export_btn = QPushButton("Export…")
        self._export_btn.clicked.connect(self._on_export)
        header.addWidget(self._export_btn)
        self._import_btn = QPushButton("Import…")
        self._import_btn.clicked.connect(self._on_import)
        header.addWidget(self._import_btn)
        self._new_btn = QPushButton("New task")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self._on_new_task)
        header.addWidget(self._new_btn)
        lay.addLayout(header)

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

    def _on_new_task(self) -> None:
        TaskEditorDialog(self.hub, self.hostname, parent=self.window()).exec()

    def _on_task_action_done(self, label: str, result) -> None:
        """Reflect a start/stop/restart/delete result on its row immediately,
        rather than waiting for the next poll tick."""
        parts = label.split(":", 2)   # "task_<verb>:<host>:<task>" (task may hold ':')
        if len(parts) != 3 or parts[1] != self.hostname:
            return
        op = parts[0]
        if op not in ("task_start", "task_stop", "task_restart", "task_delete"):
            return
        name = parts[2]
        row = self._rows.get(name)
        if op == "task_delete":
            if isinstance(result, Exception):
                if row is not None:
                    row.set_error(f"delete failed: {result}")
            else:
                self.hub.refresh_now()   # task gone — drop it from the list at once
            return
        if row is None:
            return
        if isinstance(result, m.ProcessStatus):
            row.update_status(result)
        elif isinstance(result, Exception):
            row.set_error(str(result))

    # ── Export / import (deploy a task set across units) ─────────────────────

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export tasks", "tasks.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        self._export_path = path
        self.hub.run_async(
            f"tasksio_export:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml())

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import tasks", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read file:\n{exc}")
            return
        if isinstance(doc, dict):
            tasks = doc.get("tasks") or []
        elif isinstance(doc, list):
            tasks = doc
        else:
            tasks = []
        tasks = [t for t in tasks if isinstance(t, dict) and t.get("name")]
        if not tasks:
            QMessageBox.information(self, "Import", "No tasks found in that file.")
            return
        if QMessageBox.question(
            self, "Import tasks",
            f"Create {len(tasks)} task(s) on {self.hostname}?\n"
            f"Existing tasks with the same name are skipped.",
        ) != QMessageBox.StandardButton.Yes:
            return
        client = self.hub.fleet.get(self.hostname)
        self.hub.run_async(f"tasksio_import:{self.hostname}",
                           lambda: _import_task_set(client, tasks))

    def _on_io_done(self, label: str, result) -> None:
        parts = label.split(":")
        if not label.startswith("tasksio_") or len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]
        if op == "tasksio_export":
            target = self._export_path
            self._export_path = None
            if isinstance(result, Exception) or not target:
                QMessageBox.warning(self, "Export failed", f"{result}")
                return
            try:
                with open(target, "w", encoding="utf-8", newline="") as fh:
                    fh.write(result if isinstance(result, str) else str(result))
            except OSError as exc:
                QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
                return
            QMessageBox.information(self, "Export", f"Tasks written to\n{target}")
        elif op == "tasksio_import":
            if isinstance(result, Exception) or not isinstance(result, list):
                QMessageBox.warning(self, "Import failed", f"{result}")
                return
            ok = [n for n, e in result if e is None]
            bad = [(n, e) for n, e in result if e is not None]
            msg = f"Created {len(ok)} task(s)."
            if bad:
                msg += "\n\nSkipped / failed:\n" + "\n".join(f"• {n}: {e}" for n, e in bad)
            QMessageBox.information(self, "Import complete", msg)


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
        self._scripts_panel: Optional["ScriptsPanel"] = None  # built per-unit in set_unit
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
        self._sequences_panel = SequencesPanel(hostname, self.hub)
        self._scripts_panel = ScriptsPanel(hostname, self.hub)
        self._sub_stack.addWidget(self._tasks_panel)                       # 0 Tasks
        self._sub_stack.addWidget(self._logs_panel)                        # 1 Logs
        self._sub_stack.addWidget(self._sequences_panel)                   # 2 Sequences
        self._sub_stack.addWidget(self._scripts_panel)                     # 3 Scripts
        self._select_subtab(0)

        # Pull fresh data now so the task list / status appear immediately, rather
        # than blank until the next poll tick (up to fast_interval_s away).
        self.hub.refresh_now()

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