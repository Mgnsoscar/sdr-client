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
from ui.timeline_editor import StepEditorDialog, LANE_H
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


def test_editor_steps_roundtrip_preserves_step_anchor():
    """TimelineEditor.steps() (deploy) and set_steps() (load) must carry id/anchor_step_id/
    anchor_edge — the hand-built dict round-trip that dropped them (the arm "needs anchor_step_id"
    bug + the re-edit wrong-target bug)."""
    up = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=2.0,
                     step_id="up", ramp={"param": "power", "start": -90.0, "stop": -50.0,
                                         "steps": 3, "duration_s": 6.0})
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=1.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})
    ed = _chirp_editor([_bar(), up, down])
    steps = ed.steps()
    # the up-ramp carries its id; the down-ramp carries the anchor target + edge (not empty!)
    assert any(getattr(s, "id", "") == "up" for s in steps)
    dn = next(s for s in steps if s.anchor == "step")
    assert dn.anchor_step_id == "up" and dn.anchor_edge == "end"
    # load them back: the down-ramp item keeps its anchor (re-edit shows the right target)
    ed.set_steps(steps)
    items = ed.items()
    dn_item = next(it for it in items if getattr(it, "anchor", "") == "step")
    assert dn_item.anchor_step_id == "up" and dn_item.anchor_edge == "end"
    up_item = next(it for it in items if getattr(it, "step_id", "") == "up")
    assert up_item is not None


def test_ramp_editor_target_rows_show_only_for_step():
    dlg = _ramp_dlg([_bar(), _tune(5.0, sid="tgt")])
    si = dlg._anchor.findData("step")
    dlg._anchor.setCurrentIndex(si)
    _app.processEvents()
    assert dlg._target_row.isVisibleTo(dlg) or dlg._target_row.isVisible()
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("start"))
    _app.processEvents()
    assert not dlg._target_row.isVisible()


# ── Canvas: click-to-select, drag-to-anchor, remove-anchor (100% UI) ────────────

def _ramp_item(off, sid="", task="chirp"):
    return tlm.RunItem(task_name=task, action="ramp", anchor="start", offset=off, step_id=sid,
                       ramp={"param": "power", "start": -90.0, "stop": -50.0,
                             "steps": 3, "duration_s": 6.0})


def test_canvas_make_anchor_connects_two_steps():
    """A drag-to-anchor drop sets anchor="step" + target id + edge + a >= 0 offset, and the
    target is assigned a stable id — no dialog."""
    up = _ramp_item(0.0)                              # start 0, end 6 (no id yet)
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    cv._make_anchor(later.uid, up, "end")
    assert later.anchor == "step"
    assert up.step_id and later.anchor_step_id == up.step_id     # id assigned in place
    assert later.anchor_edge == "end"
    assert later.offset == 14.0                       # 20 - 6, kept in place


def test_canvas_make_anchor_rejects_ineligible_drop():
    up = _ramp_item(0.0, sid="up")
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    # a bar isn't a Phase-1 target → no anchor created
    bar = next(it for it in cv._items if it.kind == "bar")
    cv._make_anchor(later.uid, bar, "end")
    assert later.anchor == "start" and not later.anchor_step_id


def test_canvas_detach_anchor_reverts_to_start_in_place():
    up = _ramp_item(0.0, sid="up")                   # end 6
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=3.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), up, down])._canvas
    cv._detach_anchor(down.uid)
    assert down.anchor == "start"
    assert not down.anchor_step_id and down.anchor_edge == "end"
    assert down.offset == 9.0                         # resolved base (6 + 3) — stays put


def test_canvas_selection_records_the_remove_chip_only_when_anchored():
    up = _ramp_item(0.0, sid="up")
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=3.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), up, down])._canvas
    cv._selected = down.uid                           # a step-anchored item → chip appears
    cv.grab()                                         # force a paint
    assert cv._rmchip is not None
    cv._selected = up.uid                             # a plain item → no chip
    cv.grab()
    assert cv._rmchip is None


def test_canvas_edge_at_finds_handles_and_drop_target_respects_eligibility():
    up = _ramp_item(0.0, sid="up")                   # a ramp: start + end handles
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    g = cv._geom[up.uid]; cy = g["y"] + LANE_H / 2
    assert cv._edge_at(g["start_x"], cy) == (up, "start")
    assert cv._edge_at(g["stop_x"], cy) == (up, "end")
    # a drag from `later` can drop on the ramp's end (eligible)…
    tgt = cv._drop_target(g["stop_x"], cy, later.uid)
    assert tgt is not None and tgt[0] is up and tgt[1] == "end"
    # …but not on itself
    gl = cv._geom[later.uid]
    assert cv._drop_target(gl["cx"], gl["y"] + LANE_H / 2, later.uid) is None


