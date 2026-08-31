"""
ScriptsPanel — the Scripts sub-tab of the Library (and unit detail).

A light, IDE-style editor for the unit's / library's .py scripts:

  • a file list (double-click, or right-click → Open, to open a script; single-click
    just selects it; right-click for actions),
  • editor tabs — several scripts open at once, each with a close ✕ and an unsaved
    dot; closing one with unsaved edits prompts to save,
  • a syntax-highlighted code editor (ui/code_editor) with a line-number gutter,
  • the library's per-unit-type scope ("Applies to") for the active script,
  • Upload one/many, Download one/all, Delete, and Save (writes the active tab back).

Leaving the Scripts sub-tab, or changing the unit-type library, with unsaved edits
prompts first (see `can_leave`, called by LibraryTab).

All network calls go through DataHub.run_async (off the GUI thread); results arrive
on the shared task_done signal, filtered here to this host + ops.

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
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from config import DEFAULT_UNIT_TYPE, UNIT_TYPE_LABELS
from .code_editor import CodeEditor
from .qt_adapter import DataHub
from .scope_selector import ScopeSelector, confirm_delete, scope_label
from .theme import Palette
from .widgets import natural_key

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


# ── editor tab strip ─────────────────────────────────────────────────────────────

class _EditorTab(QFrame):
    """One open-file tab: filename, an unsaved dot, and an always-present close ✕."""
    activated = pyqtSignal(str)
    closed = pyqtSignal(str)

    def __init__(self, name: str, active: bool, dirty: bool, parent=None):
        super().__init__(parent)
        self._name = name
        self.setObjectName("etab")
        self.setProperty("active", active)
        row = QHBoxLayout(self)
        row.setContentsMargins(11, 0, 7, 0)
        row.setSpacing(7)
        self._label = QLabel(name)
        self._label.setObjectName("etabname")
        f = QFont("IBM Plex Mono"); f.setStyleHint(QFont.StyleHint.Monospace); f.setPointSize(9)
        self._label.setFont(f)
        row.addWidget(self._label)
        self._dot = QLabel("●")
        self._dot.setObjectName("etabdot")
        self._dot.setVisible(dirty)
        row.addWidget(self._dot)
        self._close = QToolButton()
        self._close.setObjectName("etabx")
        self._close.setText("✕")
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(lambda: self.closed.emit(self._name))
        row.addWidget(self._close)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_dirty(self, dirty: bool) -> None:
        self._dot.setVisible(dirty)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._name)
        super().mousePressEvent(e)


class ScriptsPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._active_type = DEFAULT_UNIT_TYPE   # library view: set by the unit-type selector
        self._upload_new: set = set()           # names uploaded that didn't exist before
        self._all_names: List[str] = []         # last listing, re-filtered as you search
        self._selected: Optional[str] = None    # file highlighted in the list (single-click)
        self._pending_download: Optional[str] = None

        # ── open editor tabs (several scripts open at once) ──
        self._tabs: List[str] = []                     # open file names, in tab order
        self._active: Optional[str] = None             # file shown in the editor
        self._clean: Dict[str, str] = {}               # last saved/loaded content per open file
        self._buf: Dict[str, str] = {}                 # current editor content per open file
        self._loaded: Dict[str, bool] = {}             # content fetched for this open file?
        self._pending_save: Dict[str, str] = {}        # text sent to the in-flight save
        self._tab_widgets: Dict[str, _EditorTab] = {}
        self._loading_editor = False                   # suppress dirty while swapping content

        self._build()
        self.hub.task_done.connect(self._on_task_done)

    # ── build ────────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(8)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_file_panel())
        split.addWidget(self._build_editor_panel())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 560])
        outer.addWidget(split, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self.setStyleSheet(self._QSS)

    def _build_file_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("filepanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(12, 10, 8, 8)
        eyebrow = QLabel("SCRIPTS")
        eyebrow.setObjectName("eyebrow")
        head.addWidget(eyebrow)
        self._count = QLabel("")
        self._count.setObjectName("count")
        head.addWidget(self._count)
        head.addStretch(1)
        self._upload_btn = QToolButton()
        self._upload_btn.setText("⬆")
        self._upload_btn.setObjectName("headbtn")
        self._upload_btn.setToolTip("Upload script(s)…")
        self._upload_btn.clicked.connect(self._on_upload)
        head.addWidget(self._upload_btn)
        self._refresh_btn = QToolButton()
        self._refresh_btn.setText("⟳")
        self._refresh_btn.setObjectName("headbtn")
        self._refresh_btn.setToolTip("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        head.addWidget(self._refresh_btn)
        v.addLayout(head)

        self._search = QLineEdit()
        self._search.setObjectName("search")
        self._search.setPlaceholderText("Search scripts…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _=0: self._populate(self._all_names))
        srow = QHBoxLayout(); srow.setContentsMargins(12, 0, 12, 8); srow.addWidget(self._search)
        v.addLayout(srow)

        self._list = QListWidget()
        self._list.setObjectName("filelist")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.currentItemChanged.connect(self._on_list_select)
        self._list.itemDoubleClicked.connect(self._on_list_open)   # open only on double-click
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._list_context_menu)
        v.addWidget(self._list, stretch=1)

        panel.setMinimumWidth(210)
        return panel

    def _build_editor_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("editorpanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # tab strip + actions
        tabrow = QHBoxLayout()
        tabrow.setContentsMargins(0, 0, 6, 0)
        tabrow.setSpacing(0)
        self._tabbar = QWidget()
        self._tabbar.setObjectName("tabbar")
        self._tabbar_layout = QHBoxLayout(self._tabbar)
        self._tabbar_layout.setContentsMargins(0, 0, 0, 0)
        self._tabbar_layout.setSpacing(0)
        self._tabbar_layout.addStretch(1)
        tabrow.addWidget(self._tabbar, stretch=1)

        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("savebtn")
        self._save_btn.clicked.connect(self._on_save)
        self._save_btn.setEnabled(False)
        tabrow.addWidget(self._save_btn)
        self._download_btn = QToolButton()
        self._download_btn.setText("⬇"); self._download_btn.setObjectName("actbtn")
        self._download_btn.setToolTip("Download the active script…")
        self._download_btn.clicked.connect(lambda: self._download(self._active))
        tabrow.addWidget(self._download_btn)
        self._delete_btn = QToolButton()
        self._delete_btn.setText("🗑"); self._delete_btn.setObjectName("actbtn")
        self._delete_btn.setToolTip("Delete the active script")
        self._delete_btn.clicked.connect(lambda: self._delete(self._active))
        tabrow.addWidget(self._delete_btn)
        v.addWidget(self._wrap_row(tabrow, "tabstrip"))

        # scope row (library mode only): which unit types receive the active script
        self._scope: Optional[ScopeSelector] = None
        if self.hostname == LIBRARY_HOST:
            scoperow = QHBoxLayout()
            scoperow.setContentsMargins(12, 6, 12, 6)
            self._crumb = QLabel("")
            self._crumb.setObjectName("crumb")
            scoperow.addWidget(self._crumb)
            scoperow.addStretch(1)
            lbl = QLabel("Applies to")
            lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            scoperow.addWidget(lbl)
            self._scope = ScopeSelector()
            self._scope.setEnabled(False)
            self._scope.currentIndexChanged.connect(self._on_scope_changed)
            scoperow.addWidget(self._scope)
            v.addWidget(self._wrap_row(scoperow, "scoperow"))

        self._editor = CodeEditor()
        self._editor.setPlaceholderText("Double-click a script to open it.")
        self._editor.textChanged.connect(self._on_editor_changed)
        v.addWidget(self._editor, stretch=1)

        self._set_actions_enabled(False)
        return panel

    def _wrap_row(self, layout, objname: str) -> QWidget:
        w = QFrame(); w.setObjectName(objname); w.setLayout(layout)
        return w

    # ── shown / refresh ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self._refresh()

    def set_active_type(self, unit_type: str) -> None:
        self._active_type = unit_type
        if self._scope is not None:        # library mode: re-filter the list
            self._refresh()

    def _types_for(self, name: str) -> list:
        try:
            return self.hub.fleet.get(self.hostname).get_script_types(name)
        except Exception:  # noqa: BLE001
            return []

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"scripts_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_scripts(),
        )

    # ── list selection / open ────────────────────────────────────────────────────
    def _on_list_select(self, cur: Optional[QListWidgetItem], _prev=None) -> None:
        self._selected = cur.text() if cur is not None else None

    def _on_list_open(self, item: Optional[QListWidgetItem]) -> None:
        if item is not None:
            self._open_tab(item.text())

    def _list_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:                       # empty area — library-wide actions
            menu = QMenu(self)
            menu.addAction("Upload script(s)…", self._on_upload)
            menu.addAction("Download all…", self._on_download_all)
            menu.addAction("Refresh", self._refresh)
            menu.exec(self._list.mapToGlobal(pos))
            return
        name = item.text()
        self._selected = name
        menu = QMenu(self)
        menu.addAction("Open", lambda: self._open_tab(name))
        menu.addAction("Download…", lambda: self._download(name))
        if self._scope is not None:
            sub = menu.addMenu(f"Applies to: {scope_label(self._types_for(name))}")
            from config import UNIT_TYPES
            sub.addAction("Shared (all units)", lambda: self._set_scope(name, []))
            for t in UNIT_TYPES:
                sub.addAction(f"{UNIT_TYPE_LABELS.get(t, t)} only",
                              lambda _c=False, ut=t: self._set_scope(name, [ut]))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._delete(name))
        menu.exec(self._list.mapToGlobal(pos))

    # ── tabs ─────────────────────────────────────────────────────────────────────
    def _open_tab(self, name: str) -> None:
        if name not in self._tabs:
            self._tabs.append(name)
            self._buf.setdefault(name, "")
            self._clean.setdefault(name, "")
            self._loaded[name] = False
        self._active = name
        self._render_tabs()
        if not self._loaded.get(name):
            self._loading_editor = True
            self._editor.setPlainText(f"# loading {name} …")
            self._loading_editor = False
            self.hub.run_async(
                f"scripts_get:{self.hostname}:{name}",
                lambda: self.hub.fleet.get(self.hostname).get_script(name),
            )
        else:
            self._load_editor(name)
        self._load_scope(name)
        self._set_actions_enabled(True)

    def _activate_tab(self, name: str) -> None:
        if name == self._active or name not in self._tabs:
            return
        self._active = name
        self._render_tabs()
        if self._loaded.get(name):
            self._load_editor(name)
        else:
            self._loading_editor = True
            self._editor.setPlainText(f"# loading {name} …")
            self._loading_editor = False
        self._load_scope(name)

    def _close_tab(self, name: str) -> None:
        if self._is_dirty(name) and not self._confirm_save([name], "close"):
            return
        i = self._tabs.index(name) if name in self._tabs else -1
        for d in (self._buf, self._clean, self._loaded, self._pending_save):
            d.pop(name, None)
        if i >= 0:
            self._tabs.pop(i)
        if self._active == name:
            if self._tabs:
                self._active = self._tabs[max(0, i - 1)]
                self._render_tabs()
                self._load_editor(self._active)
                self._load_scope(self._active)
            else:
                self._active = None
                self._loading_editor = True
                self._editor.clear()
                self._loading_editor = False
                self._render_tabs()
                self._set_actions_enabled(False)
        else:
            self._render_tabs()

    def _render_tabs(self) -> None:
        # rebuild the strip (cheap — a handful of tabs)
        while self._tabbar_layout.count() > 1:      # keep the trailing stretch
            item = self._tabbar_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._tab_widgets = {}
        for name in self._tabs:
            tab = _EditorTab(name, name == self._active, self._is_dirty(name))
            tab.activated.connect(self._activate_tab)
            tab.closed.connect(self._close_tab)
            self._tab_widgets[name] = tab
            self._tabbar_layout.insertWidget(self._tabbar_layout.count() - 1, tab)
        self._refresh_save_button()

    def _is_dirty(self, name: str) -> bool:
        return bool(self._loaded.get(name)) and self._buf.get(name, "") != self._clean.get(name, "")

    def _load_editor(self, name: str) -> None:
        self._loading_editor = True
        self._editor.setPlainText(self._buf.get(name, ""))
        self._loading_editor = False
        self._refresh_save_button()

    def _on_editor_changed(self) -> None:
        if self._loading_editor or self._active is None:
            return
        self._buf[self._active] = self._editor.toPlainText()
        tab = self._tab_widgets.get(self._active)
        if tab is not None:
            tab.set_dirty(self._is_dirty(self._active))
        self._refresh_save_button()

    def _refresh_save_button(self) -> None:
        dirty = self._active is not None and self._is_dirty(self._active)
        self._save_btn.setEnabled(dirty)
        self._save_btn.setText("Save" if dirty else "Saved")

    def _set_actions_enabled(self, on: bool) -> None:
        self._download_btn.setEnabled(on)
        self._delete_btn.setEnabled(on)

    # ── save ─────────────────────────────────────────────────────────────────────
    def _on_save(self) -> None:
        name = self._active
        if not name or not self._is_dirty(name):
            return
        text = self._buf.get(name, "")
        self._pending_save[name] = text
        self._set_status(f"saving {name}…")
        self.hub.run_async(
            f"scripts_save:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).upload_script(name, text.encode("utf-8")),
        )

    # ── scope (library mode) ─────────────────────────────────────────────────────
    def _load_scope(self, name: str) -> None:
        if self._scope is None:
            return
        if hasattr(self, "_crumb"):
            self._crumb.setText(f"library  ›  {UNIT_TYPE_LABELS.get(self._active_type, self._active_type)}  ›  {name or ''}")
        types = self._types_for(name) if name else []
        self._scope.blockSignals(True)
        self._scope.set_from_types(types)
        self._scope.setEnabled(name is not None)
        self._scope.blockSignals(False)

    def _on_scope_changed(self, *_) -> None:
        if self._scope is None or not self._active:
            return
        self._set_scope(self._active, self._scope.types())

    def _set_scope(self, name: str, types: list) -> None:
        try:
            self.hub.fleet.get(self.hostname).set_script_types(name, list(types))
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"could not set scope: {exc}", error=True)
            return
        if name == self._active and self._scope is not None:
            self._scope.blockSignals(True)
            self._scope.set_from_types(types)
            self._scope.blockSignals(False)

    # ── leaving guard (called by LibraryTab before switching sub-tab / type) ──────
    def can_leave(self) -> bool:
        """True if it's OK to leave the Scripts view (no unsaved edits, or the user
        chose to save / discard). Prompts once for all dirty tabs."""
        dirty = [n for n in self._tabs if self._is_dirty(n)]
        if not dirty:
            return True
        return self._confirm_save(dirty, "leave")

    def _confirm_save(self, names: List[str], why: str) -> bool:
        """Save / Don't save / Cancel for `names`. Returns True to proceed."""
        listing = ", ".join(names) if len(names) <= 3 else f"{len(names)} scripts"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"{listing} {'has' if len(names) == 1 else 'have'} unsaved changes.")
        box.setInformativeText("Do you want to save them before continuing?")
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel:
            return False
        if clicked is save:
            for n in names:
                text = self._buf.get(n, "")
                self._pending_save[n] = text
                self.hub.run_async(
                    f"scripts_save:{self.hostname}:{n}",
                    lambda nn=n, tt=text: self.hub.fleet.get(self.hostname).upload_script(
                        nn, tt.encode("utf-8")))
        else:  # Don't save — drop the edits (revert to last clean)
            for n in names:
                self._buf[n] = self._clean.get(n, "")
                if n == self._active:
                    self._load_editor(n)
                tab = self._tab_widgets.get(n)
                if tab is not None:
                    tab.set_dirty(False)
        self._refresh_save_button()
        return True

    # ── upload / download / delete ───────────────────────────────────────────────
    def _on_upload(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Upload script(s)", "", "Python scripts (*.py)")
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
        if self._scope is not None:
            try:
                existing = set(client.list_scripts())
            except Exception:  # noqa: BLE001
                existing = set()
            self._upload_new = {name for name, _ in files if name not in existing}
        self._set_status(f"uploading {len(files)} file(s)…")
        self.hub.run_async(f"scripts_upload:{self.hostname}", lambda: _upload_many(client, files))

    def _download(self, name: Optional[str]) -> None:
        if not name:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Download script", name, "Python scripts (*.py)")
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

    def _delete(self, name: Optional[str]) -> None:
        if not name:
            return
        if self._scope is not None:
            action = confirm_delete(self, "script", name, self._types_for(name),
                                    self._active_type, self._unshare_script)
            if action == "cancel":
                return
            if action == "unshared":
                self._refresh()
                return
        else:
            resp = QMessageBox.question(
                self, "Delete script",
                f"Delete '{name}' from {self.hostname}?\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if resp != QMessageBox.StandardButton.Yes:
                return
        self._set_status(f"deleting {name}…")
        self.hub.run_async(
            f"scripts_delete:{self.hostname}:{name}",
            lambda: self.hub.fleet.get(self.hostname).delete_script(name),
        )

    def _unshare_script(self, name: str, new_types: list) -> None:
        self.hub.fleet.get(self.hostname).set_script_types(name, list(new_types))

    # ── result routing ───────────────────────────────────────────────────────────
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
            if name in self._tabs:
                content = result if isinstance(result, str) else str(result)
                self._clean[name] = content
                self._buf[name] = content
                self._loaded[name] = True
                if name == self._active:
                    self._load_editor(name)
                tab = self._tab_widgets.get(name)
                if tab is not None:
                    tab.set_dirty(False)
                self._set_status(name)
        elif op == "scripts_save":
            name = ":".join(parts[2:])
            if name in self._pending_save:
                self._clean[name] = self._pending_save.pop(name)
            if name == self._active:
                self._refresh_save_button()
            tab = self._tab_widgets.get(name)
            if tab is not None:
                tab.set_dirty(self._is_dirty(name))
            self._set_status(f"saved {name}")
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
            # close its tab if open
            for n in list(self._tabs):
                if n == deleted or (not deleted and n == self._selected):
                    for d in (self._buf, self._clean, self._loaded, self._pending_save):
                        d.pop(n, None)
                    self._tabs.remove(n)
                    if self._active == n:
                        self._active = self._tabs[-1] if self._tabs else None
            if self._active:
                self._load_editor(self._active)
            else:
                self._loading_editor = True; self._editor.clear(); self._loading_editor = False
                self._set_actions_enabled(False)
            self._render_tabs()
            self._set_status(f"deleted {deleted}")
            self._refresh()
        elif op == "scripts_upload":
            if self._scope is not None and self._upload_new:
                ok = {n for n, e in result if e is None} if isinstance(result, list) else set()
                client = self.hub.fleet.get(self.hostname)
                for name in (self._upload_new & ok):
                    try:
                        client.set_script_types(name, [self._active_type])
                    except Exception:  # noqa: BLE001
                        pass
            self._upload_new = set()
            self._report("Upload", result)
            self._refresh()

    # ── helpers ──────────────────────────────────────────────────────────────────
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
        self._all_names = list(names)
        total = len(names)
        query = self._search.text().strip().lower()
        if self._scope is not None:
            names = [n for n in names
                     if m.applies_to_type(self._types_for(n), self._active_type)]
        if query:
            names = [n for n in names if query in n.lower()]
        names = sorted(names, key=natural_key)
        self._list.blockSignals(True)
        self._list.clear()
        for n in names:
            self._list.addItem(n)
        self._list.blockSignals(False)
        self._count.setText(f"{len(names)} file(s)")

        if not names:
            if query:
                self._set_status(f"no scripts match “{query}”")
            elif self._scope is not None:
                lbl = UNIT_TYPE_LABELS.get(self._active_type, self._active_type)
                self._set_status(f"no {lbl} scripts yet — Upload to add them"
                                 + (f" ({total} in the library for other types)" if total else ""))
            else:
                self._set_status("no scripts on this unit")
            return

        if query:
            self._set_status(f"{len(names)} script(s) match · {total} total")
        elif self._scope is not None:
            lbl = UNIT_TYPE_LABELS.get(self._active_type, self._active_type)
            self._set_status(f"{len(names)} script(s) for {lbl} · {total} total")
        else:
            self._set_status(f"{len(names)} script(s)")
        if keep in names:
            items = self._list.findItems(keep, Qt.MatchFlag.MatchExactly)
            if items:
                self._list.blockSignals(True)
                self._list.setCurrentItem(items[0])
                self._list.blockSignals(False)

    def _set_status(self, text: str, error: bool = False, warn: bool = False) -> None:
        color = Palette.CRASH if error else (Palette.ARMED if warn else Palette.TEXT_FAINT)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    # ── styling ──────────────────────────────────────────────────────────────────
    _QSS = f"""
    QFrame#filepanel {{ background: {Palette.SURFACE_ALT};
        border: 1px solid {Palette.BORDER}; border-radius: 10px; }}
    QFrame#editorpanel {{ background: {Palette.SURFACE};
        border: 1px solid {Palette.BORDER}; border-radius: 10px; }}
    QLabel#eyebrow {{ font-size: 11px; font-weight: 600; letter-spacing: 1px;
        color: {Palette.TEXT_MUTED}; }}
    QLabel#count {{ font-size: 10px; color: {Palette.TEXT_FAINT}; margin-left: 6px; }}
    QToolButton#headbtn, QToolButton#actbtn {{ border: none; background: transparent;
        color: {Palette.TEXT_MUTED}; font-size: 15px; padding: 3px 6px; border-radius: 6px; }}
    QToolButton#headbtn:hover, QToolButton#actbtn:hover {{ background: {Palette.INSET};
        color: {Palette.TEXT}; }}
    QLineEdit#search {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER};
        border-radius: 7px; padding: 6px 9px; color: {Palette.TEXT}; }}
    QListWidget#filelist {{ background: transparent; border: none; padding: 2px 6px 8px; }}
    QListWidget#filelist::item {{ padding: 6px 8px; border-radius: 7px;
        color: {Palette.TEXT}; }}
    QListWidget#filelist::item:hover {{ background: {Palette.INSET}; }}
    QListWidget#filelist::item:selected {{ background: {Palette.ACCENT_SOFT};
        color: {Palette.ACCENT_INK}; }}
    QFrame#tabstrip {{ background: {Palette.SURFACE_ALT};
        border-bottom: 1px solid {Palette.BORDER}; }}
    QFrame#scoperow {{ background: {Palette.SURFACE};
        border-bottom: 1px solid {Palette.BORDER}; }}
    QLabel#crumb {{ font-family: "IBM Plex Mono"; font-size: 11px; color: {Palette.TEXT_MUTED}; }}
    QWidget#tabbar {{ background: {Palette.SURFACE_ALT}; }}
    QFrame#etab {{ background: {Palette.SURFACE_ALT}; border-right: 1px solid {Palette.BORDER};
        min-height: 30px; }}
    QFrame#etab[active="true"] {{ background: {Palette.SURFACE};
        border-top: 2px solid {Palette.ACCENT}; }}
    QLabel#etabname {{ color: {Palette.TEXT_MUTED}; }}
    QFrame#etab[active="true"] QLabel#etabname {{ color: {Palette.TEXT}; }}
    QLabel#etabdot {{ color: {Palette.ARMED}; font-size: 12px; }}
    QToolButton#etabx {{ border: none; background: transparent; color: {Palette.TEXT_FAINT};
        font-size: 12px; padding: 0 3px; border-radius: 4px; }}
    QToolButton#etabx:hover {{ background: {Palette.INSET}; color: {Palette.TEXT}; }}
    QPushButton#savebtn {{ background: {Palette.ACCENT}; color: #fff; border: none;
        border-radius: 6px; padding: 6px 14px; font-weight: 500; }}
    QPushButton#savebtn:hover {{ background: #25597E; }}
    QPushButton#savebtn:disabled {{ background: {Palette.SURFACE_ALT};
        color: {Palette.TEXT_FAINT}; border: 1px solid {Palette.BORDER}; }}
    """
