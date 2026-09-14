"""The plan timeline canvas (ui/plan_editor._PlanCanvas) PAINTS.

It subclasses the sequence canvas and overrides paintEvent, calling the base's paint helpers. The
sequence-editor redesign changed those helpers' signatures (_paint_anchor gained a `top`), and
nothing rendered the plan canvas in the suite — so "New plan" / "Edit" crashed the app with a
TypeError inside paintEvent (PyQt aborts on an unhandled exception in a virtual override).

These tests render the plan canvas for real. A paint-time exception is captured through
sys.excepthook (which PyQt calls instead of aborting when a custom hook is installed) and failed
on, so any future drift between the two canvases shows up here instead of in the field.
"""
import os
import sys
import traceback

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from api import models as m
from api.fleet import LIBRARY_HOST
from ui import plan_editor as pe

_app = QApplication.instance() or QApplication([])

_LIB_SEQS = [m.Sequence(id="s1", name="PRN", steps=[]), m.Sequence(id="s2", name="Chirp", steps=[])]


class _Library:
    def list_sequences(self):
        return list(_LIB_SEQS)


class _Unit:
    unit_type = m.DEFAULT_UNIT_TYPE


class _Fleet:
    """What PlanEditorDialog reads: the configured hostnames, the library's sequences, and each
    unit's type (no unit needs to be reachable — the plan editor is a local, offline authoring)."""

    def __init__(self, hosts=("a", "b")):
        self._hosts = list(hosts)

    def hostnames(self):
        return list(self._hosts)

    def get(self, host):
        if host == LIBRARY_HOST:
            return _Library()
        if host in self._hosts:
            return _Unit()
        raise KeyError(host)


class _Hub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.fleet = _Fleet()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:                             # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


@pytest.fixture
def paint_errors():
    """Collect exceptions raised inside Qt virtual overrides (paintEvent…) during a test — as
    TEXT only: holding the traceback would keep the failed paintEvent's frame (and its still-active
    QPainter) alive, which is fatal for the next render."""
    caught = []
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        caught.append("".join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = hook
    try:
        yield caught
    finally:
        sys.excepthook = prev


def _steps():
    return [
        m.SequenceStep(anchor="start", offset_s=-1, action=m.StepAction.START, task_name="tx",
                       args=["--freq", "1575.42", "--power", "-30"], replace_args=True),
        m.SequenceStep(anchor="stop", offset_s=1, action=m.StepAction.STOP, task_name="tx"),
    ]


def _item(host, seq_id, name, on=0.0, off=0.0):
    return m.PlanItem(hostname=host, unit_label=host.upper(), sequence_id=seq_id,
                      sequence_name=name, steps=_steps(), on_air_offset_s=on, off_air_offset_s=off)


def _editor(items):
    seqs = {"a": [m.Sequence(id="s1", name="PRN", steps=_steps())],
            "b": [m.Sequence(id="s2", name="Chirp", steps=_steps())]}
    ed = pe.PlanTimelineEditor(_Hub(), seqs)
    ed.set_items(items)
    ed.resize(1100, 320)
    _app.processEvents()
    return ed


def _render(widget, errors):
    widget.render(QPixmap(max(1200, widget.width()), max(380, widget.height())))
    _app.processEvents()
    assert not errors, "paint raised:\n" + "\n".join(errors)


def test_an_empty_plan_canvas_paints(paint_errors):
    # "New plan": no items yet — the anchors + window ticks alone must paint.
    ed = _editor([])
    _render(ed._canvas, paint_errors)


def test_a_plan_with_placed_sequences_paints(paint_errors):
    # "Edit" an existing plan: two sequence bars inside the on-air window.
    ed = _editor([_item("a", "s1", "PRN", on=5.0, off=-10.0),
                  _item("b", "s2", "Chirp", on=20.0, off=0.0)])
    cv = ed._canvas
    _render(cv, paint_errors)
    bars = [b for b in cv.items() if b.kind == "bar"]
    assert len(bars) == 2 and all(b.uid in cv._geom for b in bars)
    g = cv._geom[bars[0].uid]
    assert g["start_x"] < g["stop_x"]                     # the bar spans left→right in the window
    # …and the round-trip through the canvas keeps the placements.
    got = {it.sequence_id: (it.on_air_offset_s, it.off_air_offset_s) for it in ed.items()}
    assert got == {"s1": (5.0, -10.0), "s2": (20.0, 0.0)}


def test_the_whole_plan_editor_paints_new_and_existing(paint_errors):
    # The dialogs themselves — the two entry points that crashed ("New plan", "Edit").
    hub = _Hub()
    new = pe.PlanEditorDialog(hub, plan=None, parent=None)
    try:
        new.resize(1100, 700)
        _render(new, paint_errors)
        assert "2 unit(s)" in new._status.text() and "2 library sequence(s)" in new._status.text()
    finally:
        new.deleteLater()
    plan = m.Plan(id="p1", name="Field day", items=[_item("a", "s1", "PRN", on=5.0, off=-10.0)])
    edit = pe.PlanEditorDialog(hub, plan=plan, parent=None)
    try:
        edit.resize(1100, 700)
        _render(edit, paint_errors)
        assert edit._timeline.items() and edit._timeline.items()[0].sequence_id == "s1"
    finally:
        edit.deleteLater()