# ── Context menu (Edit · Duplicate · Remove anchor · Delete) ────────────────────

def test_context_menu_spec_varies_by_item():
    up = _ramp_item(0.0, sid="up")
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=3.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), up, down, _hold(400.0)])._canvas
    # an anchored item gets Remove anchor; a plain item doesn't; a Hold has no Duplicate
    assert cv._context_menu_spec(down) == ["Edit…", "Duplicate", "Remove anchor", "—", "Delete"]
    assert "Remove anchor" not in cv._context_menu_spec(up)
    hold = next(it for it in cv._items if tlm._is_hold(it))
    hspec = cv._context_menu_spec(hold)
    assert "Duplicate" not in hspec and "Remove anchor" not in hspec
    assert hspec[0] == "Edit…" and hspec[-1] == "Delete"


def test_canvas_duplicate_clones_with_fresh_uid_and_no_step_id():
    orig = _tune(20.0, sid="tgt")
    cv = _chirp_editor([_bar(), orig])._canvas
    cv._duplicate_item(orig)
    tunes = [it for it in cv._items if getattr(it, "action", "") == "tune"]
    assert len(tunes) == 2
    clone = next(it for it in tunes if it.uid != orig.uid)
    assert clone.step_id == ""                       # the copy is not a reference target
    assert clone.offset == 35.0                      # nudged 20 + 15
    assert cv._selected == clone.uid


def test_canvas_delete_reanchors_dependents_to_on_air():
    up = _ramp_item(0.0, sid="up")                   # start 0, end 6
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=3.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})       # fires at 6 + 3 = 9
    cv = _chirp_editor([_bar(), up, down])._canvas
    cv._delete_with_reanchor(up.uid)
    assert all(it.uid != up.uid for it in cv._items)   # up is gone
    assert down.anchor == "start" and not down.anchor_step_id
    assert down.offset == 9.0                          # kept at its would-be fire time


def test_canvas_delete_plain_item_just_removes_it():
    up = _ramp_item(0.0, sid="up")
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    n = len(cv._items)
    cv._delete_with_reanchor(later.uid)
    assert len(cv._items) == n - 1 and all(it.uid != later.uid for it in cv._items)


# ── Undo / redo ─────────────────────────────────────────────────────────────────

def _down_ramp(ref="up", off=3.0):
    return tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=off,
                       anchor_step_id=ref, anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})


def test_undo_redo_add_and_remove():
    cv = _chirp_editor([_bar()])._canvas
    t = _tune(20.0)
    cv.add_item(t)
    assert any(it.uid == t.uid for it in cv._items)
    cv.undo()
    assert all(it.uid != t.uid for it in cv._items)   # add undone
    cv.redo()
    assert any(it.uid == t.uid for it in cv._items)    # add redone


def test_undo_reverts_a_drag_to_anchor():
    up = _ramp_item(0.0, sid="up")
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    cv._make_anchor(later.uid, up, "end")
    assert next(it for it in cv._items if it.uid == later.uid).anchor == "step"
    cv.undo()
    l2 = next(it for it in cv._items if it.uid == later.uid)
    assert l2.anchor == "start" and not l2.anchor_step_id


def test_undo_restores_a_deleted_target_and_its_dependents():
    up = _ramp_item(0.0, sid="up")
    down = _down_ramp()
    cv = _chirp_editor([_bar(), up, down])._canvas
    cv._delete_with_reanchor(up.uid)
    assert next(it for it in cv._items if it.uid == down.uid).anchor == "start"
    cv.undo()
    assert any(it.uid == up.uid for it in cv._items)   # target restored
    d2 = next(it for it in cv._items if it.uid == down.uid)
    assert d2.anchor == "step" and d2.anchor_step_id == "up"   # dependency restored


def test_set_items_resets_undo_history():
    cv = _chirp_editor([_bar()])._canvas
    cv.add_item(_tune(5.0))
    assert cv.can_undo()
    cv.set_items([_bar()])
    assert not cv.can_undo() and not cv.can_redo()


# ── Hover tooltip text ──────────────────────────────────────────────────────────

def test_tooltip_text_describes_anchor_and_bar():
    up = _ramp_item(0.0, sid="up")
    down = _down_ramp()
    cv = _chirp_editor([_bar(), up, down])._canvas
    txt = cv._tooltip_text(down)
    assert "after" in txt and "chirp" in txt and "end" in txt       # names its anchor
    bar = next(it for it in cv._items if it.kind == "bar")
    btxt = cv._tooltip_text(bar)
    assert "starts" in btxt and "stops" in btxt
