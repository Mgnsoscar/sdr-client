"""The plan editor PAINTS — the stage, the embedded sequence canvases, the header column and the
whole dialog (the two entry points that once crashed: "New plan" / "Edit").

A paint-time exception is captured through sys.excepthook (which PyQt calls instead of aborting
when a custom hook is installed) and failed on, so a signature drift between the plan stage and
the sequence canvas it embeds shows up here instead of in the field.
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

    def list_tasks(self):
        return [type("T", (), {"name": "tx"})()]

    def get_tasks_yaml(self):
        return "tasks:\n- name: tx\n  command: [python3, tx.py]\n"


class _Unit:
    unit_type = m.DEFAULT_UNIT_TYPE

    def __init__(self, host):
        self.label = host.upper()


class _Fleet:
    """What the plan editor reads: the configured hostnames, the library's sequences/tasks and
    each unit's type + label (no unit needs to be reachable — the plan editor is local, offline
    authoring)."""

    def __init__(self, hosts=("a", "b")):
        self._hosts = list(hosts)

    def hostnames(self):
        return list(self._hosts)

    def get(self, host):
        if host == LIBRARY_HOST:
            return _Library()
        if host in self._hosts:
            return _Unit(host)
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
        m.SequenceStep(id="st", anchor="start", offset_s=-1, action=m.StepAction.START, task_name="tx",
                       args=["--freq", "1575.42", "--power", "-30"], replace_args=True),
        m.SequenceStep(id="st~stop", anchor="stop", offset_s=1, action=m.StepAction.STOP, task_name="tx"),
        m.SequenceStep(id="up", anchor="start", offset_s=0, action=m.StepAction.RAMP, task_name="tx",
                       ramp=m.RampSpec(param="power", start=-90, stop=-50, steps=4, duration_s=120)),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.TUNE, task_name="tx", params={"rf": "off"}),
    ]


def _item(host, seq_id, name, on=0.0, off=0.0, **kw):
    base = dict(id=f"pi-{seq_id}", hostname=host, unit_label=host.upper(), sequence_id=seq_id,
                sequence_name=name, steps=_steps(), on_air_offset_s=on, off_air_offset_s=off)
    base.update(kw)
    return m.PlanItem(**base)


def _editor(items):
    seqs = {"a": [m.Sequence(id="s1", name="PRN", steps=_steps())],
            "b": [m.Sequence(id="s2", name="Chirp", steps=_steps())]}
    ed = pe.PlanTimelineEditor(_Hub(), seqs)
    ed.set_sequences(seqs)
    ed.set_items(items)
    ed.resize(1200, 600)
    ed.show()
    _app.processEvents()
    return ed


def _render(widget, errors):
    widget.render(QPixmap(max(1200, widget.width()), max(380, widget.height())))
    _app.processEvents()
    assert not errors, "paint raised:\n" + "\n".join(errors)


def test_an_empty_plan_stage_paints(paint_errors):
    # "New plan": no sequences yet — the unit bands, the plan anchors and the axis alone must paint.
    ed = _editor([])
    _render(ed._stage, paint_errors)
    _render(ed._header, paint_errors)


def test_a_plan_with_placed_sequences_paints_expanded_collapsed_and_folded(paint_errors):
    # "Edit" an existing plan: an expanded sequence (its embedded canvas + row header), a collapsed
    # pill anchored to it, and a folded unit.
    ed = _editor([_item("a", "s1", "PRN", on=5.0, off=-10.0),
                  _item("b", "s2", "Chirp", on=20.0, off=0.0, expanded=False,
                        on_air_anchor="item", on_air_anchor_item="pi-s1", on_air_anchor_edge="off")])
    st = ed._stage
    n1, n2 = st.nodes()
    assert n1.expanded and n1.canvas.isVisible() and not n2.canvas.isVisible()
    assert n1.on_x < n1.off_x and n2.on_x == pytest.approx(n1.off_x + 20.0 * st.eff())
    _render(st, paint_errors)
    _render(n1.canvas, paint_errors)
    _render(n1.editor._rowhdr_e, paint_errors)
    _render(ed._header, paint_errors)
    st.toggle_folded("a")
    assert any(r["type"] == "ulane" for r in st.rows())
    _render(st, paint_errors)
    _render(ed, paint_errors)
    # …and the round-trip keeps the placements + anchors.
    got = {it.sequence_id: (it.on_air_offset_s, it.off_air_offset_s, it.on_air_anchor) for it in ed.items()}
    assert got == {"s1": (5.0, -10.0, "plan"), "s2": (20.0, 0.0, "item")}


def test_the_whole_plan_editor_paints_new_and_existing(paint_errors):
    hub = _Hub()
    new = pe.PlanEditorDialog(hub, plan=None, parent=None)
    try:
        new.resize(1200, 760)
        _render(new, paint_errors)
        assert "2 unit(s)" in new._status.text() and "2 library sequence(s)" in new._status.text()
    finally:
        new.deleteLater()
    plan = m.Plan(id="p1", name="Field day", items=[_item("a", "s1", "PRN", on=5.0, off=-10.0)])
    edit = pe.PlanEditorDialog(hub, plan=plan, parent=None)
    try:
        edit.resize(1200, 760)
        _render(edit, paint_errors)
        assert edit._timeline.items() and edit._timeline.items()[0].sequence_id == "s1"
        assert edit._timeline.validate() is None
    finally:
        edit.deleteLater()
