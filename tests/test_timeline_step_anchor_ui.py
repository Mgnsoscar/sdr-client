"""Step-to-step anchoring (Phase 1) — authoring surfaces.

The StepEditorDialog (tune/run) and RampEditorDialog offer an "after another step…" anchor
when an eligible target exists (and no Hold — they're mutually exclusive in Phase 1); saving
sets anchor="step" + anchor_step_id (assigning the target a stable id) + anchor_edge. A NEGATIVE
offset (fire before the target's edge, like a start/stop anchor's warm-up lead-in) is allowed —
the dialogs accept it, the canvas routes such a dependent entered from the RIGHT (arrow points
left), and the save/arm gate enforces the sequence-step-anchor-negative capability.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import StepEditorDialog, LANE_H, SNAP_PX
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
    # pick the tune target (bars are eligible too now, so select by uid, not index 0) + edge
    dlg._anchor_target.setCurrentIndex(dlg._anchor_target.findData(target.uid))
    dlg._anchor_edge.setCurrentIndex(dlg._anchor_edge.findData("end"))
    sa = dlg._resolve_step_anchor("step", 3.0)
    assert sa and sa["anchor_edge"] == "end"
    assert sa["anchor_step_id"] and target.step_id == sa["anchor_step_id"]  # id assigned in place


def test_step_editor_accepts_negative_offset():
    # A negative step offset is allowed now (fire BEFORE the target's edge, like a start/stop
    # anchor's warm-up lead-in); the dialog resolves the anchor instead of refusing it.
    target = _tune(5.0, sid="tgt")
    src = _tune(20.0)
    dlg = StepEditorDialog(src, _chirp_editor([_bar(), target, src]), new=True)
    _app.processEvents()
    dlg._anchor_target.setCurrentIndex(dlg._anchor_target.findData(target.uid))
    sa = dlg._resolve_step_anchor("step", -2.0)
    assert sa and sa["anchor_step_id"] == "tgt"           # negative offset accepted
    assert dlg._resolve_step_anchor("start", 0.0) == {}   # non-step anchors pass through


def test_step_editor_bar_offers_and_saves_a_start_step_anchor():
    """#6: a duration task (bar) can hang its START off another step (only the start anchors —
    the stop stays off-air). The dialog offers 'after another step…' in the Start-anchor picker
    when an eligible target exists, and _accept persists start_anchor_step_id/edge."""
    target = _tune(5.0, sid="tgt")
    src_bar = _bar()
    dlg = StepEditorDialog(src_bar, _chirp_editor([target, src_bar]), new=False)
    _app.processEvents()
    assert dlg._start_anchor.findData("step") >= 0          # the option is offered
    dlg._start_anchor.setCurrentIndex(dlg._start_anchor.findData("step"))
    _app.processEvents()
    assert dlg._anchor_target.isVisibleTo(dlg)              # target/edge pickers revealed
    assert "the step" in dlg._start_off._row_label.text().lower()   # offset row relabelled
    dlg._anchor_target.setCurrentIndex(dlg._anchor_target.findData(target.uid))
    dlg._anchor_edge.setCurrentIndex(dlg._anchor_edge.findData("end"))
    dlg._accept()
    r = dlg.result_item
    assert r is not None and r.kind == "bar"
    assert r.start_anchor == "step" and r.start_anchor_step_id == "tgt"
    assert r.start_anchor_edge == "end"


def test_step_editor_bar_step_anchor_hidden_with_a_hold():
    # A Hold and step anchoring are mutually exclusive (Phase 1) — no 'after another step…'.
    src_bar = _bar()
    dlg = StepEditorDialog(src_bar, _chirp_editor([_tune(5.0, sid="tgt"), _hold(300.0), src_bar]),
                           new=False)
    _app.processEvents()
    assert dlg._start_anchor.findData("step") < 0


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
    up = _ramp_item(0.0)                              # start 0, end 6 (incl. final hold)
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), up, later])._canvas
    cv._make_anchor(later.uid, up, "end")
    assert later.anchor == "step"
    assert up.step_id and later.anchor_step_id == up.step_id     # id assigned in place
    assert later.anchor_edge == "end"
    assert later.offset == 14.0                       # 20 - 6, kept in place


def test_canvas_make_anchor_onto_a_bar_edge():
    """A step now anchors to a duration task's (bar's) on-air START edge — kept in place by the
    pixel gap read off the geometry (#1/#7)."""
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), later])._canvas
    bar = next(it for it in cv._items if it.kind == "bar")
    cv._make_anchor(later.uid, bar, "start")
    assert later.anchor == "step"
    assert bar.step_id and later.anchor_step_id == bar.step_id
    assert later.anchor_edge == "start"
    assert later.offset == 20.0                        # 20 - 0 (bar starts on-air), kept in place


def test_canvas_make_anchor_onto_a_bar_stop_edge_uses_the_geometry_gap():
    """Anchoring a mid-window tune to the bar's OFF-AIR stop edge crosses the clock boundary:
    the offset is the pixel gap between the tune's start and the off-air stop edge, read off the
    PRE-anchor geometry (the band reflows once the off-air window opens, so it's a semantic — not
    pixel — keep-in-place). It comes out negative — the tune fires before off-air (#12)."""
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), later])._canvas
    bar = next(it for it in cv._items if it.kind == "bar")   # stop at 600 s (off-air)
    eff = cv._eff()
    want = tlm._snap((cv._geom[later.uid]["cx"] - cv._edge_x(bar, "end")) / eff)
    cv._make_anchor(later.uid, bar, "end")
    assert later.anchor == "step" and later.anchor_edge == "end"
    assert later.offset < 0.0                           # off-air relative — fires before off-air
    assert abs(later.offset - want) < 1e-6


