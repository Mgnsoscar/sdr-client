"""Sequence-level POWER ACHIEVABILITY (a temporal check).

Whether a commanded --power is deliverable depends on the transmit frequency and the
calibration bridge params in effect *at the moment it fires*. A power ramp's top levels can
therefore become unachievable partway through when a LATER tune step retunes the carrier (or
changes a bridge param) — something a single per-step fold can't express. `timeline_model.
achievability_warnings` walks each task's timeline in fire-time order and flags every ramp
point that will clamp. Warn, never block. See docs/sequence-power-achievability.md.
"""
import math

import pytest

from ui import timeline_model as tlm
from tests.test_live_tune_power import _ENBW, _GPS_ART, _GPS_PARAMS


# ── A frequency-dependent chain: max +5 dBm at 1000 MHz, −5 dBm at 1300 MHz (a −10 dB hop) ──
_FREQ_ART = {
    "anchor_curve": [[40, -60.0], [80, 5.0]], "min_gain_db": 40.0, "max_gain_db": 80.0,
    "gain_ceiling_db": 80.0, "center_freq_hz": 1.0e9,
    "passive_hops": [{"delta_db_by_freq": [[1.0e9, 0.0], [1.3e9, -10.0]]}],
}
_FREQ_SPECS = [
    {"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz", "is_freq": True},
    {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm", "snap_role": "power"},
]
_FREQ_BASE = ["--freq", "1000", "--power", "-20"]


def _freq_resolve(task):
    if task != "tx":
        return None
    return {"artifact": _FREQ_ART, "specs": _FREQ_SPECS, "base_args": _FREQ_BASE,
            "freq_param": "freq", "freq_factor": 1e6, "power_dest": "power"}


def _bar(task="tx", args=None):
    return tlm.BarItem(task_name=task, args=list(args if args is not None else _FREQ_BASE),
                       start_offset=0.0, stop_offset=0.0)


def _power_ramp(task="tx", start=-20.0, stop=0.0, step=2.0, hold=60.0, offset=0.0):
    # step 2 dB from -20..0 → 11 levels (idx 0..10); hold 60 s → fire times 0,60,…,600 s.
    return tlm.RunItem(task_name=task, action="ramp", anchor="start", offset=offset,
                       ramp={"param": "power", "flag": "--power", "start": start, "stop": stop,
                             "step": step, "hold_s": hold})


# ── the owner's scenario: a later retune makes a ramp's top steps unachievable ──────────────

def test_ramp_top_steps_clamp_after_a_midramp_retune():
    # −20→0 dBm ramp over 0–10 min. Fine at the starting 1000 MHz (max +5). A tune at 4:10
    # retunes to 1300 MHz (max −5), so the last steps (−4/−2/0 dBm, at 8:00/9:00/10:00) clamp.
    tune = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=250.0,
                       params={"freq": 1300.0})
    issues = tlm.achievability_warnings([_bar(), _power_ramp(), tune], _freq_resolve)

    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(-5.0, abs=1e-6)          # the folded ceiling at 1300 MHz
    assert iss.freq_hz == pytest.approx(1.3e9)
    # idx 8,9,10 = levels −4,−2,0 dBm, fired at 480/540/600 s — the only points above −5 dBm.
    assert [p[0] for p in iss.points] == [8, 9, 10]
    assert [round(p[1], 2) for p in iss.points] == [-4.0, -2.0, 0.0]
    assert [p[2] for p in iss.points] == [480.0, 540.0, 600.0]
    # the message is specific: steps, ceiling, carrier, level + time span, and the clamp verb.
    m = iss.message
    assert "steps 9–11 of 11" in m
    assert "1300.000 MHz" in m
    assert "-5.00" in m.replace("−", "-")
    assert "8:00–10:00" in m
    assert "clamped down" in m


def test_silent_when_the_retune_keeps_the_ramp_in_range():
    # A retune that stays where every level is deliverable → no warning (no false positives).
    tune = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=250.0,
                       params={"freq": 1000.0})
    assert tlm.achievability_warnings([_bar(), _power_ramp(), tune], _freq_resolve) == []


def test_the_cross_step_retune_is_what_triggers_it():
    # The whole point: WITHOUT the later tune the same ramp is fully achievable; the clamp comes
    # only from a step elsewhere on the timeline (a single per-step fold could never catch this).
    assert tlm.achievability_warnings([_bar(), _power_ramp()], _freq_resolve) == []
    tune = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=250.0,
                       params={"freq": 1300.0})
    assert len(tlm.achievability_warnings([_bar(), _power_ramp(), tune], _freq_resolve)) == 1


