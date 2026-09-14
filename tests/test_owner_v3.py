"""Owner-testing round 3 (Issues_and_wishes_v3) — the sequence timeline editor:

  #1/#6  a drag readout is the OFFSET only (no doubled root chip, no '· on-air' on a step anchor)
  #2     hover tooltip: 'X before/after the anchor's edge' + absolute line(s) (on-air / resume /
         off-air / the pause — both past a Hold)
  #3     tune / ramp rows never name their duration task (the row's indent + hue already do)
  #4     the Hold's START ('enter') edge is anchorable — a ramp's END / nothing else; the resume
         edge takes starts and tunes (anchor="enter", agent >= 1.26.0, sequence-hold-enter)
  #5     a duration task's START dot is its anchor handle; the capsule grip resizes
  #7     RF auto-gating when a duration task is dragged across on-air / off-air
  #8     a tune / ramp can't leave its task on EITHER end; dragging an anchor TARGET can't push
         a dependent out of its task
  #9     the absolute windows / off-air expand AS you drag (on-air pinned), not on release
"""
import copy
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui import rf_gate
from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import (BAR_DOT_HIT, LANE_H, StepEditorDialog, _fmt_offset,
                                _ramp_end_side_off, _timing_text)
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _tune(off, sid="", anchor="start", task="chirp", params=None):
    return tlm.RunItem(task_name=task, action="tune", anchor=anchor, offset=off,
                       params=params if params is not None else {"bw": 12}, step_id=sid)


def _ramp(off, anchor="start", sid="", dur=60.0):
    return tlm.RunItem(task_name="chirp", action="ramp", anchor=anchor, offset=off, step_id=sid,
                       ramp={"param": "power", "start": -50.0, "stop": -90.0, "steps": 3,
                             "duration_s": dur})


def _hold(off=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=off)


def _the_bar(cv):
    return next(i for i in cv._items if i.kind == "bar")


def _capture_tags(cv):
    tags = []
    cv._paint_tag = lambda p, x, y, text, strong=True: tags.append(text)
    return tags


def _tunes(cv, params):
    return [o for o in cv._items
            if getattr(o, "kind", None) != "bar" and getattr(o, "action", "") == "tune"
            and (o.params or {}) == params]


# ── #1 / #6 — drag readout = the offset only ───────────────────────────────────

def test_drag_readout_is_the_offset_only():
    t = _tune(35.0)
    cv = _chirp_editor([_bar(), t])._canvas
    tags = _capture_tags(cv)
    cv._drag = {"item": t, "part": "run_body", "moved": True}
    cv._paint_drag_readout(None)
    cv._drag = None
    assert tags == [_fmt_offset(35.0)]                 # '+35 s' — no '· on-air' suffix
    assert "on-air" not in tags[0]


def test_drag_readout_of_a_step_anchored_pin_never_names_a_root():
    tgt = _tune(10.0, sid="tgt")
    dep = _tune(5.0)
    dep.anchor = "step"; dep.anchor_step_id = "tgt"; dep.anchor_edge = "end"
    cv = _chirp_editor([_bar(), tgt, dep])._canvas
    tags = _capture_tags(cv)
    cv._drag = {"item": dep, "part": "run_body", "moved": True}
    cv._paint_drag_readout(None)
    cv._drag = None
    assert tags == [_fmt_offset(5.0)]                  # the offset from the step, nothing else
    assert "on-air" not in tags[0] and "off-air" not in tags[0]


def test_drag_readout_of_a_bar_stop_is_its_stop_offset():
    bar = _bar(); bar.stop_offset = -12.0
    cv = _chirp_editor([bar])._canvas
    tags = _capture_tags(cv)
    cv._drag = {"item": bar, "part": "bar_stop", "moved": True}
    cv._paint_drag_readout(None)
    cv._drag = None
    assert tags == [_fmt_offset(-12.0)]