def test_canvas_make_anchor_rejects_self_drop():
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), later])._canvas
    cv._make_anchor(later.uid, later, "start")         # a step can't anchor to itself
    assert later.anchor == "start" and not later.anchor_step_id


def test_canvas_detach_anchor_reverts_to_start_in_place():
    up = _ramp_item(0.0, sid="up")                   # end 6 (incl. final hold)
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


def test_paint_gridlines_cover_both_windows_not_the_relative_band():
    # Three regions: an on-air ABSOLUTE window (gridlines), a truly-relative middle band (CLEAR),
    # and an off-air ABSOLUTE window opened by a stop-anchored step (gridlines). A stop-anchored
    # tune at −60 s opens the off-air window; the tune at 45 s pins the on-air window's right edge.
    stop = tlm.RunItem(task_name="chirp", action="tune", anchor="stop", offset=-60.0,
                       params={"bw": 12})
    cv = _chirp_editor([_bar(), _tune(45.0), stop])._canvas
    cv.grab()                                            # lay out geometry
    on_x, off_x = int(cv._on), int(cv._off)
    def_x = int(cv._def_x()); off_def_x = int(cv._off_def_x())
    assert def_x < off_def_x < off_x                     # a real relative band sits between them

    class _StubPainter:
        def __init__(self): self.xs = []
        def save(self): pass
        def restore(self): pass
        def setRenderHint(self, *a): pass
        def setPen(self, *a): pass
        def drawLine(self, x0, y0, x1, y1):
            assert x0 == x1                              # every gridline is vertical
            self.xs.append(x0)

    sp = _StubPainter()
    cv._paint_gridlines(sp, 10, 500, on_x, def_x, off_x, off_def_x)
    assert sp.xs
    assert any(on_x < x <= def_x + 1 for x in sp.xs)           # on-air window has gridlines
    assert any(off_def_x - 1 <= x < off_x for x in sp.xs)      # off-air window has gridlines
    # the truly-relative middle band (def_x .. off_def_x) is left clear
    assert not any(def_x + 2 < x < off_def_x - 2 for x in sp.xs)


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


def test_drop_target_finds_a_bar_start_and_stop_edge():
    """A duration task's (bar's) START dot is an anchor handle (v3 #5) while its STOP dot stays a
    resize handle; `_drop_edge_at`/`_drop_target` offer BOTH as drop targets (#1/#7)."""
    later = _tune(20.0)
    cv = _chirp_editor([_bar(), later])._canvas
    bar = next(it for it in cv._items if it.kind == "bar")
    bg = cv._geom[bar.uid]; cy = bg["y"] + LANE_H / 2
    # the bar's START dot begins a connect-drag; its STOP dot does not (off-air is its own anchor)
    assert cv._edge_at(bg["start_x"], cy) == (bar, "start")
    assert cv._edge_at(bg["stop_x"], cy) is None
    # … and a connect-drag from `later` can drop on either bar edge
    assert cv._drop_target(bg["start_x"], cy, later.uid) == (bar, "start")
    assert cv._drop_target(bg["stop_x"], cy, later.uid) == (bar, "end")


def test_drop_target_finds_an_off_air_tune():
    """An off-air-anchored tune is a valid target now, so a drag from another step can drop
    on it (the #12 asymmetry — off-air targets were previously ineligible)."""
    off = tlm.RunItem(task_name="chirp", action="tune", anchor="stop", offset=-30.0,
                      params={"bw": 12}, step_id="off")
    src = _tune(20.0)
    cv = _chirp_editor([_bar(), off, src])._canvas
    g = cv._geom[off.uid]
    tgt = cv._drop_target(g["cx"], g["y"] + LANE_H / 2, src.uid)
    assert tgt is not None and tgt[0] is off and tgt[1] == "start"


# ── Context menu (Edit · Duplicate · Remove anchor · Delete) ────────────────────

