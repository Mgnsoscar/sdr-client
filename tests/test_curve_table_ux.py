"""Usability of the calibration curve grid (_CurveTable): single-click editing,
deselect (Esc / click-away / empty-area), Del-to-clear, cell-level selection and
row removal. Offscreen widget tests driving the overridden event handlers."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QAbstractItemView, QApplication

from ui.calibration_panel import _CurveTable

_app = QApplication.instance() or QApplication([])


def _table(points=((40, -36), (74, -2.5))):
    t = _CurveTable()
    t.set_points([{"gain_db": g, "power_dbm": p} for g, p in points])
    return t


def _key(table, key):
    table.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


# ── configuration ──────────────────────────────────────────────────────────────

def test_cell_selection_and_single_click_triggers():
    t = _CurveTable()
    assert t.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems
    trig = t.editTriggers()
    # type-to-edit and click-a-selected-cell-to-edit are both on (no double-click needed)
    assert trig & QAbstractItemView.EditTrigger.AnyKeyPressed
    assert trig & QAbstractItemView.EditTrigger.SelectedClicked


# ── deselect ────────────────────────────────────────────────────────────────────

def test_deselect_clears_selection_and_current():
    t = _table()
    t.item(0, 0).setSelected(True)
    t.setCurrentCell(0, 0)
    assert t.selectedItems()
    t._deselect()
    assert t.selectedItems() == []
    assert t.currentRow() == -1


def test_escape_deselects():
    t = _table()
    t.item(0, 0).setSelected(True)
    _key(t, Qt.Key.Key_Escape)
    assert t.selectedItems() == []


def test_focus_out_deselects_when_not_editing():
    t = _table()
    t.item(0, 0).setSelected(True)
    t.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    assert t.selectedItems() == []


def test_click_on_empty_area_deselects():
    t = _table()
    t.resize(240, 200)
    t.item(0, 0).setSelected(True)
    # A point well below the two rows is empty space → invalid index → deselect.
    pos = QPointF(10, 190)
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    t.mousePressEvent(ev)
    assert t.selectedItems() == []


# ── single-click editing ─────────────────────────────────────────────────────────

def test_single_click_opens_the_editor():
    t = _table()
    t.resize(240, 120)
    rect = t.visualItemRect(t.item(0, 0))
    pos = QPointF(rect.center())
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    t.mousePressEvent(ev)
    assert t.state() == QAbstractItemView.State.EditingState


# ── delete / clear ──────────────────────────────────────────────────────────────

def test_delete_clears_selected_cell_contents():
    t = _table()
    t.item(0, 0).setSelected(True)
    t.item(0, 1).setSelected(True)
    _key(t, Qt.Key.Key_Delete)
    assert t.item(0, 0).text() == "" and t.item(0, 1).text() == ""
    # the other row is untouched
    assert t.item(1, 0).text() != ""


def test_delete_with_no_selection_is_ignored():
    t = _table()
    t._deselect()
    _key(t, Qt.Key.Key_Delete)                 # nothing selected → no-op, no crash
    assert t.item(0, 0).text() != ""


# ── add / remove rows ────────────────────────────────────────────────────────────

def test_add_blank_row_lands_on_new_row():
    t = _table([(40, -36)])
    t.add_blank_row()
    assert t.rowCount() == 2
    assert (t.currentRow(), t.currentColumn()) == (1, 0)


def test_remove_selected_removes_the_row_of_a_selected_cell():
    t = _table([(40, -36), (74, -2.5)])
    t.item(0, 1).setSelected(True)             # a single cell in row 0
    t.remove_selected()
    assert t.rowCount() == 1
    assert t.points(strict=True) == [{"gain_db": 74.0, "power_dbm": -2.5}]


def test_remove_without_selection_still_drops_last_row():
    # Preserved fallback: no selection, no current → remove the last row.
    t = _table([(40, -36), (74, -2.5)])
    t._deselect()
    t.remove_selected()
    assert t.rowCount() == 1
    assert t.points(strict=True) == [{"gain_db": 40.0, "power_dbm": -36.0}]
