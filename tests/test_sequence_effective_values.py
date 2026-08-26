"""Carry-forward of parameter state across a sequence: the effective --freq / --power a
task is running with when a given step fires — the base for folding the --power range and
the clamp warning at the frequency actually in effect at that offset."""
from ui.timeline_model import BarItem, RunItem, sequence_effective_values

# A chirp script's freq/power params (flags → dest), enough for the carry-forward.
_SPECS = [
    {"dest": "freq", "flags": ["-Frequency", "--freq"]},
    {"dest": "power", "flags": ["-Power", "--power"]},
]


def test_base_args_seed_the_state():
    bar = BarItem(task_name="chirp", args=["--freq", "1.0e9", "--power", "20"],
                  start_offset=0.0, stop_offset=0.0)
    tune = RunItem(task_name="chirp", action="tune", anchor="start", offset=30.0,
                   params={"freq": 2.0e9})
    # entering the tune step, the task is still running the bar's freq/power
    state = sequence_effective_values([bar, tune], "chirp", list(bar.args), _SPECS,
                                      target_uid=tune.uid, target_key=(0, 30.0))
    assert state == {"freq": 1.0e9, "power": 20.0}


def test_earlier_tune_steps_carry_forward():
    bar = BarItem(task_name="chirp", args=["--freq", "1.0e9", "--power", "20"])
    t1 = RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                 params={"freq": 1.5e9})
    t2 = RunItem(task_name="chirp", action="tune", anchor="start", offset=20.0,
                 params={"power": 15.0})
    target = RunItem(task_name="chirp", action="tune", anchor="start", offset=30.0,
                     params={"freq": 2.0e9})
    state = sequence_effective_values([bar, t1, t2, target], "chirp", list(bar.args),
                                      _SPECS, target_uid=target.uid, target_key=(0, 30.0))
    # freq walked 1.0 → 1.5 (t1); power 20 → 15 (t2); target's own freq not yet applied
    assert state == {"freq": 1.5e9, "power": 15.0}


def test_later_steps_do_not_leak_backward():
    bar = BarItem(task_name="chirp", args=["--freq", "1.0e9", "--power", "20"])
    later = RunItem(task_name="chirp", action="tune", anchor="start", offset=99.0,
                    params={"power": 0.0})
    target = RunItem(task_name="chirp", action="tune", anchor="start", offset=30.0,
                     params={"freq": 2.0e9})
    state = sequence_effective_values([bar, later, target], "chirp", list(bar.args),
                                      _SPECS, target_uid=target.uid, target_key=(0, 30.0))
    assert state == {"freq": 1.0e9, "power": 20.0}         # the offset-99 step is after us


def test_other_tasks_are_ignored():
    bar = BarItem(task_name="chirp", args=["--freq", "1.0e9", "--power", "20"])
    other = BarItem(task_name="beacon", args=["--freq", "9.0e9", "--power", "0"])
    target = RunItem(task_name="chirp", action="tune", anchor="start", offset=30.0,
                     params={"freq": 2.0e9})
    state = sequence_effective_values([bar, other, target], "chirp", list(bar.args),
                                      _SPECS, target_uid=target.uid, target_key=(0, 30.0))
    assert state == {"freq": 1.0e9, "power": 20.0}