def test_context_menu_spec_varies_by_item():
    up = _ramp_item(0.0, sid="up")
    down = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=3.0,
                       anchor_step_id="up", anchor_edge="end",
                       ramp={"param": "power", "start": -50.0, "stop": -90.0,
                             "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), up, down, _hold(400.0)])._canvas
    # an anchored item gets Remove anchor; a plain item doesn't; a Hold has no Duplicate
    assert cv._context_menu_spec(down) == ["Edit…", "Offset…", "Duplicate", "Remove anchor", "—", "Delete"]
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
    up = _ramp_item(0.0, sid="up")                   # start 0, end 6 (incl. final hold)
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

# ── Connector routing (obstacle avoidance + horizontal entry) ───────────────────

def test_connector_points_drop_avoids_an_intervening_obstacle():
    cv = _chirp_editor([_bar()])._canvas
    # anchor (100,10) → dependent (500,90); an intervening step occupies the naive drop column
    obstacles = [(420.0, 460.0)]                 # overlaps x2 - chip_run (500 - 60 = 440)
    pts = cv._connector_points(100.0, 10.0, 500.0, 90.0, 1.0, obstacles, 60.0)
    drop_xs = [pts[i][0] for i in range(1, len(pts)) if pts[i][1] != pts[i - 1][1]]
    assert drop_xs and all(not (420.0 <= dx <= 460.0) for dx in drop_xs)   # never drops through it
    assert pts[0] == (100.0, 10.0) and pts[-1] == (500.0, 90.0)            # exit/enter at the ends


def test_drag_snap_targets_and_cursor():
    ramp = _ramp_item(100.0, sid="r")
    cv = _chirp_editor([_bar(), ramp])._canvas
    bar = next(it for it in cv._items if it.kind == "bar")
    gb = cv._geom[bar.uid]; gr = cv._geom[ramp.uid]
    targets = cv._snap_targets({ramp.uid})        # dragging the ramp (exclude set)
    assert cv._on in targets and cv._off in targets            # the on-air / off-air anchors
    assert gb["start_x"] in targets and gb["stop_x"] in targets   # the OTHER item's edges
    assert gr["start_x"] not in targets and gr["stop_x"] not in targets   # never itself
    # a cursor a few px from another edge snaps onto it
    snapped = cv._snap_cursor(gb["stop_x"] + 3.0, ramp.uid)
    assert snapped is not None and abs(snapped - (gb["stop_x"] + 3.0)) <= SNAP_PX


# ── Multi-select / marquee / group move ─────────────────────────────────────────

def test_multi_select_toggle_and_select_only():
    a = _tune(10.0); b = _tune(20.0)
    cv = _chirp_editor([_bar(), a, b])._canvas
    cv._select_only(a.uid)
    assert cv._selection == {a.uid} and cv._selected == a.uid
    cv._toggle_select(b.uid)
    assert cv._selection == {a.uid, b.uid} and cv._selected == b.uid
    cv._toggle_select(a.uid)
    assert cv._selection == {b.uid}
    cv._clear_selection()
    assert not cv._selection and cv._selected is None


def test_delete_selection_removes_all_in_one_undo():
    a = _tune(10.0); b = _tune(20.0)
    cv = _chirp_editor([_bar(), a, b])._canvas
    cv._selection = {a.uid, b.uid}; cv._selected = a.uid
    cv._delete_selection()
    assert all(it.uid not in (a.uid, b.uid) for it in cv._items)
    cv.undo()                                    # one undo restores both
    assert any(it.uid == a.uid for it in cv._items) and any(it.uid == b.uid for it in cv._items)


def test_apply_marquee_selects_only_intersecting_items():
    a = _tune(10.0); b = _tune(400.0)
    cv = _chirp_editor([_bar(), a, b])._canvas
    ga = cv._geom[a.uid]
    cv._marquee = {"x0": ga["cx"] - 5, "y0": ga["y"] - 2, "x1": ga["cx"] + 5,
                   "y1": ga["y"] + LANE_H + 2, "additive": False, "base": set(), "moved": True}
    cv._apply_marquee()
    assert a.uid in cv._selection and b.uid not in cv._selection


def test_group_move_shifts_all_selected_by_one_delta():
    a = _tune(10.0); b = _tune(30.0)
    cv = _chirp_editor([_bar(), a, b])._canvas
    cv._selection = {a.uid, b.uid}; cv._selected = a.uid
    eff = cv._eff(); mid = tlm.midpoint(cv._on, cv._off)
    cv._drag = {"item": a, "part": "run_body", "press_x": cv._geom[a.uid]["cx"], "moved": True,
                "start0": 0.0, "stop0": 0.0, "undo0": [], "collapse": a.uid,
                "group": {a.uid, b.uid}, "group0": cv._group_bases({a.uid, b.uid})}
    cv._group_move(cv._geom[a.uid]["cx"] + 40.0 * eff, eff, mid)
    assert abs((b.offset - a.offset) - 20.0) < 1e-6      # the gap is preserved exactly
    assert 34.0 <= a.offset <= 54.0                       # both shifted ~+40 s (snapping tolerant)


def test_connector_points_wraps_when_offset_is_zero():
    cv = _chirp_editor([_bar()])._canvas
    # dependent start == anchor x (offset 0) → no room on the right → wrap (6 waypoints)
    pts = cv._connector_points(300.0, 10.0, 300.0, 90.0, 1.0, [], 60.0)
    assert len(pts) == 6
    assert pts[0] == (300.0, 10.0) and pts[-1] == (300.0, 90.0)
    # the final segment enters horizontally (same y as the dependent)
    assert pts[-2][1] == pts[-1][1]


def test_connector_points_enters_from_the_right_for_a_negative_offset():
    cv = _chirp_editor([_bar()])._canvas
    # dependent (x2=120) sits LEFT of the anchor edge (x1=400): a negative step offset → the line
    # enters from the RIGHT (arrow points left), so the final horizontal run comes from higher x.
    pts = cv._connector_points(400.0, 10.0, 120.0, 90.0, 1.0, [], 60.0, entry_from_right=True)
    assert pts[0] == (400.0, 10.0) and pts[-1] == (120.0, 90.0)
    assert pts[-2][1] == pts[-1][1]                       # enters horizontally
    assert pts[-2][0] > pts[-1][0]                        # …from the right (higher x → x2)


def test_offset_chip_text_signs_the_offset():
    cv = _chirp_editor([_bar()])._canvas
    assert cv._offset_chip_text(90.0).startswith("+")     # a forward offset gets a leading +
    neg = cv._offset_chip_text(-90.0)
    assert neg.startswith("−") and "+" not in neg         # a negative keeps its − (no +−)


def test_span_clear_detects_a_crossing_line():
    cv = _chirp_editor([_bar()])._canvas
    other = [(200.0, 50.0), (200.0, 90.0)]                # a vertical segment at x=200
    assert not cv._span_clear(200.0, 70.0, 5.0, [other])  # right on it → not clear
    assert cv._span_clear(300.0, 70.0, 5.0, [other])      # well away → clear


def _dep(off, ref, edge="start", sid="", param="bw"):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="step", offset=off,
                       params={param: 12}, anchor_step_id=ref, anchor_edge=edge, step_id=sid)