def test_constant_chain_is_skipped():
    # A chain that neither moves with frequency nor a bridge param has a fixed range the From/To
    # field already enforces — the temporal pass stays out of it (no duplicate warnings).
    flat = {"curve": [[40, -60.0], [80, 20.0]], "min_gain_db": 40.0, "max_gain_db": 80.0}

    def resolve(task):
        return {"artifact": flat, "specs": _FREQ_SPECS, "base_args": _FREQ_BASE,
                "freq_param": "freq", "freq_factor": 1e6, "power_dest": "power"}

    tune = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=250.0,
                       params={"freq": 1300.0})
    assert tlm.achievability_warnings([_bar(), _power_ramp(), tune], resolve) == []


# ── a BRIDGE-PARAM retune (GPS C/A --sidelobes) also moves the ceiling under a ramp ──────────
# The limiting reading keys on enbw_mhz, a hidden table lookup on --sidelobes: more sidelobes →
# a LOWER density ceiling. A --sidelobes tune mid-ramp therefore clamps the ramp's top steps,
# proving the pass folds through fold_params_from_values (bridge params), not just frequency.

_GPS_BASE = ["--freq", "1227.6", "--power", "-19", "--sidelobes", "0"]


def _gps_resolve(task):
    if task != "gps":
        return None
    return {"artifact": _GPS_ART, "specs": _GPS_PARAMS["params"], "base_args": _GPS_BASE,
            "freq_param": "freq", "freq_factor": 1e6, "power_dest": "power"}


def test_ramp_clamps_when_a_bridge_param_retune_tightens_the_ceiling():
    bar = tlm.BarItem(task_name="gps", args=_GPS_BASE, start_offset=0.0, stop_offset=0.0)
    # density ramp −19 → −18 dBm/MHz, step 0.2 → 6 levels (idx 0..5) at 0,60,…,300 s.
    ramp = tlm.RunItem(task_name="gps", action="ramp", anchor="start", offset=0.0,
                       ramp={"param": "power", "flag": "--power", "start": -19.0, "stop": -18.0,
                             "step": 0.2, "hold_s": 60.0})
    # sidelobes 0 → 2 at 2:10, dropping the ceiling from ≈ −18.00 to ≈ −18.30 dBm/MHz.
    tune = tlm.RunItem(task_name="gps", action="tune", anchor="start", offset=130.0,
                       params={"sidelobes": 2})

    # Without the tune, sidelobes stays 0 (ceiling ≈ −18.0) and the top level (−18.0) is on the
    # edge, not above it → nothing clamps.
    assert tlm.achievability_warnings([bar, ramp], _gps_resolve) == []

    issues = tlm.achievability_warnings([bar, ramp, tune], _gps_resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    exp_ceiling = -18.0 - 10 * math.log10(_ENBW[2] / _ENBW[0])           # ≈ −18.30
    assert iss.bound == pytest.approx(exp_ceiling, abs=0.02)
    # idx 4,5 = −18.2 / −18.0, the levels above the tighter ceiling after the retune.
    assert [p[0] for p in iss.points] == [4, 5]
    assert iss.unit == "dBm/MHz"
    assert "dBm/MHz" in iss.message and "clamped down" in iss.message


# ── HELD --power: a fixed level pushed out of range by a LATER freq/bridge-param change (§5 step 4) ──
# The owner's report: set the spectral density to its MAX, then a later tune doubles the sweep
# bandwidth — the held density is now unachievable (density drops as the sweep widens), so the
# runtime clamps it. No ramp is involved; the walk re-checks the STANDING power on the bw event.

from tests.test_param_form_power_units import _artifact, _density_reported   # noqa: E402

_CHIRP_ART = _artifact(_density_reported())          # spectral-density chirp; density tracks --bw
_CHIRP_SPECS = [
    {"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz", "is_freq": True},
    {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm/MHz", "snap_role": "power"},
    {"dest": "bw", "flags": ["--bw"], "type": "float", "unit": "MHz"},
]
_CHIRP_BASE = ["--freq", "1575.42", "--power", "-22", "--bw", "10"]


def _chirp_resolve(task):
    if task != "chirp":
        return None
    return {"artifact": _CHIRP_ART, "specs": _CHIRP_SPECS, "base_args": _CHIRP_BASE,
            "freq_param": "freq", "freq_factor": 1e6, "power_dest": "power"}


def _chirp_bar():
    return tlm.BarItem(task_name="chirp", args=_CHIRP_BASE, start_offset=0.0, stop_offset=600.0)


def test_held_power_clamps_when_a_later_tune_doubles_the_bandwidth():
    # density max at bw 10 is −16.71 dBm/MHz; doubling to bw 20 drops it to −16.71 − 10·log10(2).
    set_max = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                          params={"power": -16.71})
    double_bw = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                            params={"bw": 20})
    issues = tlm.achievability_warnings([_chirp_bar(), set_max, double_bw], _chirp_resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(-16.71 - 10 * math.log10(2.0), abs=0.02)
    assert iss.points == [(-1, -16.71, 10.0)]           # the held point (−1 = not a ramp step)
    m = iss.message
    assert "held" in m and "clamped down" in m
    assert "-16.71" in m.replace("−", "-")              # the held level, named
    assert "0:10" in m                                  # when it goes out of range
    assert "‘bw’ change to 20" in m                     # what pushed it out


def test_held_power_silent_without_the_later_change():
    # The whole point: the SAME held density is fine until a later step widens the sweep — no tune,
    # no warning (a single per-step fold at author time could never catch the cross-step clamp).
    set_max = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                          params={"power": -16.71})
    assert tlm.achievability_warnings([_chirp_bar(), set_max], _chirp_resolve) == []