def test_root_anchor_hint_chip_is_dropped_while_dragging():
    """At rest a SELECTED root-anchored step shows its 'on-air +X' tie chip; while it is being
    dragged the drag readout is the only label (no doubled 'on-air +35s' / '+35 s · on-air')."""
    t = _tune(35.0)
    cv = _chirp_editor([_bar(), t])._canvas
    cv._select_only(t.uid)
    tags = _capture_tags(cv)
    pm = QPixmap(400, 200); p = QPainter(pm)
    try:
        cv._paint_root_anchor_hint(p)
        assert len(tags) == 1 and tags[0].startswith("on-air")
        tags.clear()
        cv._drag = {"item": t, "part": "run_body", "moved": True}
        cv._paint_root_anchor_hint(p)
        assert tags == []
    finally:
        cv._drag = None
        p.end()


# ── #2 — tooltip: relative-to-anchor + absolute lines ────────────────────────

def test_tooltip_absolute_lines_for_on_air_and_off_air_steps():
    t = _tune(35.0)
    s = _tune(-30.0, anchor="stop")
    cv = _chirp_editor([_bar(), t, s])._canvas
    ttxt, stxt = cv._tooltip_text(t), cv._tooltip_text(s)
    assert "fires 35 s after on-air" in ttxt
    assert "fires 30 s before off-air" in stxt
    assert "chirp" not in ttxt and "chirp" not in stxt      # a tune never names its task (#3)


def test_tooltip_past_a_hold_reads_both_after_resume_and_before_off_air():
    wb = _tune(20.0, anchor="hold")
    s = _tune(-30.0, anchor="stop")
    cv = _chirp_editor([_bar(), _hold(300.0), wb, s])._canvas
    txt = cv._tooltip_text(wb)
    assert "fires 20 s after resume" in txt
    # off-air floats to just past the post-hold content (20 s forward + 30 s of off-air lead-in =
    # 50 s after resume), so the window-B tune is also a fixed 30 s before off-air …
    assert "fires 30 s before off-air" in txt
    # … and the off-air-anchored tune is likewise a fixed 20 s after resume
    stxt = cv._tooltip_text(s)
    assert "fires 30 s before off-air" in stxt and "fires 20 s after resume" in stxt


def test_tooltip_of_a_pause_anchored_ramp_reads_the_pause_and_on_air():
    rp = _ramp(-10.0, anchor="enter")
    cv = _chirp_editor([_bar(), _hold(300.0), rp])._canvas
    txt = cv._tooltip_text(rp)
    assert "<b>Ramp</b>" in txt and "chirp" not in txt
    assert "ends 10 s before the pause" in txt
    assert "ends 4 min, 50 s after on-air" in txt


def test_tooltip_step_anchor_line_names_the_edge_not_the_task():
    tgt = _tune(10.0, sid="tgt")
    dep = _tune(5.0)
    dep.anchor = "step"; dep.anchor_step_id = "tgt"; dep.anchor_edge = "start"
    cv = _chirp_editor([_bar(), tgt, dep])._canvas
    txt = cv._tooltip_text(dep)
    assert "⚓ 5 s after the anchor's start" in txt
    assert "fires 15 s after on-air" in txt


# ── #3 — rows are named by what they do, not their task ───────────────────────

def test_row_header_names_tune_and_ramp_by_what_they_do():
    t = _tune(35.0); rp = _ramp(40.0)
    ed = _chirp_editor([_bar(), t, rp])
    name, sub, kind = ed._rowhdr._meta(t)
    assert name == "bw tune" and kind == "Tune" and sub == "bw=12"
    name, _sub, kind = ed._rowhdr._meta(rp)
    assert name == "power ramp" and kind == "Ramp"
    name, _sub, kind = ed._rowhdr._meta(_the_bar(ed._canvas))
    assert name == "chirp" and kind == "Duration"          # a task row IS its task


# ── #8 — drag clamps: inside the task on both ends; dependents too ────────────