def test_negative_offset_dependent_paints_without_error():
    # A four-step layout in the owner's shape: a dependent BEFORE its anchor + two after it. The
    # canvas must lay out + paint (connectors routed, arrows placed) without raising. Each step
    # sets a DISTINCT param so the layout (not param conflicts) is what's under test — s3/s4 fire
    # at the same 0:30 as s1, which the same-param/same-time rule would otherwise flag.
    s1 = _tune(30.0, sid="s1")                             # fires at 0:30 (sets bw)
    s2 = _dep(-30.0, "s1", "start", sid="s2", param="sidelobes")   # anchored to s1, 30 s BEFORE → 0:00
    s3 = _dep(30.0, "s2", "start", param="freq")          # anchored to s2, +30 s → 0:30
    s4 = _dep(30.0, "s2", "start", param="prn")           # anchored to s2, +30 s → 0:30
    ed = _chirp_editor([_bar(), s1, s2, s3, s4])
    assert tlm.validate(ed._canvas._items, ["chirp"]) is None
    ed._canvas.resize(900, 400)
    ed._canvas.grab()                                     # paints (paths + backed-off arrows) OK


def test_minimap_present_and_navigates():
    ed = _chirp_editor([_bar(), _tune(20.0)])
    assert hasattr(ed, "_minimap")
    ed._minimap.resize(400, 34)
    ed._minimap.grab()                    # paints without error (guides + segments + viewport)
    ed._minimap._scroll_to(200.0)         # driving scroll is safe even when not scrollable


def test_tooltip_text_describes_anchor_and_bar():
    up = _ramp_item(0.0, sid="up")
    down = _down_ramp()
    cv = _chirp_editor([_bar(), up, down])._canvas
    txt = cv._tooltip_text(down)
    # v3 #2/#3: "X after the anchor's end" — the anchor is visible on the canvas, so the tooltip
    # names the EDGE + offset, never the task; and an absolute on-air line follows.
    assert "⚓ 3 s after the anchor's end" in txt
    assert "chirp" not in txt                                       # no task name for a ramp
    assert "starts 9 s after on-air" in txt                         # the absolute line
    bar = next(it for it in cv._items if it.kind == "bar")
    btxt = cv._tooltip_text(bar)
    assert "starts" in btxt and "stops" in btxt and "chirp" in btxt   # a bar keeps its task


# ── /code-review fixes: drag / hit-test / display / gate ─────────────────────────

def test_anchor_base_x_uses_the_target_edge_for_a_step_anchor():
    """Fix #1: a step-anchored item's body-drag measures its offset from the TARGET's edge,
    not off-air — so dragging it no longer corrupts the anchor offset."""
    target = _tune(10.0, sid="tgt")                       # a point at on-air+10 (start==end)
    dep = _tune(3.0)                                      # will anchor to tgt
    dep.anchor = "step"; dep.anchor_step_id = "tgt"; dep.anchor_edge = "end"
    cv = _chirp_editor([_bar(), target, dep])._canvas
    eff = cv._eff()
    base_x = cv._anchor_base_x(dep)
    assert abs(base_x - (cv._on + 10.0 * eff)) < 0.5      # the target's edge, not off-air
    assert abs(base_x - cv._off) > 1.0                    # NOT the off-air anchor (the old bug)
    # a plain start / stop anchor is unchanged
    assert abs(cv._anchor_base_x(target) - cv._on) < 0.5


def test_group_move_does_not_double_shift_a_step_anchored_dependent():
    """Fix #2: moving a group that contains BOTH a target and its step-anchored dependent shifts
    the dependent via its target only (its offset is left alone), so the gap is preserved."""
    target = _tune(10.0, sid="tgt")
    dep = _tune(5.0)
    dep.anchor = "step"; dep.anchor_step_id = "tgt"; dep.anchor_edge = "end"
    cv = _chirp_editor([_bar(), target, dep])._canvas
    eff = cv._eff(); mid = tlm.midpoint(cv._on, cv._off)
    cv._selection = {target.uid, dep.uid}
    press_x = cv._geom[target.uid]["cx"]
    cv._drag = {"item": target, "part": "run_body", "press_x": press_x, "moved": True,
                "group": {target.uid, dep.uid}, "group0": cv._group_bases({target.uid, dep.uid})}
    cv._group_move(press_x + 45.0, eff, mid)
    assert dep.offset == 5.0                              # unchanged — it follows its target
    assert target.offset != 10.0                          # the target actually moved
    # the resolved gap between them is preserved (still the dependent's own offset)
    bases = tlm.resolve_step_offsets(cv._items, None)
    tgt_edge = bases_edge = target.offset                 # a point's end == its offset
    assert round(bases[dep.uid] - tgt_edge, 6) == 5.0


def test_negative_step_offset_reads_without_a_double_sign():
    """Fix #3: a negative step offset shows as '−M:SS' (not '+−M:SS') in the tooltip."""
    up = _ramp_item(0.0, sid="up")
    early = _tune(0.0)
    early.anchor = "step"; early.anchor_step_id = "up"; early.anchor_edge = "start"; early.offset = -30.0
    cv = _chirp_editor([_bar(), up, early])._canvas
    txt = cv._tooltip_text(early)
    assert "+−" not in txt and "+-" not in txt       # never a doubled sign
    # v3 #2: a negative offset reads as "X before the anchor's <edge>" (no sign glyph at all)
    assert "⚓ 30 s before the anchor's start" in txt
    assert "fires 30 s before on-air" in txt         # the absolute line


