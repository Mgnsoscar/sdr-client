"""A ramp that CROSSES the Hold is PAUSED there (docs/sequence-hold-step.md §5.7; agent 1.27.0,
capability sequence-hold-ramp-pause).

Client side: detection (`ramp_hold_cross` / `ramp_crosses_hold` / the wire-level
`ramp_crosses_hold_steps`), the level held through the pause (`ramp_level_at_pause`), the
capability gate, the canvas geometry (the ramp's END sits on the RESUME side of the Hold window and
the capsule is drawn as two pieces flanking the band), and the tooltip ("pauses X in, holding L ·
ends Y after resume").
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui import timeline_model as tlm
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _ramp(off, anchor="start", dur=60.0):
    # −50 → −90 over 60 s in 3 steps: 4 levels × 15 s (−50, −63.3, −76.7, −90)
    return tlm.RunItem(task_name="chirp", action="ramp", anchor=anchor, offset=off,
                       ramp={"param": "power", "start": -50.0, "stop": -90.0, "steps": 3,
                             "duration_s": dur})


def _hold(off=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=off)


# ── Detection ────────────────────────────────────────────────────────────────

def test_ramp_hold_cross_detects_only_a_window_a_ramp_spanning_the_pause():
    assert tlm.ramp_hold_cross(_ramp(270.0), 300.0) == (270.0, 330.0)
    assert tlm.ramp_hold_cross(_ramp(300.0), 300.0) == (300.0, 360.0)   # starting AT the pause
    assert tlm.ramp_hold_cross(_ramp(200.0), 300.0) is None              # ends at 260: before it
    assert tlm.ramp_hold_cross(_ramp(0.0, anchor="hold"), 300.0) is None   # window B
    assert tlm.ramp_hold_cross(_ramp(-10.0, anchor="enter"), 300.0) is None  # ends at the pause
    assert tlm.ramp_hold_cross(_ramp(270.0), None) is None               # no Hold
    assert tlm.ramp_crosses_hold([_bar(), _hold(300.0), _ramp(270.0)])
    assert not tlm.ramp_crosses_hold([_bar(), _hold(300.0), _ramp(200.0)])
    assert not tlm.ramp_crosses_hold([_bar(), _ramp(270.0)])


def test_ramp_level_at_pause_is_the_last_point_fired_before_it():
    # points at 270 (−50), 285 (−63.3), 300 (−76.7), 315 (−90): at the pause the level is −76.7
    assert tlm.ramp_level_at_pause(_ramp(270.0), 300.0) == pytest.approx(-76.6667, abs=1e-3)
    assert tlm.ramp_level_at_pause(_ramp(280.0), 300.0) == pytest.approx(-63.3333, abs=1e-3)
    assert tlm.ramp_level_at_pause(_ramp(200.0), 300.0) is None


def test_wire_level_crossing_detection():
    steps = [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="chirp"),
        m.SequenceStep(anchor="start", offset_s=270.0, action="ramp", task_name="chirp",
                       ramp=m.RampSpec(param="power", start=-50.0, stop=-90.0, steps=3,
                                       duration_s=60.0)),
        m.SequenceStep(anchor="start", offset_s=300.0, action="hold", task_name=""),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="chirp"),
    ]
    assert tlm.ramp_crosses_hold_steps(steps)
    assert tlm.ramp_crosses_hold_steps([s.model_dump() for s in steps])      # dict form too
    early = list(steps); early[1] = steps[1].model_copy(update={"offset_s": 200.0})
    assert not tlm.ramp_crosses_hold_steps(early)
    assert not tlm.ramp_crosses_hold_steps([s for s in steps if m._step_action(s) != "hold"])
    assert not tlm.ramp_crosses_hold_steps([])


def test_hold_ramp_pause_gate():
    class _C:
        def __init__(self, ver, caps):
            self.agent_version = ver; self._caps = caps

        def supports(self, cap):
            return cap in self._caps

    cap = {"sequence-hold-ramp-pause"}
    assert tlm.hold_ramp_pause_supported(_C("1.27.0", cap))
    assert not tlm.hold_ramp_pause_supported(_C("1.26.0", cap))
    assert not tlm.hold_ramp_pause_supported(_C("1.27.0", set()))
    assert tlm.hold_ramp_pause_supported(_C("", cap))          # unknown version → the cap wins


# ── Canvas ────────────────────────────────────────────────────────────────────

def test_canvas_puts_a_crossing_ramps_end_on_the_resume_side_and_splits_the_capsule():
    rp = _ramp(270.0)
    cv = _chirp_editor([_bar(), _hold(300.0), rp])._canvas
    g = cv._geom[rp.uid]; eff = cv._eff()
    assert g["cut_x"] == pytest.approx(cv._enter_x)
    assert g["start_x"] == pytest.approx(cv._on + 270.0 * eff)        # the run-up: window A
    assert g["stop_x"] == pytest.approx(cv._resume_x + 30.0 * eff)     # the END: 30 s after RESUME
    fwd, _bwd = cv._post_hold_extents()
    assert fwd == pytest.approx(30.0)                                  # off-air floats past it
    # a ramp fully before the pause is untouched
    rp2 = _ramp(100.0); cv.add_item(rp2)
    assert cv._geom[rp2.uid]["cut_x"] is None
    assert cv._geom[rp2.uid]["stop_x"] == pytest.approx(cv._on + 160.0 * eff)
    # painting: TWO capsules — one ending at the enter edge, one starting at the resume edge
    rects = []
    cv._capsule = lambda p, rect, *a, **k: rects.append((rect.left(), rect.right()))
    pm = QPixmap(1400, 300); p = QPainter(pm)
    try:
        cv._paint_ramp(p, rp)
        n_split = len(rects)
        rects.clear()
        cv._paint_ramp(p, rp2)
        n_plain = len(rects)
    finally:
        p.end()
    assert n_plain == 1
    assert n_split == 2


def test_split_capsule_pieces_flank_the_hold_window():
    rp = _ramp(270.0)
    cv = _chirp_editor([_bar(), _hold(300.0), rp])._canvas
    rects = []
    cv._capsule = lambda p, rect, *a, **k: rects.append((rect.left(), rect.right()))
    pm = QPixmap(1400, 300); p = QPainter(pm)
    try:
        cv._paint_ramp(p, rp)
    finally:
        p.end()
    (l1, r1), (l2, r2) = sorted(rects)
    assert r1 == pytest.approx(cv._enter_x)                 # the run-up ends at the pause
    assert l2 == pytest.approx(cv._resume_x)                # the remainder starts at resume
    assert r2 == pytest.approx(cv._geom[rp.uid]["stop_x"])


def test_live_drag_into_the_hold_splits_and_out_of_it_rejoins():
    rp = _ramp(200.0)                                        # ends at 260: not crossing
    cv = _chirp_editor([_bar(), _hold(300.0), rp])._canvas
    assert cv._geom[rp.uid]["cut_x"] is None
    eff = cv._eff()
    rp.offset = 280.0                                        # dragged so it ends at 340
    cv._drag = {"item": rp, "part": "ramp_body", "moved": True}
    cv._live_move(rp)
    assert cv._geom[rp.uid]["cut_x"] == pytest.approx(cv._enter_x)
    assert cv._geom[rp.uid]["stop_x"] == pytest.approx(cv._resume_x + 40.0 * eff)
    rp.offset = 150.0                                        # dragged back out
    cv._live_move(rp)
    cv._drag = None
    assert cv._geom[rp.uid]["cut_x"] is None
    assert cv._geom[rp.uid]["stop_x"] == pytest.approx(cv._on + 210.0 * eff)


def test_tooltip_reads_the_pause_and_the_resume_for_a_crossing_ramp():
    rp = _ramp(270.0)
    cv = _chirp_editor([_bar(), _hold(300.0), rp])._canvas
    txt = cv._tooltip_text(rp)
    assert "<b>Ramp</b> · power -50→-90 · 1 min" in txt      # its OWN duration — the pause isn't counted
    assert "starts 4 min, 30 s after on-air" in txt
    assert "pauses 30 s in, holding -76" in txt
    assert "ends 30 s after resume" in txt
    assert "ends at off-air" in txt                          # off-air floats to the ramp's resumed end
    # a ramp that doesn't cross keeps the plain lines
    rp2 = _ramp(100.0); cv.add_item(rp2)
    t2 = cv._tooltip_text(rp2)
    assert "pauses" not in t2 and "starts 1 min, 40 s after on-air" in t2
