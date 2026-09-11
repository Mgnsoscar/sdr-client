"""Step-to-step anchoring (Phase 1) — authoring surfaces.

The StepEditorDialog (tune/run) and RampEditorDialog offer an "after another step…" anchor
when an eligible target exists (and no Hold — they're mutually exclusive in Phase 1); saving
sets anchor="step" + anchor_step_id (assigning the target a stable id) + anchor_edge, and a
negative offset is refused.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import StepEditorDialog
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _tune(off, sid="", gain=None, task="chirp"):
    return tlm.RunItem(task_name=task, action="tune", anchor="start", offset=off,
                       params={"bw": 12}, step_id=sid)


def _hold(off=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=off)


# ── StepEditorDialog ───────────────────────────────────────────────────────────

def test_step_editor_offers_after_step_when_a_target_exists():
    target = _tune(5.0, sid="tgt")
    items = [_bar(), target]
    src = _tune(20.0)
    dlg = StepEditorDialog(src, _chirp_editor(items + [src]), new=True)
    _app.processEvents()
    assert dlg._anchor.findData("step") >= 0
    # the target is listed
    assert dlg._anchor_target.count() >= 1


def test_step_editor_hides_after_step_with_a_hold():
    target = _tune(5.0, sid="tgt")
    src = _tune(20.0)
    dlg = StepEditorDialog(src, _chirp_editor([_bar(), target, _hold(200.0), src]), new=True)
    _app.processEvents()
    assert dlg._anchor.findData("step") < 0        # mutually exclusive with a Hold (Phase 1)


def test_step_editor_resolve_step_anchor_assigns_target_id():
    target = _tune(5.0)                              # no id yet
    src = _tune(20.0)
    dlg = StepEditorDialog(src, _chirp_editor([_bar(), target, src]), new=True)
    _app.processEvents()
    # pick the target + edge
    dlg._anchor_target.setCurrentIndex(0)
    dlg._anchor_edge.setCurrentIndex(dlg._anchor_edge.findData("end"))
    sa = dlg._resolve_step_anchor("step", 3.0)
    assert sa and sa["anchor_edge"] == "end"
    assert sa["anchor_step_id"] and target.step_id == sa["anchor_step_id"]  # id assigned in place


def test_step_editor_refuses_negative_offset():
    target = _tune(5.0, sid="tgt")
    src = _tune(20.0)
    dlg = StepEditorDialog(src, _chirp_editor([_bar(), target, src]), new=True)
    _app.processEvents()
    dlg._anchor_target.setCurrentIndex(0)
    assert dlg._resolve_step_anchor("step", -2.0) is False
    assert dlg._resolve_step_anchor("start", 0.0) == {}   # non-step anchors pass through


# ── RampEditorDialog ────────────────────────────────────────────────────────────

def _ramp_dlg(items, src=None):
    src = src or tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=10.0, ramp={})
    dlg = RampEditorDialog(src, _chirp_editor(items + [src]), new=True)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    return dlg


def test_ramp_editor_offers_after_step_when_a_target_exists():
    dlg = _ramp_dlg([_bar(), _tune(5.0, sid="tgt")])
    assert dlg._anchor.findData("step") >= 0


def test_ramp_editor_hides_after_step_with_a_hold():
    dlg = _ramp_dlg([_bar(), _tune(5.0, sid="tgt"), _hold(200.0)])
    assert dlg._anchor.findData("step") < 0


def test_ramp_editor_target_rows_show_only_for_step():
    dlg = _ramp_dlg([_bar(), _tune(5.0, sid="tgt")])
    si = dlg._anchor.findData("step")
    dlg._anchor.setCurrentIndex(si)
    _app.processEvents()
    assert dlg._target_row.isVisibleTo(dlg) or dlg._target_row.isVisible()
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("start"))
    _app.processEvents()
    assert not dlg._target_row.isVisible()