def test_tune_is_clamped_inside_its_task_on_both_ends():
    t = _tune(35.0)
    cv = _chirp_editor([_bar(), t])._canvas
    g = cv._geom[_the_bar(cv).uid]; eff = cv._eff()
    lo, hi = cv._task_range(t)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx((g["stop_x"] - cv._on) / eff)          # the bar's drawn END
    assert cv._clamp_tune_offset(t, -5.0) == pytest.approx(0.0)
    assert cv._clamp_tune_offset(t, hi + 100.0) == pytest.approx(hi)
    assert cv._clamp_tune_offset(t, 35.0) == 35.0
    # a one-shot is not task-bound
    run = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=900.0)
    cv.add_item(run)
    assert cv._task_range(run) is None
    assert cv._clamp_tune_offset(run, 900.0) == 900.0


def test_ramp_keeps_its_whole_extent_inside_the_task():
    rp = _ramp(40.0)                                     # 60 s long, start-tied
    cv = _chirp_editor([_bar(), rp])._canvas
    g = cv._geom[_the_bar(cv).uid]; eff = cv._eff()
    lo, hi = cv._task_range(rp)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx((g["stop_x"] - cv._on) / eff - 60.0)   # its END may not pass the stop
    # a stop-tied tune is bounded by the same bar on the OFF-AIR clock
    s = _tune(-30.0, anchor="stop"); cv.add_item(s)
    g = cv._geom[_the_bar(cv).uid]                        # re-read: the band re-laid out
    lo, hi = cv._task_range(s)
    assert lo == pytest.approx((g["start_x"] - cv._off) / eff)
    assert hi == pytest.approx((g["stop_x"] - cv._off) / eff)


def test_dragging_a_target_cannot_push_its_dependent_out_of_the_task():
    tgt = _tune(100.0, sid="tgt")
    dep = _tune(30.0)
    dep.anchor = "step"; dep.anchor_step_id = "tgt"; dep.anchor_edge = "end"
    cv = _chirp_editor([_bar(), tgt, dep])._canvas
    g = cv._geom[_the_bar(cv).uid]; eff = cv._eff()
    hi = (g["stop_x"] - cv._on) / eff
    assert cv._clamp_for_dependents(tgt, hi - 10.0) == pytest.approx(hi - 30.0)  # dep sits +30 after
    assert cv._clamp_for_dependents(tgt, 200.0) == 200.0                         # free mid-task
    # a dependent BEFORE its target (negative offset) bounds the target's LOW end instead
    dep.offset = -30.0
    cv.relayout()
    assert cv._clamp_for_dependents(tgt, 5.0) == pytest.approx(30.0)
    assert cv._dependents_of(tgt.uid) == [dep]


# ── #9 — the band expands live, with on-air pinned ─────────────────────────────

def test_live_expand_grows_the_band_with_on_air_pinned():
    run = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=100.0)
    cv = _chirp_editor([_bar(), run])._canvas
    on0, off0, w0 = cv._on, cv._off, cv.minimumWidth()
    eff = cv._eff()
    run.offset = 500.0                                   # dragged 400 s past what the band held
    cv._drag = {"item": run, "part": "run_body", "moved": True}
    cv._live_expand()
    assert cv._on == on0                                 # on-air stays put under the operator's eye
    assert cv._off > off0 + 300.0 * eff                  # off-air ran out to hold the new extent
    assert cv._geom[run.uid]["cx"] == pytest.approx(on0 + 500.0 * eff)
    assert cv.minimumWidth() >= w0                       # the canvas only grows mid-drag
    cv._drag = None
    cv.relayout(keep_on=True)
    assert cv._on == pytest.approx(on0)                  # release keeps on-air where it was (no jump)