def test_pin_footprint_matches_the_drawn_chips_not_a_symmetric_band():
    """Fix #6: a tune pin's hit footprint spans the dot + its chips on the caption side, so the
    far chips are clickable and the empty space on the other side of the dot is not a false hit."""
    t = _tune(20.0)
    t.params = {"bw": 12, "sidelobes": 5, "rf": "on"}     # several chips → a wide run
    cv = _chirp_editor([_bar(), t])._canvas
    g = cv._geom[t.uid]; cx = g["cx"]
    lo, hi = cv._pin_footprint(t, g)
    total = cv._tune_chip_defs(t)[1]
    assert hi >= cx + total - 1.0                         # reaches the far edge of the chips
    assert abs(lo - (cx - 7.5)) < 0.6                     # tight to the dot on the empty side
    assert lo <= cx <= hi and (cx + total * 0.9) <= hi    # a far-chip x is inside the hit region
    assert (cx - total / 2 - 10) < lo                     # the old symmetric band's left is excluded


def test_plan_step_anchor_gate_blocks_an_old_agent():
    """Fix #4: the plan/schedule arm gate blocks a step-anchored sequence on a unit whose agent
    can't resolve it, instead of letting the agent 400 the arm."""
    from types import SimpleNamespace
    from ui.plans_tab import _step_anchor_block_lines

    def _client(caps, ver):
        return SimpleNamespace(_c=set(caps), agent_version=ver,
                               supports=lambda c, _s=set(caps): c in _s)
    old = _client([], "1.23.0")
    new = _client(["sequence-step-anchor", "sequence-step-anchor-negative"], "1.25.0")
    old_neg = _client(["sequence-step-anchor"], "1.24.0")
    step = SimpleNamespace(anchor="step", offset_s=5.0)
    neg = SimpleNamespace(anchor="step", offset_s=-5.0)
    plain = SimpleNamespace(anchor="start", offset_s=0.0)

    def fleet(mapping):
        return SimpleNamespace(get=lambda h: mapping[h])

    # old agent, step anchor → blocked (≥1.24.0)
    lines = _step_anchor_block_lines([("u", "Unit A", [step])], fleet({"u": old}))
    assert lines and "1.24.0" in lines[0]
    # capable agent → no block
    assert _step_anchor_block_lines([("u", "Unit A", [step])], fleet({"u": new})) == []
    # negative offset on a 1.24 agent → blocked (needs 1.25.0)
    lines = _step_anchor_block_lines([("u", "Unit A", [neg])], fleet({"u": old_neg}))
    assert lines and "1.25.0" in lines[0]
    # no step anchor → never blocked (agent never consulted)
    assert _step_anchor_block_lines([("u", "Unit A", [plain])], fleet({"u": old})) == []


# ── Ramp move (drag on the timeline; never resized) + real-time dependent movement ──────

def _long_ramp(off, sid="", task="chirp"):
    return tlm.RunItem(task_name=task, action="ramp", anchor="start", offset=off, step_id=sid,
                       ramp={"param": "power", "start": -90.0, "stop": -50.0,
                             "steps": 3, "duration_s": 40.0})


def test_ramp_body_is_draggable_and_moves_the_offset():
    from ui.timeline_editor import DRAG_PARTS, LANE_H
    assert "ramp_body" in DRAG_PARTS
    r = _long_ramp(20.0)
    cv = _chirp_editor([_bar(), r])._canvas
    g = cv._geom[r.uid]
    midx = (g["start_x"] + g["stop_x"]) / 2.0
    y = g["y"] + LANE_H / 2
    # the middle of a ramp body hit-tests as a movable body (not an edge/anchor handle)
    hit = cv._hit(midx, y)
    assert hit is not None and hit[0] is r and hit[1] == "ramp_body"
    # a body drag shifts the ramp's OFFSET (its start_x moves), duration unchanged
    eff = cv._eff(); w0 = g["stop_x"] - g["start_x"]
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent
    cv._drag = {"item": r, "part": "ramp_body", "press_x": midx, "moved": True,
                "start0": 0.0, "stop0": 0.0, "off0": 20.0, "undo0": [], "collapse": None,
                "group": None, "group0": {}}
    tgt_x = midx + 30.0 * eff
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(tgt_x, y), QPointF(tgt_x, y),
                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    cv.mouseMoveEvent(ev)
    assert 48.0 <= r.offset <= 52.0                         # ~+30 s (snapping tolerant)
    g2 = cv._geom[r.uid]
    assert abs((g2["stop_x"] - g2["start_x"]) - w0) < 1.0   # width (duration) unchanged — no resize


def test_live_move_repositions_a_dependent_in_real_time():
    # Dragging a TARGET moves every step anchored to it AS IT MOVES (not only on release).
    target = _tune(30.0, sid="tgt")
    dep = _dep(10.0, "tgt", edge="start")                   # fires 10 s after the target's start
    cv = _chirp_editor([_bar(), target, dep])._canvas
    eff = cv._eff()
    dep_x0 = cv._geom[dep.uid]["cx"]
    target.offset = 60.0                                    # simulate the drag mutating the target
    cv._live_move(target)
    dep_x1 = cv._geom[dep.uid]["cx"]
    assert abs((dep_x1 - dep_x0) - 30.0 * eff) < 1e-6        # the dependent tracked +30 s live


