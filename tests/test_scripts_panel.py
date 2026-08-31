"""ScriptsPanel — the IDE-style Scripts editor: files open into editor tabs (only
on double-click / context-Open, not single-click select), each tab tracks unsaved
edits independently, Save writes the active tab back, `can_leave` guards navigation
away when edits are unsaved, and scripts live in a folder tree (organizational
folders that become real subdirectories on the unit at deploy). Driven through a
stub DataHub whose run_async resolves synchronously, and — for folders — the real
LibraryClient over a temp library so the store/client folder logic is exercised."""
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from api.fleet import LIBRARY_HOST
from state.library_client import LibraryClient
from state.library_store import LibraryStore
from ui.scripts_panel import ScriptsPanel

_app = QApplication.instance() or QApplication([])
_ROLE = Qt.ItemDataRole.UserRole


class _Client:
    def __init__(self, store, types):
        self._store, self._types = store, types
    def list_scripts(self):
        return list(self._store)
    def get_script(self, n):
        return self._store[n]
    def upload_script(self, n, content):
        self._store[n] = content.decode()
        return {"ok": True}
    def delete_script(self, n):
        self._store.pop(n, None)
        return {"deleted": n}
    def get_script_types(self, n):
        return self._types.get(n, [])
    def set_script_types(self, n, t):
        self._types[n] = list(t)


class _Fleet:
    def __init__(self, client):
        self._client = client
    def get(self, host):
        return self._client


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    def __init__(self, client):
        super().__init__()
        self.fleet = _Fleet(client)
    def run_async(self, label, fn):          # synchronous in tests
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001
            r = exc
        self.task_done.emit(label, r)


def _panel():
    store = {
        "fm_chirp_tx.py": '"""FM chirp."""\nCAL = "fm_chirp"\n',
        "comb_tx.py": '"""Comb."""\nCAL = "comb"\n',
        "gps_l1ca_tx.py": '"""GPS."""\nPRN = 1\n',
    }
    hub = _Hub(_Client(store, {n: [] for n in store}))
    p = ScriptsPanel(LIBRARY_HOST, hub)
    p.on_shown()                             # loads the list synchronously
    return p, store


def _tree_files(p):
    out = []
    def walk(it):
        for i in range(it.childCount()):
            walk(it.child(i))
        pay = it.data(0, _ROLE)
        if pay and pay[0] == "file":
            out.append(pay[1])
    walk(p._tree.invisibleRootItem())
    return out


def _file_item(p, name):
    def walk(it):
        for i in range(it.childCount()):
            found = walk(it.child(i))
            if found:
                return found
        pay = it.data(0, _ROLE)
        return it if pay and pay == ("file", name) else None
    return walk(p._tree.invisibleRootItem())


# ── stub-backed behaviour (tabs, dirty, save, switch, close) ────────────────────

def test_tree_loads_and_single_click_does_not_open():
    p, _ = _panel()
    assert set(_tree_files(p)) == {"fm_chirp_tx.py", "comb_tx.py", "gps_l1ca_tx.py"}
    # selecting a file (single-click semantics) must NOT open an editor tab
    p._tree.setCurrentItem(_file_item(p, "fm_chirp_tx.py"))
    assert p._selected == "fm_chirp_tx.py"
    assert p._tabs == [] and p._active is None


def test_double_click_open_loads_content_into_a_tab():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")            # what itemDoubleClicked triggers
    assert p._tabs == ["fm_chirp_tx.py"] and p._active == "fm_chirp_tx.py"
    assert p._loaded["fm_chirp_tx.py"] is True
    assert p._editor.toPlainText().startswith('"""FM chirp."""')
    p._open_tab("comb_tx.py")
    assert p._tabs == ["fm_chirp_tx.py", "comb_tx.py"] and p._active == "comb_tx.py"


