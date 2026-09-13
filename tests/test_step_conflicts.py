"""Sequence step-conflict validation (save/arm gate in timeline_model.validate).

Rules enforced:
  A  a tune/ramp must fire INSIDE its parent duration task's on-air span;
  B  two tunes can't set the same parameter at the same time;
  C  a tune can't set a parameter a ramp is sweeping over an overlapping span;
  D  two ramps can't sweep the same parameter over overlapping spans;
  E  a task can be started at most once (no second duration bar).
--power and --gain are one control ("the output level"), so a tune/ramp on either
conflicts with a tune/ramp on the other.
"""
from ui import timeline_model as tlm
from ui.timeline_model import RunItem, BarItem

TASKS = ["tx"]


def _bar(task="tx", start=0.0, stop=0.0):
    return BarItem(task_name=task, start_offset=start, stop_offset=stop)


def _tune(off, param="bw", val=12, anchor="start", task="tx"):
    return RunItem(task_name=task, action="tune", anchor=anchor, offset=off,
                   params={param: val})


def _ramp(off, param="power", anchor="start", task="tx", dur_steps=3, dur=6.0,
          offset_end=0.0):
    return RunItem(task_name=task, action="ramp", anchor=anchor, offset=off,
                   offset_end=offset_end,
                   ramp={"param": param, "start": 0.0, "stop": 9.0,
                         "steps": dur_steps, "duration_s": dur})


def _ok(items):
    return tlm.validate(items, TASKS)


# ── baseline ────────────────────────────────────────────────────────────────

def test_a_lone_bar_and_a_simple_tune_are_valid():
    assert _ok([_bar()]) is None
    assert _ok([_bar(), _tune(5.0, "bw")]) is None


# ── Rule A: within the parent task's window ───────────────────────────────────

def test_a_start_tune_before_the_task_start_is_rejected():
    # bar starts at on-air+10; a start tune at +2 fires before the task is running.
    err = _ok([_bar(start=10.0), _tune(2.0, "bw")])
    assert err and "before the task goes on air" in err


def test_a_start_tune_at_or_after_the_task_start_is_ok():
    assert _ok([_bar(start=10.0), _tune(10.0, "bw")]) is None
    assert _ok([_bar(start=10.0), _tune(25.0, "bw")]) is None


def test_a_stop_tune_after_off_air_is_rejected():
    # a stop-anchored tune at +5 fires AFTER off-air (stop offset must be ≤ the bar's stop = 0).
    err = _ok([_bar(), _tune(5.0, "bw", anchor="stop")])
    assert err and "after the task goes off air" in err


# ── Rule E: one duration bar per task ─────────────────────────────────────────

def test_two_bars_for_the_same_task_are_rejected():
    err = _ok([_bar(), _bar()])
    assert err and "started 2 times" in err


def test_two_bars_for_different_tasks_are_ok():
    assert tlm.validate([_bar("tx"), _bar("cw")], ["tx", "cw"]) is None


# ── Rule B: two tunes, same param, same time ──────────────────────────────────

def test_two_tunes_same_param_same_time_rejected():
    err = _ok([_bar(), _tune(5.0, "bw"), _tune(5.0, "bw")])
    assert err and "same time" in err and "bw" in err


def test_two_tunes_same_param_different_time_ok():
    assert _ok([_bar(), _tune(5.0, "bw"), _tune(9.0, "bw")]) is None


def test_two_tunes_different_param_same_time_ok():
    assert _ok([_bar(), _tune(5.0, "bw"), _tune(5.0, "sidelobes")]) is None


# ── Rule C: a tune vs a ramp on the same control ──────────────────────────────

def test_tune_inside_a_both_ramp_on_the_same_param_rejected():
    # a window-filling ramp on power overlaps everything → a power tune anywhere conflicts.
    err = _ok([_bar(), _ramp(0.0, "power", anchor="both"), _tune(5.0, "power")])
    assert err and "output level" in err


def test_power_tune_conflicts_with_a_gain_ramp_output_level():
    # power and gain are the same output level → a power tune inside a gain ramp's span conflicts.
    err = _ok([_bar(), _ramp(0.0, "gain"), _tune(2.0, "power")])   # ramp spans [0, ~4.5]
    assert err and "output level" in err


def test_a_tune_outside_the_ramp_span_is_ok():
    # ramp on power spans [0, ~4.5] from on-air; a power tune well after it doesn't overlap.
    assert _ok([_bar(), _ramp(0.0, "power"), _tune(60.0, "power")]) is None


def test_a_tune_on_a_different_param_than_the_ramp_is_ok():
    assert _ok([_bar(), _ramp(0.0, "power", anchor="both"), _tune(5.0, "sidelobes")]) is None


# ── Rule D: two ramps on the same control ─────────────────────────────────────

def test_two_both_ramps_on_the_same_control_rejected():
    err = _ok([_bar(), _ramp(0.0, "power", anchor="both"),
               _ramp(0.0, "gain", anchor="both")])
    assert err and "output level" in err


def test_two_ramps_on_different_params_ok():
    assert _ok([_bar(), _ramp(0.0, "power", anchor="both"),
                _ramp(0.0, "bw", anchor="both")]) is None


# ── arm-time data path: a stored sequence's wire steps round-trip and still flag ───

def test_wire_steps_roundtrip_flags_a_conflict_at_arm():
    """The Library arm guard converts a stored sequence's SequenceStep list to items
    (model_dump → steps_to_items) then validate()s it — a conflict must survive the trip."""
    from api import models as m
    steps = [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="tx"),
        m.SequenceStep(anchor="start", offset_s=5.0, action="tune", task_name="tx",
                       params={"bw": 12}),
        m.SequenceStep(anchor="start", offset_s=5.0, action="tune", task_name="tx",
                       params={"bw": 20}),
    ]
    items = tlm.steps_to_items([s.model_dump(mode="json") for s in steps])
    err = tlm.validate(items)
    assert err and "same time" in err

    # a clean sequence survives the trip as valid
    clean = [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="tx"),
        m.SequenceStep(anchor="start", offset_s=5.0, action="tune", task_name="tx",
                       params={"bw": 12}),
    ]
    assert tlm.validate(tlm.steps_to_items([s.model_dump(mode="json") for s in clean])) is None