def test_live_move_repositions_a_chained_dependent():
    a = _tune(30.0, sid="a")
    b = _dep(5.0, "a", edge="start", sid="b")               # b anchored to a
    c = _dep(5.0, "b", edge="start")                        # c anchored to b (a chain)
    cv = _chirp_editor([_bar(), a, b, c])._canvas
    eff = cv._eff()
    b_x0 = cv._geom[b.uid]["cx"]; c_x0 = cv._geom[c.uid]["cx"]
    a.offset = 50.0
    cv._live_move(a)
    assert abs((cv._geom[b.uid]["cx"] - b_x0) - 20.0 * eff) < 1e-6
    assert abs((cv._geom[c.uid]["cx"] - c_x0) - 20.0 * eff) < 1e-6   # the whole chain followed


def test_ramp_end_handle_begins_an_anchor(qtbot=None):
    # A drag from a ramp's END dot now BEGINS a drag-to-anchor (the ramp is still positioned by
    # its start), so the operator can grab whichever end is nearer the target (#13).
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    up = _long_ramp(2.0, sid="up")
    down = _long_ramp(60.0)                                  # a plain start-anchored ramp
    cv = _chirp_editor([_bar(), up, down])._canvas
    g = cv._geom[down.uid]
    # the END dot hit-tests as edge_end …
    hit = cv._hit(g["stop_x"], g["y"] + LANE_H / 2)
    assert hit is not None and hit[0] is down and hit[1] == "edge_end"
    # … and pressing it starts a connect-drag with the source = the ramp, grabbed from its end
    pos = QPointF(g["stop_x"], g["y"] + LANE_H / 2)
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, pos, pos,
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    cv.mousePressEvent(ev)
    assert cv._connect is not None
    assert cv._connect["src"] == down.uid and cv._connect["from_edge"] == "end"


# ── #10: drag-anchor a step onto a root line (on-air / off-air / Hold resume) + selected hint ──

def test_drop_target_locks_onto_the_on_air_line():
    src = _tune(40.0)
    cv = _chirp_editor([_bar(), src])._canvas
    # a drop near the on-air line resolves to the root-start anchor sentinel
    tgt = cv._drop_target(cv._on + 2.0, cv._geom[src.uid]["y"] + LANE_H / 2, src.uid)
    assert tgt == ("__root__", "start")
    # near off-air → root-stop; far from any line → nothing
    assert cv._drop_target(cv._off - 1.0, 60.0, src.uid) == ("__root__", "stop")
    assert cv._drop_target((cv._on + cv._off) / 2.0, 60.0, src.uid) is None


def test_make_root_anchor_sets_a_stop_anchor_in_place():
    src = _tune(40.0)                                    # start-anchored at +40
    cv = _chirp_editor([_bar(), src])._canvas
    x0 = cv._geom[src.uid]["cx"]; off0 = cv._off; eff = cv._eff()
    cv._make_root_anchor(src.uid, "stop")               # drag onto off-air → stop-anchored
    assert src.anchor == "stop" and src.anchor_step_id == ""
    # its offset now measures its (unchanged) time from off-air — it kept its instant, not its
    # pixel x (the band reflows to fit the new stop-anchored content).
    assert abs(src.offset - (x0 - off0) / eff) < 1e-6


def test_selected_root_anchored_step_paints_a_hint():
    from PyQt6.QtGui import QPixmap
    src = _tune(40.0)
    cv = _chirp_editor([_bar(), src])._canvas
    cv._select_only(src.uid)
    cv.render(QPixmap(1400, 360))                        # paints the on-air tie without error
    # the hint only shows for a root-anchored selection; a step-anchored one uses the connector
    assert cv._selected == src.uid and getattr(src, "anchor", "") == "start"


# ── #9: a two-sided target (ramp/bar) exits AWAY from its body, never behind the bar ────

def test_connector_exits_right_from_a_two_sided_end_edge():
    cv = _chirp_editor([_bar()])._canvas
    # END edge at x1=400 (exit_dir +1, body to the left); dependent at x2=250 sits LEFT of the
    # edge (between the two sides) → the exit must go RIGHT (away from the body), not left.
    pts = cv._connector_points(400.0, 10.0, 250.0, 60.0, 1.0, [], 60.0, two_sided=True)
    assert pts[0] == (400.0, 10.0)
    assert pts[1][0] > 400.0                       # exits RIGHT, clear of the bar
    assert pts[-1] == (250.0, 60.0)


def test_connector_exits_left_from_a_two_sided_start_edge():
    cv = _chirp_editor([_bar()])._canvas
    # START edge at x1=200 (exit_dir −1, body to the right); dependent at x2=350 sits RIGHT of the
    # edge (between the sides) → the exit must go LEFT (away from the body), not right.
    pts = cv._connector_points(200.0, 10.0, 350.0, 60.0, -1.0, [], 60.0, two_sided=True)
    assert pts[1][0] < 200.0                        # exits LEFT, clear of the bar
    assert pts[-1] == (350.0, 60.0)


def test_connector_two_sided_off_keeps_the_old_routing():
    cv = _chirp_editor([_bar()])._canvas
    # a POINT target (two_sided=False) with a left dependent keeps the entry-from-right routing
    pts = cv._connector_points(400.0, 10.0, 250.0, 60.0, 1.0, [], 60.0, entry_from_right=True)
    assert pts[0] == (400.0, 10.0) and pts[-1] == (250.0, 60.0)
    assert pts[1][0] <= 400.0                       # does NOT jump right past the edge (unchanged)


