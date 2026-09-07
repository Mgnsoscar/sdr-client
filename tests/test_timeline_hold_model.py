"""Phase 0 of the Hold step (docs/sequence-hold-step.md, Appendix A.3): the client
mirrors the agent's Hold data model and round-trips a Hold through the timeline compile
WITHOUT any canvas rendering (that is Phase 2).

Two guarantees:
  1. A Hold-bearing timeline round-trips losslessly through items_to_steps/steps_to_items
     (the HOLD boundary marker survives, and window-B steps keep anchor="hold").
  2. A Hold-FREE timeline compiles byte-identically to before — the added hold branch is
     inert unless a HOLD is present.
Plus the api-model mirror (SequenceRun/ArmSequenceRequest/ProceedRequest) and the
SEQUENCE_HOLD_CAPABILITY string.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
import api.models as m

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _hold(offset=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=offset)


# ── The capability string (Phase 0 defines it; the gate is Phase 2) ──────────

def test_capability_constant():
    assert tlm.SEQUENCE_HOLD_CAPABILITY == "sequence-hold"


# ── Hold round-trip through the pure compile ─────────────────────────────────

def test_hold_marker_compiles_to_a_hold_step():
    steps = tlm.items_to_steps([_hold(300.0)])
    assert len(steps) == 1
    s = steps[0]
    assert s["action"] == "hold"
    assert s["anchor"] == "start"
    assert s["offset_s"] == 300.0
    assert s["task_name"] == ""          # a boundary marker names no task
    assert s["args"] == []


def test_hold_bearing_timeline_round_trips_losslessly():
    items = [
        tlm.BarItem(task_name="tx", args=["--power", "-40"], start_offset=0.0, stop_offset=0.0),
        tlm.RunItem(task_name="atten", action="run", anchor="start", offset=5.0, args=["--a", "1"]),
        _hold(300.0),                                                    # end of window A
        tlm.RunItem(task_name="tx", action="tune", anchor="hold",       # window B
                    offset=0.0, params={"gain": 10}),
    ]
    steps = tlm.items_to_steps(items)

    # The HOLD step and the anchor="hold" window-B step are present.
    holds = [s for s in steps if s["action"] == "hold"]
    assert len(holds) == 1 and holds[0]["anchor"] == "start" and holds[0]["offset_s"] == 300.0
    wb = [s for s in steps if s["anchor"] == "hold"]
    assert len(wb) == 1 and wb[0]["action"] == "tune" and wb[0]["params"] == {"gain": 10}

    # Round-trip back into items: the marker and the window-B anchor survive (order may
    # differ — bars are regrouped to the end, the pre-existing behavior).
    back = tlm.steps_to_items(steps)
    hold_items = [it for it in back if getattr(it, "action", None) == "hold"]
    assert len(hold_items) == 1
    assert hold_items[0].anchor == "start" and hold_items[0].offset == 300.0
    assert hold_items[0].task_name == ""
    wb_items = [it for it in back if getattr(it, "anchor", None) == "hold"]
    assert len(wb_items) == 1
    assert wb_items[0].action == "tune" and wb_items[0].params == {"gain": 10}


def test_hold_survives_a_second_round_trip():
    # items → steps → items → steps is stable (the marker doesn't drift or duplicate).
    items = [_hold(120.0),
             tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=2.0,
                         params={"gain": 1})]
    once = tlm.items_to_steps(items)
    twice = tlm.items_to_steps(tlm.steps_to_items(once))
    assert [s["action"] for s in once if s["action"] == "hold"] == ["hold"]
    assert once == twice


# ── Regression: a Hold-FREE timeline is byte-identical to before ─────────────

def test_hold_free_timeline_compiles_byte_identically():
    items = [
        tlm.BarItem(task_name="tx", args=["--power", "-40"], start_offset=-30.0, stop_offset=5.0),
        tlm.RunItem(task_name="atten", action="run", anchor="start", offset=0.0, args=["--a", "1"]),
        tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=10.0, params={"gain": 3}),
    ]
    expected = [
        {"anchor": "start", "offset_s": -30.0, "action": "start", "task_name": "tx",
         "args": ["--power", "-40"], "replace_args": True, "inject_resume_offset": False,
         "power_view": None, "power_hold_dest": None},
        {"anchor": "stop", "offset_s": 5.0, "action": "stop", "task_name": "tx",
         "args": [], "replace_args": False},
        {"anchor": "start", "offset_s": 0.0, "action": "run", "task_name": "atten",
         "args": ["--a", "1"], "replace_args": True,
         "power_view": None, "power_hold_dest": None},
        {"anchor": "start", "offset_s": 10.0, "action": "tune", "task_name": "tx",
         "params": {"gain": 3}, "power_view": None, "power_hold_dest": None},
    ]
    assert tlm.items_to_steps(items) == expected


# ── The api-model mirror of the agent additions ──────────────────────────────

def test_step_action_and_state_mirror_the_agent():
    assert m.StepAction("hold") == m.StepAction.HOLD
    assert m.SequenceState("holding") == m.SequenceState.HOLDING


def test_sequence_run_hold_fields_default_off():
    run = m.SequenceRun(id="r", sequence_id="s", sequence_name="n",
                        on_air_at="2026-01-01T00:00:00+00:00")
    assert run.hold_at_offset_s is None
    assert run.held_actual is None
    assert run.resumed_actual is None
    assert run.hold_aware is False
    assert run.max_hold_s == 1800.0


def test_arm_and_proceed_requests():
    req = m.ArmSequenceRequest(on_air_at="2026-01-01T00:00:00+00:00")
    assert req.hold_aware is False and req.max_hold_s == 1800.0
    p = m.ProceedRequest(proceed_at="2026-01-01T00:00:00+00:00")
    assert p.proceed_at == "2026-01-01T00:00:00+00:00" and p.steps is None


def test_hold_sequencestep_round_trips_through_the_model():
    step = m.SequenceStep(anchor="start", offset_s=45.0, action=m.StepAction.HOLD, task_name="")
    again = m.SequenceStep(**step.model_dump())
    assert again.action == m.StepAction.HOLD and again.anchor == "start" and again.offset_s == 45.0


# ── Full editor round-trip (canvas → SequenceStep list → canvas) ─────────────

def test_hold_round_trips_through_the_timeline_editor():
    # Reuses the real chirp fixtures/editor from the carried-bw suite; confirms a Hold
    # survives ed.steps()/ed.set_steps() (the actual deploy/load path), including the
    # hold_control_quantity precompute, with no canvas rendering required.
    from tests.test_step_editor_carried_bw import _editor, _bar
    items = [
        _bar(10),
        _hold(300.0),
        tlm.RunItem(task_name="chirp", action="tune", anchor="hold", offset=0.0,
                    params={"power": -7.38}),
    ]
    ed = _editor(items)
    seq = ed.steps()
    holds = [s for s in seq if s.action == m.StepAction.HOLD]
    assert len(holds) == 1 and holds[0].anchor == "start" and holds[0].offset_s == 300.0
    assert any(s.anchor == "hold" for s in seq)          # window B tagged

    ed.set_steps(seq)                                    # back onto the canvas
    back = ed.items()
    assert sum(1 for it in back if getattr(it, "action", None) == "hold") == 1
    assert any(getattr(it, "anchor", None) == "hold" for it in back)
