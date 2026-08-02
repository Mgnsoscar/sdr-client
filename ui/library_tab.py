"""
LibraryTab — the shared definition library.

One fleet-wide set of scripts, tasks, and sequences (the authoring source the
sequence/plan editors read from, and — later — the set deployed to every unit).
Per-unit differences are parameters and live in plans, not here.

This view shows the library's contents and lets you populate it:
  - Pull from unit… — snapshot a connected unit's scripts/tasks/sequences into the
    library (any one unit is enough; the fleet holds one identical library).
  - Import… / Export… — move the whole library as a JSON file (seed a fresh PC,
    or share it).

Within-library integrity is surfaced here (a sequence step referencing an unknown
task, a task whose script is missing). Editing individual definitions from here
comes with the editor rework; for now this is view + populate.

Operation labels routed in _on_task_done:
    lib_pull:<hostname>
"""
from __future__ import annotations

import json
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from api import models as m
from state import LibraryStore, pull_library
from .qt_adapter import DataHub
from .theme import Palette


class _Column(QWidget):
    """A titled list column (Scripts / Tasks / Sequences)."""

    def __init__(self, title: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._title = QLabel(title)
        self._title.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {Palette.TEXT};")
        lay.addWidget(self._title)
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        lay.addWidget(self._list, stretch=1)
        self._base = title

    def set_items(self, items: List[str]) -> None:
        self._list.clear()
        self._list.addItems(items)
        self._title.setText(f"{self._base}  ({len(items)})")


class LibraryTab(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._store = LibraryStore()
        self._pull_pending = False
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self._refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        row = QHBoxLayout()
        title = QLabel("Library")
        title.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {Palette.TEXT};")
        row.addWidget(title)
        row.addSpacing(12)
        self._pull_btn = QPushButton("Pull from unit…")
        self._pull_btn.setObjectName("primary")
        self._pull_btn.setToolTip("Snapshot a connected unit's scripts, tasks and "
                                  "sequences into the shared library")
        self._pull_btn.clicked.connect(self._on_pull)
        row.addWidget(self._pull_btn)
        self._import_btn = QPushButton("Import…")
        self._import_btn.setToolTip("Load a library from a JSON file (replaces the current one)")
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._import_btn)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setToolTip("Save the whole library to a JSON file")
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)
        row.addStretch(1)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        self._scripts = _Column("Scripts")
        self._tasks = _Column("Tasks")
        self._sequences = _Column("Sequences")
        for c in (self._scripts, self._tasks, self._sequences):
            cols.addWidget(c, stretch=1)
        outer.addLayout(cols, stretch=1)

    # ── Shown / refresh ────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._store.load()
        self._refresh()

    def _refresh(self) -> None:
        self._scripts.set_items([s.name for s in self._store.scripts()])
        self._tasks.set_items([t.name for t in self._store.tasks()])
        self._sequences.set_items([s.name or s.id for s in self._store.sequences()])
        self._set_status()

    def _set_status(self, note: str = "", error: bool = False) -> None:
        n_sc = len(self._store.scripts())
        n_t = len(self._store.tasks())
        n_seq = len(self._store.sequences())
        empty = (n_sc + n_t + n_seq) == 0
        problems = self._store.check_integrity()
        parts = []
        if note:
            parts.append(note)
        if empty:
            parts.append("Library is empty — pull it from a unit or import a file.")
        else:
            parts.append(f"{n_sc} script(s) · {n_t} task(s) · {n_seq} sequence(s)")
        if problems:
            parts.append("⚠ " + "; ".join(problems[:3])
                         + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""))
        color = Palette.CRASH if (error or problems) else Palette.TEXT_FAINT
        self._status.setText("   ·   ".join(parts))
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    # ── Pull from a unit ───────────────────────────────────────────────────────

    def _on_pull(self) -> None:
        if self._pull_pending:
            return
        hosts = self.hub.fleet.hostnames()
        if not hosts:
            QMessageBox.information(self, "No units", "No units are configured to pull from.")
            return
        labels = []
        by_label = {}
        for h in hosts:
            try:
                label = self.hub.fleet.get(h).unit_id
            except KeyError:
                label = h
            labels.append(label)
            by_label[label] = h
        if len(labels) == 1:
            hostname = by_label[labels[0]]
        else:
            label, ok = QInputDialog.getItem(
                self, "Pull from unit", "Snapshot the library from:", labels, 0, False)
            if not ok:
                return
            hostname = by_label[label]

        if not self._store.library().scripts and not self._store.tasks():
            pass  # empty library — no need to warn about replacing
        elif QMessageBox.question(
            self, "Replace library",
            "Pulling replaces the current library with this unit's definitions.\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._pull_pending = True
        self._set_status(f"pulling from {hostname}…")
        client = self.hub.fleet.get(hostname)
        self.hub.run_async(f"lib_pull:{hostname}", lambda: pull_library(client))

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("lib_pull:"):
            return
        self._pull_pending = False
        if isinstance(result, Exception) or not isinstance(result, m.Library):
            self._set_status(f"pull failed: {result}", error=True)
            return
        self._store.replace(result)
        self._refresh()
        self._set_status("pulled from unit")

    # ── Import / export the whole library ──────────────────────────────────────

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export library", "library.json", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._store.library().model_dump(), fh, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        self._set_status(f"exported to {path}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import library", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lib = m.Library(**json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read file:\n{exc}")
            return
        if QMessageBox.question(
            self, "Replace library",
            f"Import replaces the current library with:\n"
            f"{len(lib.scripts)} script(s), {len(lib.tasks)} task(s), "
            f"{len(lib.sequences)} sequence(s).\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.replace(lib)
        self._refresh()
        self._set_status("imported library")