def test_tune_body_drag_moves_by_delta_not_to_the_cursor():
    # Grabbing a tune's CAPTION (offset from the dot) and dragging must move the dot by the drag
    # DELTA from where it was — not jump the dot under the cursor (owner #11).
    from PyQt6.QtCore import QPointF, QEvent, Qt
    from PyQt6.QtGui import QMouseEvent
    t = _tune(30.0)
    cv = _chirp_editor([_bar(), t])._canvas
    g = cv._geom[t.uid]; dot_x = g["cx"]; y = g["y"] + LANE_H / 2
    press_x = dot_x + 50.0                              # grab the caption, 50 px right of the dot
    cv._drag = {"item": t, "part": "run_body", "press_x": press_x, "moved": True,
                "start0": 0.0, "stop0": 0.0, "off0": 30.0, "undo0": [], "collapse": None,
                "group": None, "group0": {}}
    tgt_x = press_x + 24.0                              # move the mouse +24 px
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(tgt_x, y), QPointF(tgt_x, y),
                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    cv.mouseMoveEvent(ev)
    new_dot = cv._geom[t.uid]["cx"]
    assert new_dot > dot_x + 8.0                        # the dot moved right by roughly the delta
    assert new_dot < tgt_x - 20.0                       # …and did NOT jump under the cursor


# ── v2 owner issues: connector exit side, END-tied ramps, delete cascade, bar-source connectors ──

def _connector_for(cv, dep):
    """Route `dep`'s connector exactly as _paint_connectors does, returning its waypoints."""
    by_sid = {getattr(o, "step_id", "") or "": o for o in cv._rows if getattr(o, "step_id", "")}
    ref, edge = tlm.step_source_ref(dep)
    tgt = by_sid[ref]
    x1 = cv._edge_x(tgt, edge); y1 = cv._geom[tgt.uid]["y"] + LANE_H / 2
    x2, efr, _two = cv._dep_entry(dep); y2 = cv._geom[dep.uid]["y"] + LANE_H / 2
    if efr is None:
        efr = x2 < x1 - 1.0
    two_sided = tlm._is_ramp(tgt) or getattr(tgt, "kind", "") == "bar"
    exit_dir = (-1.0 if edge == "start" else 1.0) if two_sided else cv._point_exit_dir(tgt, dep)
    return cv._connector_points(x1, y1, x2, y2, exit_dir, cv._intervening_obstacles(tgt.uid, dep.uid),
                                cv._chip_w(dep) + 24.0, None, efr, two_sided=two_sided)


def test_point_target_exits_left_toward_a_ramp_starting_at_it():
    # Owner image: a ramp anchored to a tune at +0 s. The line must leave the pin on the LEFT (the
    # ramp's start is at the pin; the drop column is left of it), not exit right and wrap around.
    t = _tune(20.0, sid="t")
    r = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=0.0, anchor_step_id="t",
                    anchor_edge="start", ramp={"param": "power", "start": -90.0, "stop": -50.0,
                                               "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), t, r])._canvas
    assert cv._point_exit_dir(t, r) < 0
    pts = _connector_for(cv, r)
    cx = cv._geom[t.uid]["cx"]
    assert pts[0][0] == cx and pts[1][0] < cx                 # exits LEFT of the pin…
    assert pts[-2][1] == pts[-1][1] and pts[-2][0] < pts[-1][0]   # …and enters the ramp from the left
    assert len(pts) == 4                                       # no wrap
    assert cv._pin_caption_side(t) == "right"                  # the caption keeps the clear side


def test_ramp_dependent_before_its_target_is_entered_outside_its_body():
    # Owner image: a ramp anchored to a tune with a NEGATIVE offset (it starts before the tune).
    # A ramp is always entered from OUTSIDE its body — from the left of its start — never from the
    # right (through the capsule); and the pin exits left toward it.
    t = _tune(30.0, sid="t")
    r = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=-3.0, anchor_step_id="t",
                    anchor_edge="start", ramp={"param": "power", "start": -90.0, "stop": -50.0,
                                               "steps": 3, "duration_s": 6.0})
    cv = _chirp_editor([_bar(), t, r])._canvas
    x2, efr, two = cv._dep_entry(r)
    assert two and efr is False and x2 == cv._geom[r.uid]["start_x"]
    assert cv._point_exit_dir(t, r) < 0
    pts = _connector_for(cv, r)
    assert pts[1][0] < cv._geom[t.uid]["cx"]                    # exits left
    assert pts[-2][0] < pts[-1][0] == x2                       # enters the ramp START from the left


def test_far_dependent_still_exits_right_and_flips_the_caption():
    # The old rule is kept where the chip fits: a dependent well to the right → exit right.
    t = _tune(20.0, sid="t")
    d = _tune(80.0)
    d.anchor, d.anchor_step_id, d.anchor_edge = "step", "t", "end"; d.offset = 60.0
    cv = _chirp_editor([_bar(), t, d])._canvas
    assert cv._point_exit_dir(t, d) > 0
    assert cv._pin_caption_side(t) == "left"