def test_held_power_no_warning_when_it_stays_in_range():
    # A density comfortably within range stays achievable after doubling → no false positive.
    low = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                      params={"power": -25.0})
    double_bw = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                            params={"bw": 20})
    assert tlm.achievability_warnings([_chirp_bar(), low, double_bw], _chirp_resolve) == []


def test_held_power_warns_once_not_at_every_later_event():
    # Once flagged, a still-clamped held power must not re-warn at each subsequent event — only the
    # transition into violation is reported.
    set_max = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                          params={"power": -16.71})
    double_bw = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                            params={"bw": 20})
    widen_more = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=15.0,
                             params={"bw": 40})
    issues = tlm.achievability_warnings([_chirp_bar(), set_max, double_bw, widen_more],
                                        _chirp_resolve)
    assert len(issues) == 1                              # one transition, not one per later event


# ── DIRECTLY-SET --power: a tune/run/baseline step commanding a level unachievable HERE ──────────
# The owner's second report: after a step has widened the sweep to 20 MHz, setting the density to
# the bw-10 max (−16.71 dBm/MHz) in a LATER tune step is not deliverable — the runtime clamps it,
# but nothing warned. Unlike held-power (a standing level pushed out by a later change), this is the
# operator's own explicit command, checked against the operating point in effect when it fires.

def test_directly_set_power_clamps_at_the_carried_bandwidth():
    # bar starts at bw 10; a tune widens it to 20 at 0:05; a LATER tune (5:10) sets the density to
    # −16.71 (bw-10's max) — unachievable at bw 20 (max ≈ −19.72), so it clamps. Warn on the command.
    widen = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                        params={"bw": 20})
    set_bad = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=310.0,
                          params={"power": -16.71})
    issues = tlm.achievability_warnings([_chirp_bar(), widen, set_bad], _chirp_resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(-16.71 - 10 * math.log10(2.0), abs=0.02)
    assert iss.points == [(-1, -16.71, 310.0)]           # the commanded point (−1 = not a ramp step)
    m = iss.message
    assert "set to" in m and "clamped down" in m
    assert "-16.71" in m.replace("−", "-")
    assert "5:10" in m                                    # when the command fires


def test_owner_scenario_held_then_re_set_yields_two_warnings():
    # The full report: set the density to bw-10's max, widen the sweep (the HELD level now clamps),
    # then re-command that same max in a still-later tune (the COMMAND clamps too). Both are flagged:
    # one held-power transition, one directly-set command — each at its own time.
    set_max = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=0.0,
                          params={"power": -16.71})
    widen = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                        params={"bw": 20})
    set_again = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=310.0,
                            params={"power": -16.71})
    issues = tlm.achievability_warnings([_chirp_bar(), set_max, widen, set_again], _chirp_resolve)
    assert len(issues) == 2
    held = [i for i in issues if "held" in i.message]
    setc = [i for i in issues if "set to" in i.message]
    assert len(held) == 1 and len(setc) == 1
    assert held[0].points == [(-1, -16.71, 5.0)]         # flagged at the widen event
    assert setc[0].points == [(-1, -16.71, 310.0)]       # flagged at the re-set command


def test_directly_set_power_in_range_is_silent():
    # A density comfortably within range at the carried bandwidth → no warning (no false positive).
    widen = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                        params={"bw": 20})
    set_ok = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=310.0,
                         params={"power": -25.0})
    assert tlm.achievability_warnings([_chirp_bar(), widen, set_ok], _chirp_resolve) == []


def test_directly_set_power_flags_a_run_step_at_its_own_operating_point():
    # A run step (replace_args) re-invokes the task with its OWN args, so its --power is folded at
    # the run step's own --bw — 20 here → −16.71 is above the bw-20 max and clamps.
    run = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=30.0,
                      args=["--freq", "1575.42", "--power", "-16.71", "--bw", "20"])
    issues = tlm.achievability_warnings([_chirp_bar(), run], _chirp_resolve)
    assert len(issues) == 1
    assert issues[0].direction == "high"
    assert "set to" in issues[0].message
    assert issues[0].points == [(-1, -16.71, 30.0)]