def test_live_move_re_places_a_dependent_while_the_band_grows():
    tgt = _tune(100.0, sid="tgt")
    dep = tlm.RunItem(task_name="chirp", action="run", anchor="step", offset=20.0,
                      anchor_step_id="tgt", anchor_edge="end")
    cv = _chirp_editor([_bar(), tgt, dep])._canvas
    on0 = cv._on; eff = cv._eff()
    tgt.offset = 400.0
    cv._drag = {"item": tgt, "part": "run_body", "moved": True}
    cv._live_move(tgt)
    cv._drag = None
    assert cv._on == on0
    assert cv._geom[dep.uid]["cx"] == pytest.approx(on0 + 420.0 * eff)   # follows its target live


# ── #5 — the bar's START dot anchors; the grip resizes ────────────────────────

def test_bar_start_dot_is_the_anchor_handle_and_the_grips_resize():
    lead = _tune(5.0, sid="lead", task="other")
    bar = _bar()
    cv = _chirp_editor([lead, bar])._canvas
    g = cv._geom[bar.uid]; cy = g["y"] + LANE_H / 2
    assert cv._hit(g["start_x"], cy) == (bar, "edge_start")                    # the dot: anchor
    assert cv._hit(g["start_x"] + BAR_DOT_HIT + 3, cy) == (bar, "bar_start")   # the grip: resize
    assert cv._hit(g["stop_x"], cy) == (bar, "bar_stop")                       # stop: resize only
    assert cv._is_anchor_source(bar)
    # a drop on the lead tune hangs the task's START off it, keeping the bar in place …
    cv._make_anchor(bar.uid, lead, "end")
    assert bar.start_anchor == "step" and bar.start_anchor_step_id == "lead"
    assert bar.start_anchor_edge == "end"
    assert bar.start_offset == pytest.approx(-5.0)       # it starts 5 s before the lead
    # … and a drop on the on-air line re-roots it there at the same instant
    cv._make_root_anchor(bar.uid, "start")
    assert bar.start_anchor == "start" and bar.start_anchor_step_id == ""
    assert bar.start_offset == pytest.approx(0.0)


# ── #4 — the Hold's START edge ('enter') ───────────────────────────────────────

def test_enter_anchor_geometry_ends_at_the_pause():
    rp = _ramp(-10.0, anchor="enter")
    (la, lo), (ra, ro) = tlm.ramp_span(rp, 300.0)
    assert (la, ra) == ("start", "start")
    assert ro == pytest.approx(290.0) and lo == pytest.approx(230.0)   # END at the pause − 10
    assert tlm.effective_anchor_offset(_tune(-5.0, anchor="enter"), 300.0) == ("start", 295.0)
    assert tlm.carry_order_key("enter", -5.0, 300.0) < tlm.carry_order_key("hold", 0.0, 300.0)
    assert _ramp_end_side_off("enter", "start", 290.0, 300.0) == ("enter", -10.0)


def test_enter_timing_labels():
    assert _timing_text(0.0, "enter", True) == "at pause"
    assert _timing_text(-5.0, "enter", True) == "-5 s · before pause"
    assert _timing_text(-5.0, "enter", False) == "-5 s"


def test_validate_enter_needs_a_hold_and_cannot_reach_into_the_pause():
    assert tlm.validate([_bar(), _hold(300.0), _ramp(-10.0, anchor="enter")]) is None
    assert tlm.validate([_bar(), _hold(300.0), _tune(0.0, anchor="enter")]) is None
    err = tlm.validate([_bar(), _ramp(-10.0, anchor="enter")])
    assert err and "Hold" in err
    err = tlm.validate([_bar(), _hold(300.0), _tune(5.0, anchor="enter")])
    assert err and "at or before the pause" in err


def test_hold_enter_gate_and_uses():
    class _C:
        def __init__(self, ver, caps):
            self.agent_version = ver; self._caps = caps

        def supports(self, cap):
            return cap in self._caps

    assert tlm.hold_enter_supported(_C("1.26.0", {"sequence-hold-enter"}))
    assert not tlm.hold_enter_supported(_C("1.25.3", {"sequence-hold-enter"}))
    assert not tlm.hold_enter_supported(_C("1.26.0", set()))
    assert tlm.hold_enter_supported(_C("", {"sequence-hold-enter"}))   # unknown version → cap wins
    assert tlm.uses_hold_enter([_bar(), _ramp(-10.0, anchor="enter")])
    assert not tlm.uses_hold_enter([_bar(), _ramp(-10.0)])