def test_edit_marks_dirty_per_tab_and_save_clears_it():
    p, store = _panel()
    p._open_tab("comb_tx.py")
    assert not p._is_dirty("comb_tx.py")
    p._editor.setPlainText(p._editor.toPlainText() + "\nEXTRA = 1\n")
    assert p._is_dirty("comb_tx.py") and p._save_btn.isEnabled()
    p._on_save()
    assert not p._is_dirty("comb_tx.py")
    assert "EXTRA = 1" in store["comb_tx.py"]
    assert p._save_btn.text() == "Saved"


def test_switching_tabs_preserves_each_buffer():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")
    p._open_tab("comb_tx.py")
    p._editor.setPlainText("comb edit\n")
    p._activate_tab("fm_chirp_tx.py")
    assert p._editor.toPlainText().startswith('"""FM chirp."""')
    p._activate_tab("comb_tx.py")
    assert p._editor.toPlainText() == "comb edit\n"
    assert p._is_dirty("comb_tx.py") and not p._is_dirty("fm_chirp_tx.py")


def test_can_leave_true_when_clean_and_close_clean_tab():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")
    assert p.can_leave() is True
    p._close_tab("fm_chirp_tx.py")
    assert p._tabs == [] and p._active is None


# ── folder tree (real LibraryClient over a temp library) ────────────────────────

def _lib_panel():
    tmp = Path(tempfile.mkdtemp()) / "library.json"
    store = LibraryStore(tmp)
    for n, folder in [("fm_chirp_tx.py", "Chirps"), ("comb_tx.py", "Chirps"),
                      ("gps_l1ca_tx.py", "GPS PRN"), ("cw_tx.py", "")]:
        store.upsert_script(m.LibraryScript(name=n, content=f'"""{n}"""\n', folder=folder))
    store.add_folder("Empty")
    client = LibraryClient(store)
    p = ScriptsPanel(LIBRARY_HOST, _Hub(client))
    p.on_shown()
    return p, client


def _folders(p):
    out = {}
    root = p._tree.invisibleRootItem()
    for i in range(root.childCount()):
        it = root.child(i)
        pay = it.data(0, _ROLE)
        if pay and pay[0] == "folder":
            out[pay[1]] = [it.child(j).text(0) for j in range(it.childCount())]
    return out


def test_tree_groups_scripts_by_folder_and_keeps_empty_folders():
    p, _ = _lib_panel()
    folders = _folders(p)
    assert folders["Chirps"] == ["comb_tx.py", "fm_chirp_tx.py"]
    assert folders["GPS PRN"] == ["gps_l1ca_tx.py"]
    assert folders["Empty"] == []                      # declared empty folder persists
    assert "cw_tx.py" in _tree_files(p)                # a root (folderless) script


def test_move_script_to_folder_updates_tree_and_never_changes_identity():
    p, client = _lib_panel()
    p._move_to_folder("cw_tx.py", "GPS PRN")
    assert client.get_script_folder("cw_tx.py") == "GPS PRN"
    assert _folders(p)["GPS PRN"] == ["cw_tx.py", "gps_l1ca_tx.py"]
    # the script's identity (name) is unchanged — references stay valid
    assert "cw_tx.py" in _tree_files(p)


def test_delete_folder_moves_its_scripts_to_root_not_deletes_them():
    p, client = _lib_panel()
    p._delete_folder = ScriptsPanel._delete_folder.__get__(p)   # bypass the confirm dialog
    client.delete_folder("Chirps")                              # scripts -> root
    p._populate(p._all_names)
    assert "Chirps" not in _folders(p)
    assert {"comb_tx.py", "fm_chirp_tx.py"} <= set(_tree_files(p))
    assert client.get_script_folder("fm_chirp_tx.py") == ""


def test_rename_folder_carries_scripts():
    p, client = _lib_panel()
    client.rename_folder("Chirps", "Sweeps")
    p._populate(p._all_names)
    assert "Sweeps" in _folders(p) and "Chirps" not in _folders(p)
    assert client.get_script_folder("fm_chirp_tx.py") == "Sweeps"


def test_folder_survives_a_content_save():
    p, client = _lib_panel()
    client.upload_script("fm_chirp_tx.py", b'"""edited"""\n')   # a Save
    assert client.get_script_folder("fm_chirp_tx.py") == "Chirps"