# ── small formatting helpers ────────────────────────────────────────────────────────────────

def test_steps_phrase_and_mmss():
    assert tlm._steps_phrase([8, 9, 10], 11) == "steps 9–11 of 11"
    assert tlm._steps_phrase([4], 6) == "step 5 of 6"
    assert tlm._steps_phrase([1, 3], 6) == "steps 2, 4 of 6"
    assert tlm._mmss(0) == "0:00"
    assert tlm._mmss(485) == "8:05"
    assert tlm._mmss(600) == "10:00"


# ── the TimelineEditor surfaces the warning (wiring end-to-end) ──────────────────────────────

def test_timeline_editor_surfaces_the_ramp_clamp_warning():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from ui.timeline_editor import TimelineEditor
    QApplication.instance() or QApplication([])

    ed = TimelineEditor()
    # Seed the caches a step/ramp dialog would normally populate.
    ed._task_commands = {"tx": ["python3", "tx.py", "--freq", "1000", "--power", "-20"]}
    ed._task_signals = {"tx": "sig"}
    ed._param_specs = {"tx.py": _FREQ_SPECS}
    ed._script_cal_freq_params = {"tx.py": "freq"}
    ed._cal_hostname = "unit"
    ed._calibration = {"valid": True, "signals": {"sig": {"artifact": _FREQ_ART}}}

    tune = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=250.0,
                       params={"freq": 1300.0})
    ed._canvas.set_items([_bar(), _power_ramp(), tune])
    ed._update_achievability()

    # (isVisible() is False in a headless, unshown window; assert on the label's text + intent.)
    assert "clamped down" in ed._achv_warn.text()
    assert "1300.000 MHz" in ed._achv_warn.text()
    assert not ed._achv_warn.isHidden()               # explicitly shown by _update_achievability

    # Retune back into range → the warning clears.
    ed._canvas.set_items([_bar(), _power_ramp(),
                          tlm.RunItem(task_name="tx", action="tune", anchor="start",
                                      offset=250.0, params={"freq": 1000.0})])
    ed._update_achievability()
    assert ed._achv_warn.text() == ""
    assert ed._achv_warn.isHidden()                   # explicitly hidden when nothing clamps


def test_timeline_editor_prefetches_params_so_the_banner_shows_without_opening_a_dialog():
    # The owner-reported "I get no warning": a freshly-loaded sequence whose task params haven't been
    # fetched yet (no step/ramp dialog opened) left the banner blank because resolve() had no specs.
    # _update_achievability now PROACTIVELY prefetches each sequence task's script params; when they
    # land the banner appears on its own — here the held-density clamp after a later --bw doubling.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication
    from ui.timeline_editor import TimelineEditor
    QApplication.instance() or QApplication([])

    served = {"params": _CHIRP_SPECS, "calibration_signal": "sig",
              "calibration_freq_param": "freq", "calibration_power_laws": []}

    class _Client:
        def get_script_params(self, script):
            _Hub.fetched.append(script)
            return served

    class _Fleet:
        def get(self, host):
            return _Client()

    class _Hub(QObject):
        task_done = pyqtSignal(str, object)
        fetched: list = []

        def __init__(self):
            super().__init__()
            self.fleet = _Fleet()

        def run_async(self, label, fn):              # synchronous stand-in for the threaded hub
            try:
                res = fn()
            except Exception as exc:                 # noqa: BLE001
                res = exc
            self.task_done.emit(label, res)

    _Hub.fetched = []
    hub = _Hub()
    ed = TimelineEditor()
    ed.set_context(hub, "unit")
    ed._task_commands = {"chirp": ["python3", "chirp.py", "--freq", "1575.42", "--power",
                                   "-22", "--bw", "10"]}
    ed._task_signals = {"chirp": "sig"}
    ed._cal_hostname = "unit"
    ed._calibration = {"valid": True, "signals": {"sig": {"artifact": _CHIRP_ART}}}
    # NOTE: param cache is deliberately EMPTY — no dialog has fetched params.
    assert ed.param_cache() == {}

    set_max = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                          params={"power": -16.71})
    double_bw = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                            params={"bw": 20})
    ed._canvas.set_items([_chirp_bar(), set_max, double_bw])
    ed._update_achievability()

    assert _Hub.fetched == ["chirp.py"]              # prefetched exactly once, on demand
    assert "chirp.py" in ed.param_cache()            # and cached for every later dialog
    assert "held" in ed._achv_warn.text()            # the banner surfaced without any dialog
    assert "clamped down" in ed._achv_warn.text()
    assert not ed._achv_warn.isHidden()

    # A second refresh doesn't re-fetch (already cached) — no request spam.
    ed._update_achievability()
    assert _Hub.fetched == ["chirp.py"]
