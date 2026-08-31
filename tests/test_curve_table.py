"""_CurveTable (shared by measured curves, the component library, and the active-component
baseline) supports pasting a measured/VNA sweep and clearing the whole table — cross-program,
so every table in the app behaves the same."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import _CurveTable

_app = QApplication.instance() or QApplication([])


def _table():
    fired = {"n": 0}
    t = _CurveTable(on_changed=lambda: fired.__setitem__("n", fired["n"] + 1),
                    headers=("freq (Hz)", "Δ dB"))
    return t, fired


def test_paste_a_vna_sweep_block():
    t, fired = _table()
    _app.clipboard().setText("freq\tdB\n1.0e9\t-4.2\n2.0e9\t-4.8\n3.0e9\t-5.5")
    t._deselect()
    assert t._paste_csv() is True
    assert t.rows(strict=False) == [[1.0e9, -4.2], [2.0e9, -4.8], [3.0e9, -5.5]]
    assert fired["n"] > 0                              # the change hook fired


def test_paste_accepts_comma_and_space_separators():
    t, _ = _table()
    _app.clipboard().setText("1e9, -4\n2e9 -6")
    t._deselect()
    assert t._paste_csv() is True
    assert t.rows(strict=False) == [[1.0e9, -4.0], [2.0e9, -6.0]]


def test_clear_rows_empties_the_table_and_is_undoable():
    t, fired = _table()
    t.set_rows([[1.0e9, -4.0], [2.0e9, -6.0]])
    t.clear_rows()
    assert t.rows(strict=False) == []
    t.undo()                                           # clear is undoable
    assert t.rows(strict=False) == [[1.0e9, -4.0], [2.0e9, -6.0]]


def test_clear_rows_on_empty_is_a_noop():
    t, fired = _table()
    t.clear_rows()
    assert t.rows(strict=False) == []
