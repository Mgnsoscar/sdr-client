"""
LibraryTab — the shared definition library.

One fleet-wide set of scripts, tasks, and sequences: the authoring source the
sequence/plan editors read from, and — later — the set deployed to every unit.
Per-unit differences are parameters and live in plans, not here.

The tab has a library-wide header (populate/move the whole library) over the same
editing panels the unit card uses, repointed at the offline library instead of a
live unit (fleet.get("__library__") → LibraryClient):

    header:  Pull from unit… · Import… · Export…   +   integrity status
    sub-tabs:  Tasks  |  Sequences  |  Scripts

Because those panels talk to the LibraryClient, all authoring — including a
script's auto-generated parameter form — works with no unit connected. The Tasks
and Sequences panels run in "library mode": definition editing only, no
Start/Stop/run-state (a library isn't a running unit).

Operation labels routed in _on_task_done:
    lib_pull:<hostname>
"""
from __future__ import annotations

import json
from typing import List

from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from state import LibraryStore, pull_library
from .library_panels import LibraryTasksPanel
from .plans_tab import PlansTab
from .qt_adapter import DataHub
from .scripts_panel import ScriptsPanel
from .sequences_panel import SequencesPanel
from .theme import Palette


class LibraryTab(QWidget):
    SUBTABS = ["Tasks", "Sequences", "Scripts", "Plans"]

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        # Share the one store the fleet's LibraryClient wraps, so this header (its
        # counts / integrity) and the editing panels (via the client) read and
        # write the same library. Falls back to a private store only if the fleet
        # has none registered (e.g. an isolated test harness).
        self._store = self.hub.fleet.library_store() or LibraryStore()
        self._pull_pending = False
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self._set_status()

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

        # Sub-tab bar
        subbar = QHBoxLayout()
        subbar.setSpacing(6)
        self._subtab_buttons: List[QPushButton] = []
        for i, name in enumerate(self.SUBTABS):
            b = QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.clicked.connect(lambda _c, idx=i: self._select_subtab(idx))
            subbar.addWidget(b)
            self._subtab_buttons.append(b)
        subbar.addStretch(1)
        outer.addLayout(subbar)

        # Editing panels, repointed at the library (LIBRARY_HOST), definition-only.
        self._stack = QStackedWidget()
        self._tasks_panel = LibraryTasksPanel(self.hub)
        self._sequences_panel = SequencesPanel(LIBRARY_HOST, self.hub, library_mode=True)
        self._scripts_panel = ScriptsPanel(LIBRARY_HOST, self.hub)
        self._plans_panel = PlansTab(self.hub.fleet, self.hub)
        self._stack.addWidget(self._tasks_panel)        # 0 Tasks
        self._stack.addWidget(self._sequences_panel)    # 1 Sequences
        self._stack.addWidget(self._scripts_panel)      # 2 Scripts
        self._stack.addWidget(self._plans_panel)        # 3 Plans
        outer.addWidget(self._stack, stretch=1)
        self._select_subtab(0)

    def _select_subtab(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        for i, b in enumerate(self._subtab_buttons):
            b.setChecked(i == idx)
        w = self._stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()
        # Header counts / integrity may have changed from edits in the panel we're
        # leaving — reload the store and re-derive them.
        self._store.load()
        self._set_status()

    # ── Shown / refresh ────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._store.load()
        self._set_status()
        w = self._stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()

    def _refresh_panels(self) -> None:
        """Reload every editing panel — used after a whole-library replace (pull /
        import) so all three sub-tabs reflect the new contents, not just the visible
        one."""
        for w in (self._tasks_panel, self._sequences_panel, self._scripts_panel):
            if hasattr(w, "on_shown"):
                w.on_shown()

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
            parts.append("Library is empty — pull it from a unit, import a file, "
                         "or add definitions in the tabs below.")
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
        self._refresh_panels()
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
        self._refresh_panels()
        self._set_status("imported library")
