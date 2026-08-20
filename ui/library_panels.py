"""
LibraryTasksPanel — the Tasks sub-tab of the Library.

Lists the shared library's tasks and lets you create, edit and delete them with
the same TaskEditorDialog the unit card uses — so a task's script parameters are
still auto-generated into a typed form, offline, against the library's scripts.
Unlike a unit's Tasks panel there are no Start/Stop controls: the library is a
set of definitions, not a running unit.

The library client is local (no network), so the task list and import/export run
synchronously; the editor dialog still does its own reads/writes through the hub.
"""
from __future__ import annotations

from typing import List

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from config import UNIT_TYPE_LABELS, DEFAULT_UNIT_TYPE
from state import LibraryError
from .qt_adapter import DataHub
from .scope_selector import scope_chip, confirm_delete
from .task_editor import TaskEditorDialog
from .theme import Palette


class _LibTaskRow(QFrame):
    """One library task: name, description, scope chip, Edit / Delete."""

    def __init__(self, task: m.ProcessStatus, types, on_edit, on_delete):
        super().__init__()
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout()
        box.setSpacing(1)
        name = QLabel(task.name)
        name.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(name)
        if task.description:
            desc = QLabel(task.description)
            desc.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(desc)
        lay.addLayout(box, stretch=1)
        lay.addWidget(scope_chip(types), alignment=Qt.AlignmentFlag.AlignVCenter)

        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        for b in (self._edit, self._delete):
            b.setFixedWidth(66)
        self._edit.clicked.connect(lambda: on_edit(task))
        self._delete.clicked.connect(lambda: on_delete(task))
        lay.addWidget(self._edit, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._delete, alignment=Qt.AlignmentFlag.AlignTop)


class LibraryTasksPanel(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = LIBRARY_HOST
        self._active_type = DEFAULT_UNIT_TYPE   # set by the tab's unit-type selector
        self._build()

    def set_active_type(self, unit_type: str) -> None:
        self._active_type = unit_type
        self._refresh()

    def _client(self):
        return self.hub.fleet.get(LIBRARY_HOST)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        self._new_btn = QPushButton("New task")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self._on_new)
        row.addWidget(self._new_btn)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setToolTip("Save the library's tasks to a YAML file")
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)
        self._import_btn = QPushButton("Import…")
        self._import_btn.setToolTip("Create tasks from a YAML file (existing names skipped)")
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._import_btn)
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
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        try:
            tasks = self._client().list_tasks()
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"error: {exc}", error=True)
            return
        if not tasks:
            empty = QLabel("No tasks in the library yet. Click “New task” to create one.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            self._list.addWidget(empty)
            self._set_status("")
            return
        want = self._active_type
        shown = 0
        for t in tasks:
            types = self._types_for(t.name)
            if not m.applies_to_type(types, want):
                continue
            self._list.addWidget(_LibTaskRow(t, types, self._on_edit, self._on_delete))
            shown += 1
        if shown == 0:
            empty = QLabel(f"No {UNIT_TYPE_LABELS.get(want, want)} tasks yet. "
                           "Click “New task” to add one (shared with all units via "
                           "the scope in the editor).")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        self._set_status(
            f"{shown} task(s) for {UNIT_TYPE_LABELS.get(want, want)} · {len(tasks)} total")

    def _types_for(self, name: str) -> list:
        """A task's unit-type scope, read from the shared library store (list_tasks
        returns a lightweight status without it)."""
        store = self.hub.fleet.library_store()
        if store is None:
            return []
        t = store.get_task(name)
        return list(t.types) if t is not None else []

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        # New tasks default to the active unit type; the editor's scope picker can
        # widen them to Shared (or the other type).
        if TaskEditorDialog(self.hub, LIBRARY_HOST, default_types=[self._active_type],
                            parent=self.window()).exec():
            self._refresh()

    def _on_edit(self, task: m.ProcessStatus) -> None:
        if TaskEditorDialog(self.hub, LIBRARY_HOST, existing_name=task.name,
                            parent=self.window()).exec():
            self._refresh()

    def _on_delete(self, task: m.ProcessStatus) -> None:
        types = self._types_for(task.name)
        action = confirm_delete(self, "task", task.name, types, self._active_type,
                                self._unshare_task)
        if action == "cancel":
            return
        if action == "unshared":
            self._refresh()
            return
        try:
            self._client().delete_task(task.name)
        except LibraryError as exc:
            QMessageBox.warning(self, "Cannot delete task", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Delete failed", str(exc))
            return
        self._refresh()

    def _unshare_task(self, name: str, new_types: list) -> None:
        """Re-scope a shared task to `new_types` (remove it from the active type) via
        a read-modify-write, so 'Remove from this type only' keeps it on the others."""
        store = self.hub.fleet.library_store()
        t = store.get_task(name) if store is not None else None
        spec = t.model_dump() if t is not None else {"name": name}
        spec["types"] = list(new_types)
        self._client().update_task(name, spec)

    # ── Import / export ──────────────────────────────────────────────────────

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export tasks", "tasks.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            text = self._client().get_tasks_yaml()
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        self._set_status(f"exported to {path}")

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
            f"Create {len(tasks)} task(s) in the library?\n"
            f"Existing tasks with the same name are skipped.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        client = self._client()
        existing = {t.name for t in client.list_tasks()}
        ok: List[str] = []
        bad: List[tuple] = []
        for t in tasks:
            name = t.get("name", "?")
            if name in existing:
                bad.append((name, "skipped (name already exists)"))
                continue
            try:
                client.create_task(dict(t))
                existing.add(name)
                ok.append(name)
            except Exception as exc:  # noqa: BLE001
                bad.append((name, str(exc)))
        self._refresh()
        msg = f"Created {len(ok)} task(s)."
        if bad:
            msg += "\n\nSkipped / failed:\n" + "\n".join(f"• {n}: {e}" for n, e in bad)
        QMessageBox.information(self, "Import complete", msg)

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
