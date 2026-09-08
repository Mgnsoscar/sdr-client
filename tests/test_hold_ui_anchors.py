"""Hold-step UI fixes: a window-B (Hold-anchored) step reads its timing from the resume
instant, and a RAMP can be anchored to the Hold (the down-ramp).

Bug 1: the ramp editor didn't offer "Hold" as an anchor (only start/stop/both), though the
runtime + canvas already resolve anchor="hold".
Bug 2: the offset sub-label for a Hold-anchored step read "… • off-air" — it should read
on-resume (0/after) or pre-hold (before).
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

import api.models as m
from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import _timing_text, StepEditorDialog
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _hold(offset=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=offset)


# ── Bug 2: the timing sub-label of a Hold-anchored step ──────────────────────

def test_timing_text_hold_side():
    # On/after the resume instant → on-resume; before it → pre-hold. Never "off-air".
    assert _timing_text(0.0, "hold", with_side=True) == "on-resume"
    assert _timing_text(0.0, "hold", with_side=False) == "on-resume"
    assert _timing_text(5.0, "hold", with_side=True).endswith("· on-resume")
    assert _timing_text(5.0, "hold", with_side=True) != "on-resume"
    assert _timing_text(-5.0, "hold", with_side=True).endswith("· pre-hold")
    # The compact (canvas) form is just the signed offset, no misleading edge word.
    compact = _timing_text(5.0, "hold", with_side=False)
    assert "off-air" not in compact and "resume" not in compact


def test_timing_text_start_stop_unchanged():
    assert _timing_text(0.0, "start", with_side=True) == "on-air"
    assert _timing_text(0.0, "stop", with_side=True) == "off-air"
    assert _timing_text(10.0, "start", with_side=True).endswith("· on-air")


# ── Bug 1: the ramp editor offers a Hold anchor when a Hold exists ───────────

def _ramp_dlg(items, src_anchor="start", offset=10.0):
    ed = _chirp_editor(items)
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor=src_anchor,
                      offset=offset, ramp={})
    dlg = RampEditorDialog(src, ed, new=True)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    return dlg


def test_ramp_anchor_offers_hold_only_when_a_hold_exists():
    with_hold = _ramp_dlg([_bar(10), _hold(300.0)])
    assert with_hold._anchor.findData("hold") >= 0

    without = _ramp_dlg([_bar(10)])
    assert without._anchor.findData("hold") < 0


def test_ramp_anchor_offers_hold_when_editing_an_existing_hold_ramp():
    # Even with no Hold marker present (defensive), editing a ramp already anchored to
    # the hold keeps the option so it round-trips instead of silently resetting.
    dlg = _ramp_dlg([_bar(10)], src_anchor="hold")
    assert dlg._anchor.findData("hold") >= 0
    assert dlg._anchor.currentData() == "hold"


def test_selecting_hold_anchor_updates_rows_and_sublabels():
    dlg = _ramp_dlg([_bar(10), _hold(300.0)])
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("hold"))
    _app.processEvents()
    # Single-anchor (forward from resume): the end-offset row is hidden, the offset
    # label names the Hold, and the From sub-label is measured from the resume instant.
    assert not dlg._offend_row.isVisible()
    assert "Hold" in dlg._off_lbl.text()
    frm, to = dlg._ft_sublabels()
    assert "on-resume" in frm
    assert to == "ramp end"


def test_hold_anchored_ramp_round_trips():
    ramp = {"param": "gain", "start": 40, "stop": 20, "steps": 2, "hold_s": 0.5, "mode": "tune"}
    item = tlm.RunItem(task_name="chirp", action="ramp", anchor="hold", offset=0.0, ramp=ramp)
    steps = tlm.items_to_steps([item])
    assert steps[0]["anchor"] == "hold" and steps[0]["action"] == "ramp"
    back = tlm.steps_to_items(steps)
    r = next(it for it in back if getattr(it, "action", None) == "ramp")
    assert r.anchor == "hold" and dict(r.ramp)["param"] == "gain"


# ── Bug 1 (duration tasks): a bar's START can anchor to the Hold (window-B task) ──

def test_window_b_bar_round_trips_and_places_at_the_hold():
    bar = tlm.BarItem(task_name="mon", start_offset=0.0, stop_offset=0.0, start_anchor="hold")
    steps = tlm.items_to_steps([bar, _hold(300.0)])
    start = next(s for s in steps if s["action"] == "start")
    assert start["anchor"] == "hold"                       # START rides the Hold
    assert next(s for s in steps if s["action"] == "stop")["anchor"] == "stop"  # STOP stays off-air
    back = tlm.steps_to_items(steps)
    b = next(it for it in back if getattr(it, "kind", None) == "bar")
    assert b.start_anchor == "hold"
    # Geometry: the start end is placed at the Hold divider (hold_offset + its offset).
    assert tlm.bar_start_placement(b, 300.0) == ("start", 300.0)
    assert tlm.bar_start_placement(tlm.BarItem(task_name="x", start_offset=5.0), 300.0) == ("start", 5.0)


def test_window_b_bar_collapses_for_the_schedule():
    steps = tlm.items_to_steps([tlm.BarItem(task_name="mon", start_offset=0.0, stop_offset=0.0,
                                            start_anchor="hold"), _hold(300.0)])
    ss = [m.SequenceStep(anchor=s["anchor"], offset_s=s["offset_s"],
                         action=m.StepAction(s["action"]), task_name=s["task_name"],
                         args=list(s.get("args") or [])) for s in steps]
    collapsed = m.collapse_hold(ss)
    assert not m.has_hold(collapsed)                        # marker compiled out
    start = next(s for s in collapsed if s.action == m.StepAction.START)
    assert start.anchor == "start" and start.offset_s == 300.0   # re-anchored on-air at the hold


def test_step_editor_bar_offers_hold_start_anchor_only_with_a_hold():
    with_hold = StepEditorDialog(_bar(10), _chirp_editor([_bar(10), _hold(300.0)]), new=False)
    _app.processEvents()
    assert with_hold._start_anchor.findData("hold") >= 0
    assert not with_hold._start_anchor.isHidden()          # row shown for a bar + hold

    without = StepEditorDialog(_bar(10), _chirp_editor([_bar(10)]), new=False)
    _app.processEvents()
    assert without._start_anchor.findData("hold") < 0
    assert without._start_anchor.isHidden()                # single option → row hidden


def test_step_editor_saves_hold_start_anchor():
    dlg = StepEditorDialog(_bar(10), _chirp_editor([_bar(10), _hold(300.0)]), new=False)
    _app.processEvents()
    dlg._start_anchor.setCurrentIndex(dlg._start_anchor.findData("hold"))
    _app.processEvents()
    assert "Hold" in dlg._start_off._row_label.text()      # start-offset row relabelled
    dlg._accept()
    assert getattr(dlg, "result_item", None) is not None, "form should validate a chirp bar"
    assert dlg.result_item.kind == "bar" and dlg.result_item.start_anchor == "hold"