def test_collapse_hold_compiles_an_enter_step_out():
    steps = [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="chirp"),
        m.SequenceStep(anchor="start", offset_s=300.0, action="hold", task_name=""),
        m.SequenceStep(anchor="enter", offset_s=-5.0, action="tune", task_name="chirp",
                       params={"bw": 12}),
        m.SequenceStep(anchor="enter", offset_s=-10.0, action="ramp", task_name="chirp",
                       ramp=m.RampSpec(param="power", start=-50.0, stop=-90.0, steps=3,
                                       duration_s=60.0)),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="chirp"),
    ]
    out = m.collapse_hold(steps)
    acts = [m._step_action(s) for s in out]
    assert "hold" not in acts and all(s.anchor != "enter" for s in out)
    tune = next(s for s in out if m._step_action(s) == "tune")
    assert tune.anchor == "start" and tune.offset_s == pytest.approx(295.0)
    ramp = next(s for s in out if m._step_action(s) == "ramp")
    # the ramp's END was 10 s before the pause; by its START (the agent's forward layout) it's 60 s earlier
    assert ramp.anchor == "start" and ramp.offset_s == pytest.approx(230.0)


def test_canvas_offers_the_pause_edge_to_a_ramp_end_and_resume_to_a_start():
    rp = _ramp(100.0)
    cv = _chirp_editor([_bar(), _hold(300.0), _tune(20.0, anchor="hold"), rp])._canvas
    ex, rx = cv._enter_x, cv._resume_x
    assert cv._root_anchor_at(ex, for_end=True) == "enter"
    assert cv._root_anchor_at(ex, for_end=False) is None      # a start / a tune can't tie to the pause
    assert cv._root_anchor_at(rx, for_end=False) == "hold"
    assert cv._root_anchor_at(rx, for_end=True) is None       # a ramp's END can't tie to resume
    cv._make_root_anchor(rp.uid, "enter")
    assert rp.anchor == "enter" and rp.offset == pytest.approx(-140.0)   # it ended 140 s before the pause
    g = cv._geom[rp.uid]
    assert g["stop_x"] == pytest.approx(cv._enter_x + rp.offset * cv._eff())
    assert cv._clamp_tune_offset(rp, 5.0) == 0.0              # it can never reach INTO the pause
    assert cv._end_tied(rp)


def test_dialogs_offer_the_hold_start_anchor_only_with_a_hold():
    t = _tune(35.0)
    dlg = StepEditorDialog(t, _chirp_editor([_bar(), _hold(300.0), t]), new=False)
    _app.processEvents()
    assert dlg._anchor.findData("enter") >= 0
    dlg.close()
    dlg2 = StepEditorDialog(t, _chirp_editor([_bar(), t]), new=False)
    _app.processEvents()
    assert dlg2._anchor.findData("enter") < 0
    dlg2.close()
    rp = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=10.0, ramp={})
    rd = RampEditorDialog(rp, _chirp_editor([_bar(), _hold(300.0), rp]), new=True)
    _app.processEvents()
    assert rd._anchor.findData("enter") >= 0
    rd.close()


# ── #7 — RF auto-gating on a duration-task drag ───────────────────────────────

_RF = {"dest": "rf", "flags": ["--rf"], "type": "str", "choices": ["on", "off"],
       "default": "on", "live": True}


def _rf_editor(items):
    ed = _chirp_editor(items)
    ed._param_specs = {"chirp.py": list(ed._param_specs["chirp.py"]) + [_RF]}
    return ed


