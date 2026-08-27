"""Usability of the calibration curve grid (_CurveTable): the current-cell is the
only in-focus visual (no lingering selection fill), double-click / type to edit,
Esc / click-away clears focus, Del clears the current cell, and Ctrl+Z / Ctrl+Y
undo & redo. Offscreen widget tests driving the overridden handlers."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QWidget

from ui.calibration_panel import _CurveTable

_app = QApplication.instance() or QApplication([])


def _table(points=((40, -36), (74, -2.5))):
    t = _CurveTable()
    t.set_points([{"gain_db": g, "power_dbm": p} for g, p in points])
    return t


def _key(table, key, mods=Qt.KeyboardModifier.NoModifier):
    table.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))


# ── configuration: no selection fill, double-click to edit ───────────────────────

def test_no_selection_mode_and_double_click_editing():
    t = _CurveTable()
    assert t.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    trig = t.editTriggers()
    assert trig & QAbstractItemView.EditTrigger.DoubleClicked
    # single-click-to-edit was reverted — SelectedClicked must NOT be enabled
    assert not (trig & QAbstractItemView.EditTrigger.SelectedClicked)
    # typing on the current cell still edits (direct keyboard entry)
    assert trig & QAbstractItemView.EditTrigger.AnyKeyPressed


def test_editing_opens_on_demand():
    t = _table()
    assert t.state() != QAbstractItemView.State.EditingState
    t.editItem(t.item(0, 0))              # what a double-click ends up doing
    assert t.state() == QAbstractItemView.State.EditingState


# ── clearing focus ───────────────────────────────────────────────────────────────

def test_escape_clears_current_cell():
    t = _table()
    t.setCurrentCell(0, 0)
    assert t.currentRow() == 0
    _key(t, Qt.Key.Key_Escape)
    assert t.currentRow() == -1


def test_focus_leaving_grid_clears_current_cell():
    t = _table()
    t.setCurrentCell(1, 1)
    outside = QWidget()
    t._on_focus_changed(None, outside)   # focus moved to a widget outside the grid
    assert t.currentRow() == -1


def test_focus_staying_in_grid_keeps_current_cell():
    t = _table()
    t.setCurrentCell(1, 0)
    t._on_focus_changed(None, t)         # focus is still the grid itself
    assert t.currentRow() == 1


# ── delete clears the current cell ───────────────────────────────────────────────

def test_delete_clears_current_cell_only():
    t = _table()
    t.setCurrentCell(0, 1)
    _key(t, Qt.Key.Key_Delete)
    assert t.item(0, 1).text() == ""
    assert t.item(0, 0).text() != ""     # the rest of the row is untouched


def test_delete_with_no_current_cell_is_ignored():
    t = _table()
    t.setCurrentCell(-1, -1)
    _key(t, Qt.Key.Key_Delete)           # no current cell → no-op, no crash
    assert t.item(0, 0).text() != ""


# ── add / remove rows (unchanged fallbacks) ──────────────────────────────────────

def test_add_blank_row_lands_on_new_row():
    t = _table([(40, -36)])
    t.add_blank_row()
    assert t.rowCount() == 2
    assert (t.currentRow(), t.currentColumn()) == (1, 0)


def test_remove_uses_current_row():
    t = _table([(40, -36), (74, -2.5)])
    t.setCurrentCell(0, 1)               # current cell is in row 0
    t.remove_selected()
    assert t.rowCount() == 1
    assert t.points(strict=True) == [{"gain_db": 74.0, "power_dbm": -2.5}]


def test_remove_without_current_drops_last_row():
    t = _table([(40, -36), (74, -2.5)])
    t.setCurrentCell(-1, -1)
    t.remove_selected()
    assert t.rowCount() == 1
    assert t.points(strict=True) == [{"gain_db": 40.0, "power_dbm": -36.0}]


# ── paste a gain/power block (Ctrl+V) ────────────────────────────────────────────

def _paste(table, text):
    QApplication.clipboard().setText(text)
    return table._paste_csv()


def test_paste_two_column_block_into_empty_grid():
    t = _CurveTable()
    assert _paste(t, "40, -36\n50, -26\n74, -2.5") is True
    assert t.numeric_points() == [(40.0, -36.0), (50.0, -26.0), (74.0, -2.5)]


def test_paste_accepts_tabs_and_skips_a_header_row():
    t = _CurveTable()
    _paste(t, "gain (dB)\tpower (dBm)\n40\t-36\n74\t-2.5")
    assert t.numeric_points() == [(40.0, -36.0), (74.0, -2.5)]


def test_paste_at_current_cell_overwrites_downward():
    t = _table([(40, -36), (50, -26), (60, -16)])
    t.setCurrentCell(1, 0)                       # anchor on the 2nd row
    _paste(t, "55, -21\n65, -11")
    # rows 1..2 overwritten; row 0 kept
    assert t.numeric_points() == [(40.0, -36.0), (55.0, -21.0), (65.0, -11.0)]


def test_paste_extends_rows_when_block_runs_past_the_end():
    t = _table([(40, -36)])
    t.setCurrentCell(0, 0)
    _paste(t, "40, -36\n50, -26\n60, -16")
    assert t.numeric_points() == [(40.0, -36.0), (50.0, -26.0), (60.0, -16.0)]


def test_single_column_paste_fills_the_focused_column():
    t = _table([(40, -36), (50, -26)])
    t.setCurrentCell(0, 1)                       # power column
    _paste(t, "-35\n-25")
    assert t.numeric_points() == [(40.0, -35.0), (50.0, -25.0)]


def test_paste_with_no_current_cell_appends_after_stripping_blanks():
    t = _table([(40, -36)])
    t.add_blank_row()                            # a trailing empty row
    t.setCurrentCell(-1, -1)                     # nothing focused
    _paste(t, "50, -26\n60, -16")
    assert t.numeric_points() == [(40.0, -36.0), (50.0, -26.0), (60.0, -16.0)]


def test_paste_is_undoable():
    t = _table([(40, -36)])
    t.setCurrentCell(-1, -1)
    _paste(t, "50, -26")
    assert (50.0, -26.0) in t.numeric_points()
    t.undo()
    assert t.numeric_points() == [(40.0, -36.0)]


# ── undo / redo ──────────────────────────────────────────────────────────────────

def test_undo_redo_a_cell_edit():
    t = _table([(40, -36), (74, -2.5)])
    t.item(0, 0).setText("41")           # an edit → recorded in history
    assert t.item(0, 0).text() == "41"
    t.undo()
    assert t.item(0, 0).text() == "40"
    t.redo()
    assert t.item(0, 0).text() == "41"


def test_undo_removes_an_added_row_redo_restores_it():
    t = _table([(40, -36)])
    t.add_blank_row()
    assert t.rowCount() == 2
    t.undo()
    assert t.rowCount() == 1
    t.redo()
    assert t.rowCount() == 2


def test_undo_stops_at_loaded_baseline():
    t = _table([(40, -36), (74, -2.5)])
    # No edits yet → undo does nothing (never reverts the loaded points).
    t.undo()
    assert t.points(strict=True) == [{"gain_db": 40.0, "power_dbm": -36.0},
                                     {"gain_db": 74.0, "power_dbm": -2.5}]


def test_new_edit_after_undo_drops_the_redo_branch():
    t = _table([(40, -36)])
    t.item(0, 0).setText("41")
    t.item(0, 0).setText("42")
    t.undo()                              # back to 41
    assert t.item(0, 0).text() == "41"
    t.item(0, 0).setText("99")           # a fresh edit
    t.redo()                             # nothing to redo — 99 stands
    assert t.item(0, 0).text() == "99"


def test_ctrl_z_and_ctrl_y_shortcuts():
    t = _table([(40, -36)])
    t.item(0, 0).setText("41")
    _key(t, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)   # Ctrl+Z → undo
    assert t.item(0, 0).text() == "40"
    _key(t, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)   # Ctrl+Y → redo
    assert t.item(0, 0).text() == "41"


def test_undo_restores_a_deleted_cell():
    t = _table([(40, -36)])
    t.setCurrentCell(0, 1)
    _key(t, Qt.Key.Key_Delete)
    assert t.item(0, 1).text() == ""
    t.undo()
    assert t.item(0, 1).text() == "-36"
