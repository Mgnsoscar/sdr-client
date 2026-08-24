"""Scan a library for absolute --power levels and range-check them against a unit's
calibration (deploy-time 'this unit will clip that level' informing). Pure logic."""
from types import SimpleNamespace as NS

from state.power_scan import extract_power, scan_absolute_power, power_out_of_range


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