def test_rf_gate_helpers():
    assert rf_gate.gate([{"dest": "bw"}, _RF]) is _RF
    marked = {"dest": "tx_enable", "flags": ["--tx-enable"], "is_rf": True,
              "choices": [{"label": "On", "value": "1"}, {"label": "Off", "value": "0"}]}
    assert rf_gate.gate([_RF, marked]) is marked                # the explicit marker wins
    assert rf_gate.gate_tokens(marked) == ("1", "0")
    assert rf_gate.gate_tokens(_RF) == ("on", "off")
    assert rf_gate.gate([{"dest": "rfgain", "flags": ["--rf-gain"], "type": "float"}]) is None
    assert rf_gate.gate_arg_state(["--rf", "off"], _RF) is False
    assert rf_gate.gate_arg_state(["--bw", "10"], _RF) is None
    assert rf_gate.set_gate_arg(["--rf", "off"], _RF, True) == ["--rf", "on"]
    assert rf_gate.set_gate_arg(["--bw", "10"], _RF, False) == ["--bw", "10", "--rf", "off"]


def test_task_start_before_on_air_mutes_the_launch_and_adds_an_rf_on_tune():
    bar = _bar(); bar.stop_offset = 0.0
    cv = _rf_editor([bar])._canvas
    bar.start_offset = -5.0
    assert cv._auto_rf_gate(bar) is True
    assert rf_gate.gate_arg_state(bar.args, _RF) is False        # launches muted
    on = _tunes(cv, {"rf": "on"})
    assert len(on) == 1 and on[0].anchor == "start" and on[0].offset == 0.0
    assert cv._auto_rf_gate(bar) is False                        # idempotent — no second tune
    # dragged back to on-air (or later): the tune goes and the launch gate is restored
    bar.start_offset = 0.0
    assert cv._auto_rf_gate(bar) is True
    assert rf_gate.gate_arg_state(bar.args, _RF) is True
    assert _tunes(cv, {"rf": "on"}) == []


def test_task_end_past_off_air_adds_an_rf_off_tune():
    bar = _bar(); bar.stop_offset = 0.0
    cv = _rf_editor([bar])._canvas
    assert cv._auto_rf_gate(bar) is False                        # nothing to do at the edges
    bar.stop_offset = 4.0
    assert cv._auto_rf_gate(bar) is True
    off = _tunes(cv, {"rf": "off"})
    assert len(off) == 1 and off[0].anchor == "stop" and off[0].offset == 0.0
    bar.stop_offset = 0.0
    assert cv._auto_rf_gate(bar) is True
    assert _tunes(cv, {"rf": "off"}) == []


def test_a_script_without_an_rf_gate_is_left_alone():
    bar = _bar(); bar.start_offset = -5.0; bar.stop_offset = 0.0
    cv = _chirp_editor([bar])._canvas                            # the chirp specs carry no gate
    assert cv._auto_rf_gate(bar) is False
    assert len(cv._items) == 1


def test_releasing_a_bar_drag_runs_the_auto_gate_in_the_same_undo_step():
    bar = _bar(); bar.stop_offset = 0.0
    cv = _rf_editor([bar])._canvas
    undo0 = copy.deepcopy(cv._items)
    bar.start_offset = -5.0                                      # as the start-grip drag left it
    cv._drag = {"item": bar, "part": "bar_start", "press_x": 0.0, "moved": True,
                "start0": 0.0, "stop0": 0.0, "off0": 0.0, "undo0": undo0, "collapse": None,
                "group": None, "group0": {}}
    pos = QPointF(cv._on - 15.0, cv._geom[bar.uid]["y"] + LANE_H / 2)
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, pos, pos, Qt.MouseButton.LeftButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    cv.mouseReleaseEvent(ev)
    assert cv._drag is None
    assert len(_tunes(cv, {"rf": "on"})) == 1
    assert rf_gate.gate_arg_state(bar.args, _RF) is False
    cv.undo()                                                    # ONE undo: the drag AND the gate
    assert len(cv._items) == 1 and cv._items[0].start_offset == 0.0
