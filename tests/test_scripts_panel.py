"""ScriptsPanel — the IDE-style Scripts editor: files open into editor tabs (only
on double-click / context-Open, not single-click select), each tab tracks unsaved
edits independently, Save writes the active tab back, and `can_leave` guards
navigation away when edits are unsaved. Driven through a stub DataHub whose
run_async resolves synchronously so the task_done routing is exercised end to end."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.fleet import LIBRARY_HOST
from ui.scripts_panel import ScriptsPanel

_app = QApplication.instance() or QApplication([])


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


def test_list_loads_and_single_click_does_not_open():
    p, _ = _panel()
    assert p._list.count() == 3
    # selecting a row (single-click semantics) must NOT open an editor tab
    p._list.setCurrentRow(0)
    assert p._selected is not None
    assert p._tabs == [] and p._active is None


def test_double_click_open_loads_content_into_a_tab():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")            # what itemDoubleClicked triggers
    assert p._tabs == ["fm_chirp_tx.py"]
    assert p._active == "fm_chirp_tx.py"
    assert p._loaded["fm_chirp_tx.py"] is True
    assert p._editor.toPlainText().startswith('"""FM chirp."""')
    # a second file opens a second tab and becomes active
    p._open_tab("comb_tx.py")
    assert p._tabs == ["fm_chirp_tx.py", "comb_tx.py"]
    assert p._active == "comb_tx.py"


def test_edit_marks_dirty_per_tab_and_save_clears_it():
    p, store = _panel()
    p._open_tab("comb_tx.py")
    assert not p._is_dirty("comb_tx.py")
    p._editor.setPlainText(p._editor.toPlainText() + "\nEXTRA = 1\n")
    assert p._is_dirty("comb_tx.py")
    assert p._save_btn.isEnabled() and p._save_btn.text() == "Save"
    p._on_save()
    assert not p._is_dirty("comb_tx.py")
    assert "EXTRA = 1" in store["comb_tx.py"]     # persisted through upload_script
    assert p._save_btn.text() == "Saved"


def test_switching_tabs_preserves_each_buffer():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")
    p._open_tab("comb_tx.py")
    p._editor.setPlainText("comb edit\n")         # edits the active (comb) buffer
    p._activate_tab("fm_chirp_tx.py")
    assert p._editor.toPlainText().startswith('"""FM chirp."""')
    p._activate_tab("comb_tx.py")
    assert p._editor.toPlainText() == "comb edit\n"   # comb's unsaved edit survived
    assert p._is_dirty("comb_tx.py") and not p._is_dirty("fm_chirp_tx.py")


def test_can_leave_true_when_clean_and_close_clean_tab():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")
    assert p.can_leave() is True                  # nothing unsaved
    p._close_tab("fm_chirp_tx.py")                # clean tab closes without a prompt
    assert p._tabs == [] and p._active is None


def test_scope_change_writes_types_for_the_active_script():
    p, _ = _panel()
    p._open_tab("fm_chirp_tx.py")
    p._set_scope("fm_chirp_tx.py", ["broadcaster"])
    assert p._types_for("fm_chirp_tx.py") == ["broadcaster"]
