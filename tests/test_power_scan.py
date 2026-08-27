"""Scan a library for absolute --power levels and range-check them against a unit's
calibration (deploy-time 'this unit will clip that level' informing). Pure logic."""
from types import SimpleNamespace as NS

from state.power_scan import (
    extract_power, scan_absolute_power, power_out_of_range,
    extract_amplitude, scan_amplitudes, amplitude_mismatch, clip_library_power,
)
from api import models as m


def test_extract_power_finds_flag():
    assert extract_power(["--prn", "5", "--power", "24.0"]) == 24.0
    assert extract_power(["-Power", "-3"]) == -3.0
    assert extract_power(["--gain", "60"]) is None
    assert extract_power(["--power"]) is None                # flag with no value
    assert extract_power(["--power", "abc"]) is None         # unparseable


def _lib():
    tasks = [
        NS(name="beacon", env={"SDR_CAL_SIGNAL_ID": "l1"}, command=["python", "b.py", "--power", "28"]),
        NS(name="chirp", env={"SDR_CAL_SIGNAL_ID": "l5"}, command=["python", "c.py", "--gain", "60"]),
        NS(name="uncal", env={}, command=["python", "u.py", "--power", "10"]),
    ]
    seqs = [NS(name="sweep", steps=[
        NS(task_name="beacon", args=["--power", "30"]),
        NS(task_name="beacon", args=["--gain", "70"]),
    ])]
    return NS(tasks=tasks, sequences=seqs)


def test_scan_collects_task_and_step_levels():
    got = scan_absolute_power(_lib())
    wheres = {w: (sid, dbm) for w, sid, dbm in got}
    assert wheres["task beacon"] == ("l1", 28.0)
    assert wheres["task uncal"] == (None, 10.0)              # no signal → can't range-check
    assert wheres["sequence sweep · step 1 (beacon)"] == ("l1", 30.0)
    assert "task chirp" not in wheres                        # --gain, not absolute power


def _cal(sig_bounds):
    return {"signals": {sid: {"min_power_dbm": lo, "max_power_dbm": hi}
                        for sid, (lo, hi) in sig_bounds.items()}}


def test_out_of_range_flags_above_and_below():
    levels = scan_absolute_power(_lib())
    cal = _cal({"l1": (-30.0, 24.0)})                        # l5/uncal not calibrated here
    warns = power_out_of_range(levels, cal)
    where = {w["where"]: w for w in warns}
    assert where["task beacon"]["side"] == "above" and where["task beacon"]["limit"] == 24.0
    assert where["sequence sweep · step 1 (beacon)"]["dbm"] == 30.0
    assert "task uncal" not in where                         # signal not calibrated → skipped


def test_out_of_range_below_minimum():
    levels = [("task quiet", "l1", -50.0)]
    warns = power_out_of_range(levels, _cal({"l1": (-30.0, 24.0)}))
    assert warns[0]["side"] == "below" and warns[0]["limit"] == -30.0


def test_in_range_is_silent():
    levels = [("task ok", "l1", 20.0)]
    assert power_out_of_range(levels, _cal({"l1": (-30.0, 24.0)})) == []


# ── amplitude scan + mismatch ───────────────────────────────────────────────────

def test_extract_amplitude():
    assert extract_amplitude(["--power", "20", "--amplitude", "0.5"]) == 0.5
    assert extract_amplitude(["-Amplitude", "0.3"]) == 0.3
    assert extract_amplitude(["--power", "20"]) is None


def _amp_lib():
    tasks = [
        NS(name="beacon", env={"SDR_CAL_SIGNAL_ID": "l1"},
           command=["python", "b.py", "--amplitude", "0.5"]),
        NS(name="uncal", env={}, command=["python", "u.py", "--amplitude", "0.9"]),
    ]
    seqs = [NS(name="sweep", steps=[NS(task_name="beacon", args=["--amplitude", "0.8"])])]
    return NS(tasks=tasks, sequences=seqs)


def _cal_amp(sig_amp):
    return {"signals": {sid: {"amplitude": a} for sid, a in sig_amp.items()}}


def test_scan_amplitudes_collects_task_and_step():
    got = {w: (sid, a) for w, sid, a in scan_amplitudes(_amp_lib())}
    assert got["task beacon"] == ("l1", 0.5)
    assert got["sequence sweep · step 1 (beacon)"] == ("l1", 0.8)


def test_amplitude_mismatch_flags_differences():
    warns = amplitude_mismatch(scan_amplitudes(_amp_lib()), _cal_amp({"l1": 0.8}))
    where = {w["where"]: w for w in warns}
    # beacon task set 0.5 but the curve was measured at 0.8 → flagged
    assert where["task beacon"]["amp"] == 0.5 and where["task beacon"]["cal_amp"] == 0.8
    # the sequence step set 0.8, which matches → not flagged
    assert "sequence sweep · step 1 (beacon)" not in where
    # the uncal task has no signal → skipped
    assert "task uncal" not in where


def test_amplitude_mismatch_skips_when_calibration_records_none():
    warns = amplitude_mismatch([("task x", "l1", 0.5)], {"signals": {"l1": {}}})
    assert warns == []


# ── deploy-time clip: rewrite the DEPLOYED task's --power to the unit's range ─────────

def _real_lib():
    return m.Library(
        tasks=[
            m.TaskConfig(name="beacon", command=["python", "b.py", "--power", "40"],
                         env={"SDR_CAL_SIGNAL_ID": "l1"}),        # above max → clip down
            m.TaskConfig(name="low", command=["python", "l.py", "--power", "-30"],
                         env={"SDR_CAL_SIGNAL_ID": "l1"}),        # below min → clamp up
            m.TaskConfig(name="fine", command=["python", "f.py", "--power", "10"],
                         env={"SDR_CAL_SIGNAL_ID": "l1"}),        # in range → untouched
            m.TaskConfig(name="uncal", command=["python", "u.py", "--power", "99"],
                         env={}),                                  # no signal → untouched
        ],
        sequences=[m.Sequence(id="s1", name="sweep", steps=[
            m.SequenceStep(offset_s=0.0, action=m.StepAction.RUN, task_name="beacon",
                           args=["--power", "50"]),               # above max → clip down
        ])],
    )


def test_clip_library_power_clamps_deployed_tasks():
    cal = _cal({"l1": (-1.8, 28.2)})
    clipped, adjustments = clip_library_power(_real_lib(), cal)
    by = {t.name: t.command for t in clipped.tasks}
    assert by["beacon"][-1] == "28.2"          # 40 → max
    assert by["low"][-1] == "-1.8"             # -30 → min
    assert by["fine"][-1] == "10"              # untouched
    assert by["uncal"][-1] == "99"             # not calibrated → untouched
    assert clipped.sequences[0].steps[0].args[-1] == "28.2"     # step 50 → max
    wheres = {a["where"]: a["to"] for a in adjustments}
    assert wheres["task beacon"] == 28.2 and wheres["task low"] == -1.8
    assert "task fine" not in wheres and "task uncal" not in wheres


def test_clip_library_power_does_not_mutate_the_original():
    lib = _real_lib()
    clip_library_power(lib, _cal({"l1": (-1.8, 28.2)}))
    assert lib.tasks[0].command[-1] == "40"    # original library untouched (deep copy)
