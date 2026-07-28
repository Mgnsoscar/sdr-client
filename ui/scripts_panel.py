"""
ScriptsPanel — the Scripts sub-tab of the unit detail view.

Lists the .py scripts in the unit's scripts directory, shows a selected script's
contents (read-only), and lets you upload, download, or delete scripts.

All network calls go through the DataHub's run_async (off the GUI thread); their
results arrive on the shared task_done signal, which this panel filters down to
its own host and operations. Reads happen on demand — when the tab is shown, on
Refresh, or after an upload/delete — since scripts change rarely and aren't part
of the poll.

Operation labels (parsed back in _on_task_done):
    scripts_list:<host>
    scripts_get:<host>:<name>
    scripts_download:<host>:<name>
    scripts_delete:<host>:<name>
    scripts_upload:<host>:<name>
"""
from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .qt_adapter import DataHub
from .theme import Palette


class ScriptsPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._selected: Optional[str] = None
        self._pending_download: Optional[str] = None   # local path awaiting a download
        self._build()
        # task_done is shared across the app; we filter to this host + our ops.
        self.hub.task_done.connect(self._on_task_done)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        # Controls row
        row = QHBoxLayout()
        row.setSpacing(8)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)

        self._upload_btn = QPushButton("Upload…")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        row.addWidget(self._upload_btn)

        self._download_btn = QPushButton("Download…")
        self._download_btn.clicked.connect(self._on_download)
        self._download_btn.setEnabled(False)
        row.addWidget(self._download_btn)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        self._delete_btn.setEnabled(False)
        row.addWidget(self._delete_btn)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)
        row.addStretch(1)
        outer.addLayout(row)

        # Master-detail: script list on the left, read-only viewer on the right
        split = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.setMinimumWidth(200)
        self._list.currentItemChanged.connect(self._on_select)
        self._list.setStyleSheet(
            f"QListWidget {{ background: {Palette.SURFACE}; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 8px; padding: 4px; }}"
        )
        split.addWidget(self._list)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._view.setFont(mono)
        self._view.setPlaceholderText("Select a script to view its contents.")
        self._view.setStyleSheet(
            f"QPlainTextEdit {{ background: #1E2530; color: #D6DCE5; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 8px; padding: 8px; }}"
        )
        split.addWidget(self._view)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 520])
        outer.addWidget(split, stretch=1)

    # ── Shown / refresh ──────────────────────────────────────────────────────

    def on_shown(self) -> None:
        """Called by UnitDetail when this sub-tab becomes visible."""
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"scripts_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_scripts(),
        )

    # ── Selection / view ─────────────────────────────────────────────────────

    def _on_select(self, cur: Optional[QListWidgetItem], _prev=None) -> None:
        has = cur is not None
        self._delete_btn.setEnabled(has)
        self._download_btn.setEnabled(has)
        if not has:
            self._selected = None
            return
        name = cur.text()
        self._selected = name
        self._view.setPlainText(f"# loading {name} …")
        self.hub.run_async(
            f"scripts_get:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).get_script(name),
        )

    # ── Upload / download / delete ───────────────────────────────────────────

    def _on_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Upload script", "", "Python scripts (*.py)"
        )
        if not path:
            return
        name = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            self._set_status(f"could not read file: {exc}", error=True)
            return
        self._set_status(f"uploading {name}…")
        self.hub.run_async(
            f"scripts_upload:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).upload_script(name, content),
        )

    def _on_download(self) -> None:
        name = self._selected
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Download script", name, "Python scripts (*.py)"
        )
        if not path:
            return
        self._pending_download = path
        self._set_status(f"downloading {name}…")
        self.hub.run_async(
            f"scripts_download:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).get_script(name),
        )

    def _on_delete(self) -> None:
        name = self._selected
        if not name:
            return
        resp = QMessageBox.question(
            self, "Delete script",
            f"Delete '{name}' from {self.hostname}?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"deleting {name}…")
        self.hub.run_async(
            f"scripts_delete:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).delete_script(name),
        )

    # ── Result routing (shared task_done, filtered to this host) ─────────────

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("scripts_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]
        if isinstance(result, Exception):
            self._pending_download = None
            self._set_status(f"error: {result}", error=True)
            return

        if op == "scripts_list":
            self._populate(result if isinstance(result, list) else [])
        elif op == "scripts_get":
            name = ":".join(parts[2:])
            if name == self._selected:
                self._view.setPlainText(result if isinstance(result, str) else str(result))
        elif op == "scripts_download":
            target = self._pending_download
            self._pending_download = None
            if not target:
                return
            text = result if isinstance(result, str) else str(result)
            try:
                # newline="" preserves the file's own line endings (scripts on the
                # Pi use \n) instead of translating to the local platform's.
                with open(target, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            except OSError as exc:
                self._set_status(f"could not save: {exc}", error=True)
                return
            self._set_status(f"downloaded to {target}")
        elif op == "scripts_delete":
            deleted = result.get("deleted", "") if isinstance(result, dict) else ""
            self._set_status(f"deleted {deleted}")
            self._selected = None
            self._view.clear()
            self._refresh()
        elif op == "scripts_upload":
            saved = result.get("saved", "") if isinstance(result, dict) else ""
            self._set_status(f"uploaded {saved}")
            self._refresh()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _populate(self, names: List[str]) -> None:
        keep = self._selected
        self._list.blockSignals(True)
        self._list.clear()
        for n in names:
            self._list.addItem(n)
        self._list.blockSignals(False)

        if not names:
            self._set_status("no scripts on this unit")
            self._view.clear()
            self._selected = None
            self._delete_btn.setEnabled(False)
            self._download_btn.setEnabled(False)
            return

        self._set_status(f"{len(names)} script(s)")
        # Restore the previous selection if the script still exists (this re-fetches
        # its content, keeping the viewer current after an upload/refresh).
        if keep in names:
            items = self._list.findItems(keep, Qt.MatchFlag.MatchExactly)
            if items:
                self._list.setCurrentItem(items[0])

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")