def test_drag_from_a_ramps_end_ties_its_end_and_keeps_it_in_place():
    # #C: dragging from the END dot ties the END: offset = (end x − target x)/eff, the ramp runs
    # backward from there, and its start base stays where it was (the ramp doesn't move).
    r = _long_ramp(10.0)                                       # 10 → 50 (dur 40)
    t = _tune(60.0, sid="t")
    cv = _chirp_editor([_bar(), r, t])._canvas
    cv._make_anchor(r.uid, t, "start", from_edge="end")
    assert r.anchor == "step" and r.anchor_own_edge == "end"
    assert r.offset == -10.0                                   # its END sits 10 s before the tune
    assert tlm.step_wire_offset(r) == -50.0                    # the START's offset on the wire
    assert cv._step_bases[r.uid] == 10.0                       # didn't move
    x2, efr, two = cv._dep_entry(r)
    assert two and efr is True and x2 == cv._geom[r.uid]["stop_x"]   # entered at its end, from the right
    pts = _connector_for(cv, r)
    assert pts[-2][0] > pts[-1][0] == x2                       # the run comes in from the right
    assert cv._edge_linked(r, "end") and not cv._edge_linked(r, "start")
    assert "its end" in cv._tooltip_text(r)
    # detaching re-roots it on-air at its resolved START and clears the tie
    cv._detach_anchor(r.uid)
    assert r.anchor == "start" and r.offset == 10.0 and r.anchor_own_edge == "start"


def test_drag_from_a_ramps_start_still_ties_its_start():
    r = _long_ramp(10.0)
    t = _tune(60.0, sid="t")
    cv = _chirp_editor([_bar(), r, t])._canvas
    cv._make_anchor(r.uid, t, "start", from_edge="start")
    assert r.anchor_own_edge == "start" and r.offset == -50.0


def test_editor_steps_roundtrip_preserves_the_end_tie():
    t = _tune(60.0, sid="t")
    r = tlm.RunItem(task_name="chirp", action="ramp", anchor="step", offset=-10.0, anchor_step_id="t",
                    anchor_edge="start", anchor_own_edge="end",
                    ramp={"param": "power", "start": -90.0, "stop": -50.0, "steps": 3, "duration_s": 40.0})
    ed = _chirp_editor([_bar(), t, r])
    steps = ed.steps()
    rs = next(s for s in steps if s.action.value == "ramp")
    assert rs.offset_s == -50.0 and rs.anchor_own_edge == "end"
    ed.set_steps(steps)
    rb = next(it for it in ed.items() if tlm._is_ramp(it))
    assert rb.anchor_own_edge == "end" and rb.offset == -10.0


def test_ramp_editor_tie_picker_saves_an_end_tie():
    dlg = _ramp_dlg([_bar(), _tune(5.0, sid="tgt")])
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("step"))
    _app.processEvents()
    assert dlg._own_row.isVisibleTo(dlg)                       # Tie picker shown for a step anchor
    dlg._own_edge.setCurrentIndex(dlg._own_edge.findData("end"))
    _app.processEvents()
    assert dlg._ft_sublabels()[0] == "ramp start"              # the TO end is what's timed now
    assert "End offset" in dlg._off_lbl.text()
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0); dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._accept()
    assert dlg.result_item is not None
    assert dlg.result_item.anchor == "step" and dlg.result_item.anchor_own_edge == "end"
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("start"))
    _app.processEvents()
    assert not dlg._own_row.isVisible()


def test_deleting_a_bar_cascades_to_its_tunes_and_ramps_but_not_one_shots():
    # #D: a duration task takes its tunes/ramps with it; a one-shot of the same task is an
    # independent launch and stays; a dependent of a deleted tune is re-rooted at its fire time.
    bar = _bar()
    tune = _tune(10.0, sid="tn")
    ramp = _ramp_item(20.0)
    shot = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=30.0)
    dep = tlm.RunItem(task_name="chirp", action="run", anchor="step", offset=5.0,
                      anchor_step_id="tn", anchor_edge="end")        # a one-shot hanging off the tune
    cv = _chirp_editor([bar, tune, ramp, shot, dep])._canvas
    cv._delete_with_reanchor(bar.uid)
    left = {it.uid for it in cv._items}
    assert bar.uid not in left and tune.uid not in left and ramp.uid not in left
    assert shot.uid in left and dep.uid in left
    assert dep.anchor == "start" and dep.offset == 15.0             # re-rooted at 10 + 5
    cv.undo()                                                       # ONE undo restores everything
    assert {it.uid for it in cv._items} >= {bar.uid, tune.uid, ramp.uid, shot.uid, dep.uid}


def test_delete_selection_with_a_bar_cascades_too():
    bar = _bar(); tune = _tune(10.0); other = _tune(20.0)
    cv = _chirp_editor([bar, tune, other])._canvas
    cv._selection = {bar.uid}; cv._selected = bar.uid
    cv._delete_selection()
    assert all(it.uid not in (bar.uid, tune.uid, other.uid) for it in cv._items)
    assert not cv._selection


def test_bar_source_draws_a_connector_and_can_be_detached():
    # Gap from #6: a bar whose START hangs off a step gets a connector, a Remove-anchor entry,
    # a tooltip line, and detaches onto on-air at its resolved start.
    t = _tune(20.0, sid="t")
    b2 = tlm.BarItem(task_name="other", start_anchor="step", start_anchor_step_id="t",
                     start_anchor_edge="end", start_offset=5.0, stop_offset=0.0, args=[])
    ed = _chirp_editor([_bar(), t, b2]); cv = ed._canvas
    calls = []
    orig = cv._connector_points
    cv._connector_points = lambda *a, **k: (calls.append(a), orig(*a, **k))[1]
    cv.grab()
    assert len(calls) == 1
    assert calls[0][0] == cv._geom[t.uid]["cx"] and calls[0][2] == cv._geom[b2.uid]["start_x"]
    assert "Remove anchor" in cv._context_menu_spec(b2)
    assert "its start" in cv._tooltip_text(b2)
    assert cv._edge_linked(b2, "start")
    assert cv._is_anchor_target(t)
    cv._detach_anchor(b2.uid)
    assert b2.start_anchor == "start" and b2.start_offset == 25.0 and not b2.start_anchor_step_id
