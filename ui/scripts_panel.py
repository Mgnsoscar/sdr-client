"""
ScriptsPanel — the Scripts sub-tab of the unit detail view.

Lists the .py scripts in the unit's scripts directory, shows a selected script in
an EDITABLE viewer (edit + Save writes it back via upload, which overwrites), and
lets you upload one or many scripts, download one, download all, or delete.

All network calls go through the DataHub's run_async (off the GUI thread); their
results arrive on the shared task_done signal, filtered here to this host + ops.

Operation labels (parsed back in _on_task_done):
    scripts_list:<host>
    scripts_get:<host>:<name>
    scripts_save:<host>:<name>
    scripts_download:<host>:<name>
    scripts_delete:<host>:<name>
    scripts_upload:<host>            (one or more files, batched)
    scripts_download_all:<host>
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontMetricsF
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from api.fleet import LIBRARY_HOST
from .qt_adapter import DataHub
from .scope_selector import ScopeSelector
from .theme import Palette

Result = Tuple[str, Optional[str]]   # (name, error-or-None)


def _upload_many(client, files: List[Tuple[str, bytes]]) -> List[Result]:
    """Upload each (name, content); runs on a worker thread."""
    out: List[Result] = []
    for name, content in files:
        try:
            client.upload_script(name, content)
            out.append((name, None))
        except Exception as exc:  # noqa: BLE001 — reported per file
            out.append((name, str(exc)))
    return out


def _download_many(client, dest_dir: str) -> List[Result]:
    """Download every script into dest_dir; runs on a worker thread."""
    try:
        names = client.list_scripts()
    except Exception as exc:  # noqa: BLE001
        return [("(listing)", str(exc))]
    out: List[Result] = []
    for name in names:
        try:
            content = client.get_script(name)
            with open(os.path.join(dest_dir, name), "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            out.append((name, None))
        except Exception as exc:  # noqa: BLE001
            out.append((name, str(exc)))
    return out


class ScriptsPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._selected: Optional[str] = None
        self._pending_download: Optional[str] = None
        self._dirty = False           # unsaved edits in the viewer?
        self._loading = False         # suppress dirty while setting text programmatically
        self._clean_text = ""         # last saved/loaded content (dirty = differs from this)
        self._pending_save_text = ""  # content sent to the last save, applied on success
        self._build()
        self.hub.task_done.connect(self._on_task_done)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)

        self._upload_btn = QPushButton("Upload…")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        row.addWidget(self._upload_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setEnabled(False)
        row.addWidget(self._save_btn)

        self._download_btn = QPushButton("Download…")
        self._download_btn.clicked.connect(self._on_download)
        self._download_btn.setEnabled(False)
        row.addWidget(self._download_btn)

        self._download_all_btn = QPushButton("Download all…")
        self._download_all_btn.clicked.connect(self._on_download_all)
        row.addWidget(self._download_all_btn)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        self._delete_btn.setEnabled(False)
        row.addWidget(self._delete_btn)

        # Library-only: the selected script's unit-type scope, applied immediately.
        self._scope: Optional[ScopeSelector] = None
        if self.hostname == LIBRARY_HOST:
            row.addSpacing(8)
            row.addWidget(QLabel("Applies to"))
            self._scope = ScopeSelector()
            self._scope.setEnabled(False)
            self._scope.currentIndexChanged.connect(self._on_scope_changed)
            row.addWidget(self._scope)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)
        row.addStretch(1)
        outer.addLayout(row)

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
        self._view.setReadOnly(False)          # editable — Save writes it back
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.textChanged.connect(self._on_text_changed)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._view.setFont(mono)
        # Render a Tab as four characters wide (Qt's default is a fixed 80px,
        # which looks far wider than four spaces in a monospace font).
        self._view.setTabStopDistance(QFontMetricsF(mono).horizontalAdvance(" ") * 4)
        self._view.setPlaceholderText("Select a script to view or edit its contents.")
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
        self._refresh()

    def _refresh(self) -> None:
        if not self._confirm_discard():
            return
        self._set_status("loading…")
        self.hub.run_async(
            f"scripts_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_scripts(),
        )

    # ── Selection / view / edit ──────────────────────────────────────────────

    def _on_select(self, cur: Optional[QListWidgetItem],
                   prev: Optional[QListWidgetItem] = None) -> None:
        if self._dirty and prev is not None and cur is not prev:
            if not self._confirm_discard():
                self._list.blockSignals(True)
                self._list.setCurrentItem(prev)
                self._list.blockSignals(False)
                return
        self._dirty = False
        has = cur is not None
        self._delete_btn.setEnabled(has)
        self._download_btn.setEnabled(has)
        self._save_btn.setEnabled(has)
        if not has:
            self._selected = None
            if self._scope is not None:
                self._scope.setEnabled(False)
            return
        name = cur.text()
        self._selected = name
        self._loading = True
        self._view.setPlainText(f"# loading {name} …")
        self._loading = False
        self._load_scope(name)
        self.hub.run_async(
            f"scripts_get:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).get_script(name),
        )

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        dirty = self._view.toPlainText() != self._clean_text
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self._set_status("unsaved changes" if dirty else (self._selected or ""), warn=dirty)

    def _on_save(self) -> None:
        name = self._selected
        if not name:
            return
        self._pending_save_text = self._view.toPlainText()
        content = self._pending_save_text.encode("utf-8")
        self._set_status(f"saving {name}…")
        self.hub.run_async(
            f"scripts_save:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).upload_script(name, content),
        )

    # ── Unit-type scope (library mode only) ──────────────────────────────────

    def _load_scope(self, name: str) -> None:
        if self._scope is None:
            return
        try:
            types = self.hub.fleet.get(self.hostname).get_script_types(name)
        except Exception:  # noqa: BLE001
            types = []
        self._scope.blockSignals(True)
        self._scope.set_from_types(types)
        self._scope.setEnabled(True)
        self._scope.blockSignals(False)

    def _on_scope_changed(self, *_) -> None:
        if self._scope is None or not self._selected:
            return
        try:
            self.hub.fleet.get(self.hostname).set_script_types(
                self._selected, self._scope.types())
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"could not set scope: {exc}", error=True)

    def _confirm_discard(self) -> bool:
        """True if it's OK to discard the current unsaved edits."""
        if not self._dirty:
            return True
        resp = QMessageBox.question(
            self, "Unsaved changes",
            f"Discard unsaved changes to '{self._selected}'?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return resp == QMessageBox.StandardButton.Discard

    # ── Upload / download / delete ───────────────────────────────────────────

    def _on_upload(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Upload script(s)", "", "Python scripts (*.py)"
        )
        if not paths:
            return
        files: List[Tuple[str, bytes]] = []
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    files.append((os.path.basename(p), fh.read()))
            except OSError as exc:
                self._set_status(f"could not read {os.path.basename(p)}: {exc}", error=True)
                return
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"uploading {len(files)} file(s)…")
        self.hub.run_async(f"scripts_upload:{self.hostname}", lambda: _upload_many(client, files))

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

    def _on_download_all(self) -> None:
        dest = QFileDialog.getExistingDirectory(self, "Download all scripts to folder")
        if not dest:
            return
        client = self.hub.fleet.get(self.hostname)
        self._set_status("downloading all scripts…")
        self.hub.run_async(f"scripts_download_all:{self.hostname}",
                           lambda: _download_many(client, dest))

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

    # ── Result routing ───────────────────────────────────────────────────────

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
                content = result if isinstance(result, str) else str(result)
                self._clean_text = content
                self._loading = True
                self._view.setPlainText(content)
                self._loading = False
                self._dirty = False
                self._set_status(name)
        elif op == "scripts_save":
            name = ":".join(parts[2:])
            self._clean_text = self._pending_save_text
            self._dirty = self._view.toPlainText() != self._clean_text
            self._set_status(f"saved {name}" if not self._dirty else "unsaved changes",
                             warn=self._dirty)
        elif op == "scripts_download":
            target = self._pending_download
            self._pending_download = None
            if not target:
                return
            text = result if isinstance(result, str) else str(result)
            try:
                with open(target, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            except OSError as exc:
                self._set_status(f"could not save: {exc}", error=True)
                return
            self._set_status(f"downloaded to {target}")
        elif op == "scripts_download_all":
            self._report("Download all", result)
        elif op == "scripts_delete":
            deleted = result.get("deleted", "") if isinstance(result, dict) else ""
            self._set_status(f"deleted {deleted}")
            self._selected = None
            self._dirty = False
            self._loading = True
            self._view.clear()
            self._loading = False
            self._refresh()
        elif op == "scripts_upload":
            self._report("Upload", result)
            self._refresh()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _report(self, title: str, results) -> None:
        if not isinstance(results, list):
            self._set_status(f"{title.lower()} done")
            return
        ok = [n for n, e in results if e is None]
        bad = [(n, e) for n, e in results if e is not None]
        self._set_status(f"{title.lower()}: {len(ok)} ok" + (f", {len(bad)} failed" if bad else ""))
        if bad:
            lines = "\n".join(f"• {n}: {e}" for n, e in bad)
            QMessageBox.warning(self, f"{title} — some failed",
                                f"{len(ok)} succeeded, {len(bad)} failed:\n\n{lines}")

    def _populate(self, names: List[str]) -> None:
        keep = self._selected
        self._list.blockSignals(True)
        self._list.clear()
        for n in names:
            self._list.addItem(n)
        self._list.blockSignals(False)

        if not names:
            self._set_status("no scripts on this unit")
            self._loading = True
            self._view.clear()
            self._loading = False
            self._clean_text = ""
            self._dirty = False
            self._selected = None
            self._delete_btn.setEnabled(False)
            self._download_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            if self._scope is not None:
                self._scope.setEnabled(False)
            return

        self._set_status(f"{len(names)} script(s)")
        if keep in names:
            items = self._list.findItems(keep, Qt.MatchFlag.MatchExactly)
            if items:
                self._list.setCurrentItem(items[0])

    def _set_status(self, text: str, error: bool = False, warn: bool = False) -> None:
        color = Palette.CRASH if error else (Palette.ARMED if warn else Palette.TEXT_FAINT)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
