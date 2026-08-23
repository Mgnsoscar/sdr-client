"""Offscreen widget tests for the unit-detail Tasks panel: alphanumeric ordering
and the search box — the same affordances the Library offers, brought to the unit's
drill-in view. A fake hub just carries the task_done signal the panel connects to."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui.unit_detail import _TasksPanel, _TaskRow

_app = QApplication.instance() or QApplication([])


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)


def _task(name, state=m.ProcessState.STOPPED, description=""):
    return m.ProcessStatus(name=name, description=description, state=state)


def _row_names(panel):
    """Names of the task rows currently laid out, top to bottom."""
    out = []
    for i in range(panel._list.count()):
        w = panel._list.itemAt(i).widget()
        if isinstance(w, _TaskRow):
            out.append(w.task_name)
    return out


def test_tasks_sorted_alphanumerically():
    p = _TasksPanel("u", FakeHub())
    p.update_tasks([_task("task10"), _task("task2"), _task("Alpha"), _task("task1")])
    # digit-aware, case-insensitive: Alpha, task1, task2, task10 (not task1, task10, task2)
    assert _row_names(p) == ["Alpha", "task1", "task2", "task10"]


def test_search_filters_by_name():
    p = _TasksPanel("u", FakeHub())
    p.update_tasks([_task("beacon"), _task("beamer"), _task("sweep")])
    assert _row_names(p) == ["beacon", "beamer", "sweep"]

    p._search.setText("bea")             # beacon + beamer, still sorted
    assert _row_names(p) == ["beacon", "beamer"]
    assert "2 task(s) match · 3 total" in p._status.text()

    p._search.setText("ee")              # only sweep
    assert _row_names(p) == ["sweep"]

    p._search.setText("")                # cleared → everything back, sorted
    assert _row_names(p) == ["beacon", "beamer", "sweep"]
    assert "3 task(s)" in p._status.text()


def test_search_matches_description():
    p = _TasksPanel("u", FakeHub())
    p.update_tasks([_task("alpha", description="wideband noise"),
                    _task("bravo", description="carrier tone")])
    p._search.setText("noise")           # matches alpha's description only
    assert _row_names(p) == ["alpha"]
    assert "1 task(s) match · 2 total" in p._status.text()


def test_search_no_match_shows_hint_and_keeps_total():
    p = _TasksPanel("u", FakeHub())
    p.update_tasks([_task("beacon"), _task("sweep")])
    p._search.setText("nonesuch")
    assert _row_names(p) == []
    assert "2 total" in p._status.text()


def test_state_change_updates_in_place_without_reordering():
    p = _TasksPanel("u", FakeHub())
    p.update_tasks([_task("bbb"), _task("aaa")])
    assert _row_names(p) == ["aaa", "bbb"]
    row_before = p._rows["aaa"]
    # A pure state change (same names + descriptions) must not rebuild the rows — the
    # existing row objects are updated in place so live pills never flicker.
    p.update_tasks([_task("bbb"), _task("aaa", state=m.ProcessState.RUNNING)])
    assert _row_names(p) == ["aaa", "bbb"]
    assert p._rows["aaa"] is row_before
    assert p._rows["aaa"]._state == m.ProcessState.RUNNING


def test_empty_states():
    p = _TasksPanel("u", FakeHub())
    # Before any poll: neutral "not yet reached" hint, no count.
    assert _row_names(p) == []
    assert p._status.text() == ""
    # After a poll returns no tasks: the deploy hint.
    p.update_tasks([])
    assert _row_names(p) == []
    assert p._status.text() == ""
