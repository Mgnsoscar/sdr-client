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


# ── Edit-while-holding: the remainder as its own post-hold ramp ───────────────

def _wire_seq(ramp_at=280.0, hold_at=300.0, steps=3, duration=60.0, mode="tune"):
    spec = m.RampSpec(param="power", start=-50.0, stop=-90.0, steps=steps, duration_s=duration,
                      mode=mode, flag="--power" if mode == "run" else None)
    return [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="chirp"),
        m.SequenceStep(anchor="start", offset_s=ramp_at, action="ramp", task_name="chirp",
                       ramp=spec, power_view="psd_live", id="rmp"),
        m.SequenceStep(anchor="start", offset_s=hold_at, action="hold", task_name=""),
        m.SequenceStep(anchor="hold", offset_s=20.0, action="tune", task_name="chirp",
                       params={"bw": 12}),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="chirp"),
    ]


def _fires(step, base=0.0):
    """[(time, value)] a wire ramp step fires at (start layout), via the drift-guarded api.ramp."""
    from api import ramp as _ramp
    r = step.ramp
    res = _ramp.resolve_ramp(r.start, r.stop, steps=r.steps, step=r.step, hold_s=r.hold_s,
                             duration_s=r.duration_s, include_first=r.include_first,
                             include_last=r.include_last)
    return [(round(base + off, 6), round(v, 6))
            for (_a, off, v) in _ramp.place_ramp("start", float(step.offset_s), res)]


def test_split_ramps_at_hold_presents_the_remainder_as_a_post_hold_ramp():
    steps = _wire_seq()          # points 280 (−50), 295 (−63.3), 310 (−76.7), 325 (−90); pause @ 300
    original = _fires(steps[1])
    out = m.split_ramps_at_hold(steps)
    assert not tlm.ramp_crosses_hold_steps(out)                 # nothing crosses any more
    ramps = [s for s in out if m._step_action(s) == "ramp"]
    assert len(ramps) == 2
    a, b = ramps
    # the run-up: exactly the points that fired before the pause, still window A
    assert a.anchor == "start" and a.offset_s == 280.0 and a.id == "rmp"
    assert _fires(a) == original[:2]
    # the remainder: its own post-hold ramp, defaulting to what resuming would have produced
    assert b.anchor == "hold" and b.offset_s == pytest.approx(10.0)      # 310 − 300
    assert b.ramp.start == pytest.approx(-76.666667) and b.ramp.stop == -90.0
    assert b.ramp.steps == 1 and b.ramp.duration_s == pytest.approx(30.0)
    assert b.power_view == "psd_live" and b.id == ""                     # a NEW step, not the target
    assert _fires(b, base=300.0) == original[2:]                         # identical fire times/levels
    # the other steps ride through untouched, in order
    assert [m._step_action(s) for s in out] == ["start", "ramp", "ramp", "hold", "tune", "stop"]
    assert out[0] is steps[0] and out[3] is steps[2] and out[-1] is steps[-1]


def test_split_ramps_at_hold_lone_points_become_tunes():
    # points 270, 285, 300 | 315 → the remainder is one level: a post-hold TUNE 15 s after the pause
    out = m.split_ramps_at_hold(_wire_seq(ramp_at=270.0))
    tail = [s for s in out if s.anchor == "hold" and m._step_action(s) == "tune"
            and "power" in (s.params or {})]
    assert len(tail) == 1 and tail[0].offset_s == pytest.approx(15.0)
    assert tail[0].params == {"power": -90.0} and tail[0].ramp is None
    # points 295 | 310, 325, 340 → the run-up is one level: a window-A tune at 295
    out = m.split_ramps_at_hold(_wire_seq(ramp_at=295.0))
    head = next(s for s in out if s.anchor == "start" and m._step_action(s) == "tune")
    assert head.offset_s == 295.0 and head.params == {"power": -50.0}
    rem = next(s for s in out if s.anchor == "hold" and m._step_action(s) == "ramp")
    assert rem.offset_s == pytest.approx(10.0) and rem.ramp.steps == 2
    assert _fires(rem, base=300.0) == _fires(_wire_seq(ramp_at=295.0)[1])[1:]
    # a run-mode ramp's lone remainder is the one-shot run that point is
    out = m.split_ramps_at_hold(_wire_seq(ramp_at=270.0, mode="run"))
    run = next(s for s in out if s.anchor == "hold" and m._step_action(s) == "run")
    assert run.args[-2:] == ["--power", "-90"] and run.replace_args


def test_split_ramps_at_hold_leaves_non_crossing_sequences_alone():
    steps = _wire_seq(ramp_at=100.0)                             # ends at 160: before the pause
    out = m.split_ramps_at_hold(steps)
    assert [s is t for s, t in zip(out, steps)] == [True] * len(steps)
    free = [s for s in _wire_seq() if m._step_action(s) != "hold"]
    assert m.split_ramps_at_hold(free) == free                   # no Hold: unchanged


def test_hold_edit_dialog_loads_a_crossing_ramp_split():
    from PyQt6.QtCore import QObject, pyqtSignal
    from ui.hold_edit_dialog import HoldEditDialog

    class _EditHub(QObject):
        task_done = pyqtSignal(str, object)

        def __init__(self):
            super().__init__()
            client = type("C", (), {
                "list_tasks": lambda self_: [type("T", (), {"name": "chirp"})()],
                "get_tasks_yaml": lambda self_: ("tasks:\n  - name: chirp\n"
                                                 "    command: [python3, chirp.py]\n"),
                "get_calibration": lambda self_: {"unit_type": "broadcaster", "valid": True,
                                                  "signals": {}},
            })()
            self.fleet = type("F", (), {"get": lambda self_, h: client})()

        def run_async(self, label, fn):
            try:
                res = fn()
            except Exception as exc:                         # noqa: BLE001
                res = exc
            self.task_done.emit(label, res)

    seq = m.Sequence(id="s1", name="cross", steps=_wire_seq())
    dlg = HoldEditDialog(_EditHub(), "unit", seq)
    _app.processEvents()
    loaded = dlg._timeline.steps()
    assert not tlm.ramp_crosses_hold_steps(loaded)               # split on load
    rem = [s for s in loaded if s.anchor == "hold" and m._step_action(s) == "ramp"]
    assert len(rem) == 1 and rem[0].offset_s == pytest.approx(10.0)
    assert rem[0].ramp.stop == -90.0                             # retargetable like any window-B step
    dlg._accept()
    assert dlg.result_steps is not None and any(s.anchor == "hold" for s in dlg.result_steps)
    dlg.close()
