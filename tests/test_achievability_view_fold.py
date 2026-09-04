"""HOLD-LIVE-DENSITY temporal warnings on the REAL FM-chirp structure.

The real chirp's operating/BASE quantity is the FIXED-reference measured density — it is
bandwidth-INVARIANT (``param_dependent`` False), so ``PowerFold.bounds_at`` returns the same base
range at every ``--bw``. The bandwidth dependence lives ONLY in the ``psd_live`` VIEW law
(``restates_measurement``), which the operator authors ``--power`` in. So the base fold can never
see a live density going out of range as the sweep widens — the achievability walk must fold the
CONTROLLED view at each event's fire-time ``--bw``.

The walk now does (`resolve()` surfaces the controlling ``view_law``): a held/commanded density is
expressed in the controlled view at the ``--bw`` in effect when it was SET, and re-checked against
the achievable view range (``[base_min+view_delta(bw), base_max+view_delta(bw)]``) at each later
event's ``--bw``. See docs/sequence-power-achievability.md §10.

The artifact + laws are copied VERBATIM from tests/test_step_editor_carried_bw.py (the resolver's
real output), so these tests pin the structure the fixture tests in test_achievability_warnings.py
missed (that file's ``_CHIRP_ART`` uses a param-dependent reported-bridge density instead).
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm

# A module-level app (as in test_step_editor_carried_bw.py) stabilises headless-Qt teardown for the
# one editor-wiring test in this file; the pure model tests ignore it.
_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    """Drain Qt's DeferredDelete queue after each test (the editor-wiring test builds real Qt
    widgets; a pre-existing headless-Qt teardown SIGABRT otherwise leaks to process exit)."""
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# psd_live: live spectral density = base + coeff·log10(bw/ref); coeff −10, ref 10 (dBm/MHz).
_PSD = {"id": "psd_live", "name": "Spectral density", "unit": "dBm/MHz", "in": "density",
        "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0,
        "restates_measurement": True}
_FBW = {"id": "fbw_power", "name": "Full-bandwidth (total) power", "unit": "dBm", "in": "density",
        "out": "abs", "k": 10.0, "rep": 10.0}

# The artifact EXACTLY as the agent resolver publishes it for a density measurement whose dBm
# ceiling is gauged through the (constant) total-power law: the base quantity maps to gain
# bandwidth-INDEPENDENTLY, so bounds_at is the same at every --bw (param_dependent is False).
_ART = {
    "schema_version": 1, "signal_id": "fm_chirp", "unit_type": "broadcaster",
    "operating_plane": "sdr_output", "quantity": "spectral density", "amplitude": 0.5,
    "min_gain_db": 0.0, "max_gain_db": 70.0, "min_power_dbm": -27.38, "max_power_dbm": -7.38,
    "curve": [[0.0, -27.38], [70.0, -7.38]], "gain_step_db": 0.5, "operating_unit": "dBm/MHz",
    "readings": {"reported": {"kind": "same"},
                 "limiting": {"kind": "law", "law": {"id": "fbw_power", "name": "fbw",
                                                     "in": "density", "out": "abs", "k": 10.0},
                              "max_dbm": 50.0},
                 "reported_delta_db": 0.0, "limiting_delta_db": 10.0},
    "anchor_curve": [[0.0, -27.38], [70.0, -7.38]], "passive_hops": [],
    "freq_dependent_limits": [], "gain_ceiling_db": 70.0, "center_freq_hz": 1575420000.0,
}
_SPECS = [
    {"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz", "is_freq": True},
    {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm/MHz", "snap_role": "power"},
    {"dest": "bw", "flags": ["--bw"], "type": "float", "unit": "MHz"},
]
# A low baseline density (comfortably in range) so the bar itself never warns.
_BASE = ["--freq", "1575.42", "--power", "-25", "--bw", "10"]


def _resolve(task, view=True):
    if task != "chirp":
        return None
    info = {"artifact": _ART, "specs": _SPECS, "base_args": _BASE,
            "freq_param": "freq", "freq_factor": 1e6, "power_dest": "power"}
    if view:
        info["view_law"] = _PSD                       # default control view = live density
        info["view_laws"] = {"psd_live": _PSD, "fbw_power": _FBW}   # per-step lookup
    return info


def _set_power_view(dbm, offset, view):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"power": dbm}, power_view=view)


def _bar():
    return tlm.BarItem(task_name="chirp", args=_BASE, start_offset=0.0, stop_offset=600.0)


def _set_power(dbm, offset):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"power": dbm})


def _set_bw(bw, offset):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"bw": bw})


# density max at bandwidth bw = base_max (−7.38) + view_delta(bw), view_delta = −10·log10(bw/10).
def _psd_max(bw):
    return -7.38 - 10 * math.log10(bw / 10.0)


# ── the crux: the base is bandwidth-invariant, so ONLY the view fold catches the clamp ──────────

def test_the_base_fold_alone_would_skip_this_task():
    # Guard the premise: with NO controlling view law surfaced, the walk sees a constant chain
    # (base bw-invariant, no frequency dependence) and stays out — exactly why the owner saw no
    # warning before this fix. The bandwidth dependence is invisible to the base fold.
    set_max = _set_power(-7.38, 5.0)          # bw-10 max density, set at bw 10
    double_bw = _set_bw(20, 10.0)             # later widened to 20 MHz
    assert tlm.achievability_warnings([_bar(), set_max, double_bw],
                                      lambda t: _resolve(t, view=False)) == []


def test_held_density_clamps_when_a_later_tune_widens_the_sweep():
    # The owner's report: set the density to the bw-10 max (−7.38), then a LATER tune doubles the
    # sweep to 20 MHz. The live density can't be held there (max ≈ −10.39), so the runtime clamps
    # it. The base value (−7.38) is unchanged and the base range is bandwidth-invariant, so ONLY
    # the controlled-view fold catches this.
    set_max = _set_power(-7.38, 5.0)
    double_bw = _set_bw(20, 10.0)
    issues = tlm.achievability_warnings([_bar(), set_max, double_bw], _resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(_psd_max(20), abs=0.02)      # ≈ −10.39 dBm/MHz
    assert iss.unit == "dBm/MHz"
    assert iss.points == [(-1, -7.38, 10.0)]                       # the held density, at the widen
    m = iss.message
    assert "held" in m and "clamped down" in m
    assert "-7.38" in m.replace("−", "-")                          # the held density, named
    assert "0:10" in m                                             # when it goes out of range
    assert "‘bw’ change to 20" in m                                # what pushed it out


def test_held_density_silent_without_the_later_widen():
    # The same held density is fine until a later step widens the sweep — no widen, no warning.
    assert tlm.achievability_warnings([_bar(), _set_power(-7.38, 5.0)], _resolve) == []


def test_held_density_in_range_stays_silent_after_widening():
    # A density comfortably below the bw-20 max stays achievable after doubling → no false positive.
    low = _set_power(-15.0, 5.0)              # −15 is below _psd_max(20) ≈ −10.39
    assert tlm.achievability_warnings([_bar(), low, _set_bw(20, 10.0)], _resolve) == []


def test_held_density_warns_once_not_at_every_later_widen():
    set_max = _set_power(-7.38, 5.0)
    issues = tlm.achievability_warnings(
        [_bar(), set_max, _set_bw(20, 10.0), _set_bw(40, 15.0)], _resolve)
    assert len(issues) == 1                   # one transition into violation, not one per widen


# ── the SET-TIME-bandwidth model: a density AUTHORED at a wide sweep is not spuriously flagged ──
# The pragmatic §10 reading (stored base == intended density at the REFERENCE bw) would MIS-flag a
# density the operator authored while a non-reference --bw was carried, because the step editor
# already folds the view at the carried bw. The set-time model interprets the stored base as the
# density at the --bw IN EFFECT WHEN IT WAS SET, so an in-range authored density stays silent.

def test_density_authored_at_the_wide_sweep_is_not_spuriously_warned():
    # Widen to 20 FIRST, then set --power −7.38 (base). At bw 20 that base reads as −10.39 dBm/MHz
    # — exactly the bw-20 max, i.e. deliverable — so the operator authored a valid density. The
    # set-time model stays silent; the reference-bw reading would have wrongly flagged −7.38.
    seq = [_bar(), _set_bw(20, 5.0), _set_power(-7.38, 10.0)]
    assert tlm.achievability_warnings(seq, _resolve) == []


def test_density_authored_at_the_wide_sweep_then_widened_further_clamps():
    # Author −7.38 base at bw 20 (density −10.39, deliverable), THEN widen to 40. Holding that
    # −10.39 density needs the bw-40 max (≈ −13.40) — undeliverable → clamp. Confirms the held
    # density tracked is the SET-TIME one (−10.39 at bw 20), not the raw base or the reference.
    seq = [_bar(), _set_bw(20, 5.0), _set_power(-7.38, 10.0), _set_bw(40, 15.0)]
    issues = tlm.achievability_warnings(seq, _resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(_psd_max(40), abs=0.02)      # ≈ −13.40 dBm/MHz
    assert iss.points[0][1] == pytest.approx(_psd_max(20), abs=0.02)   # the held density ≈ −10.39


def test_run_step_commanding_an_over_max_base_density_clamps_at_its_bandwidth():
    # A run step re-invokes the task with its OWN args. A base −4.0 dBm/MHz exceeds the chain max
    # (−7.38) — at its own bw 20 that reads as −7.01 dBm/MHz, above the −10.39 max, so it clamps.
    run = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=30.0,
                      args=["--freq", "1575.42", "--power", "-4.0", "--bw", "20"])
    issues = tlm.achievability_warnings([_bar(), run], _resolve)
    assert len(issues) == 1
    assert issues[0].direction == "high"
    assert "set to" in issues[0].message


# ── DENSITY RAMP whose top points become undeliverable after a mid-ramp --bw widen (Issue 1) ────
# The owner authored a live-density ramp up to the bw-10 max (−7.38) and added a LATER tune doubling
# the sweep to 20 MHz. The top ramp points can no longer hold their intended density (max ≈ −10.39),
# so the runtime clamps them — but no warning appeared. The ramp is authored ONCE at the start width,
# so each point's intended density is held CONSTANT across the ramp; the walk must check that
# intended density against the achievable range at each point's own fire-time --bw.

def _density_ramp(start=-16.24, stop=-7.38, steps=11, hold=12.0, offset=0.0, view="psd_live"):
    return tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=offset,
                       ramp={"param": "power", "flag": "--power", "start": start, "stop": stop,
                             "steps": steps, "hold_s": hold}, power_view=view)


def test_density_ramp_top_points_clamp_after_a_midramp_widen():
    # −16.24 → −7.38 dBm/MHz density ramp; a tune at +36 s doubles the sweep to 20 MHz mid-ramp.
    # Points whose INTENDED density exceeds the bw-20 max (−10.39) clamp; the lower ones don't.
    widen = _set_bw(20, 36.0)
    issues = tlm.achievability_warnings([_bar(), _density_ramp(), widen], _resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(_psd_max(20), abs=0.02)      # ≈ −10.39 dBm/MHz, the bw-20 max
    assert iss.unit == "dBm/MHz"
    # every flagged level is a real intended density above the ceiling; the top (−7.38) is included.
    levels = [v for (_i, v, _t) in iss.points]
    assert all(v > _psd_max(20) - 0.02 for v in levels)
    assert max(levels) == pytest.approx(-7.38, abs=0.02)
    # and the lower ramp points (comfortably below the ceiling) are NOT flagged.
    assert min(levels) > _psd_max(20) - 1.0
    assert "clamped down" in iss.message and "ramp" in iss.message


def test_density_ramp_silent_without_the_later_widen():
    # Same ramp at bw 10 (max −7.38) with no widen → every point is deliverable → no warning.
    assert tlm.achievability_warnings([_bar(), _density_ramp()], _resolve) == []


def test_density_ramp_authored_at_the_wide_sweep_stays_silent():
    # Widen to 20 FIRST, then draw the ramp: it is authored at bw 20, so its intended densities are
    # the base values read at bw 20 (top −7.38 base → −10.39, the bw-20 max). All deliverable at the
    # width they were drawn — the set-time model must NOT spuriously flag them.
    assert tlm.achievability_warnings([_bar(), _set_bw(20, -1.0), _density_ramp(offset=0.0)],
                                      _resolve) == []


def test_density_ramp_top_matches_a_directly_set_density_at_the_same_widen():
    # Sanity: the ramp's top point (−7.38 intended density, held to bw 20) clamps to the SAME ceiling
    # as a directly-set −7.38 density held to bw 20 — the two code paths agree on the operating point.
    ramp_iss = tlm.achievability_warnings([_bar(), _density_ramp(), _set_bw(20, 36.0)], _resolve)
    set_iss = tlm.achievability_warnings([_bar(), _set_power(-7.38, 5.0), _set_bw(20, 36.0)], _resolve)
    assert ramp_iss and set_iss
    assert ramp_iss[0].bound == pytest.approx(set_iss[0].bound, abs=0.001)


# ── per-step control view (latest-set-wins): the SAME base value warns as density, not as total ─
# The held quantity is whatever the LATEST power-setting step was authored in (its power_view). A
# density (bw-keyed) view is re-checked at each --bw; a total-power view is bandwidth-invariant, so
# holding it never clamps on a --bw change even at the same base value.

def test_total_power_step_is_not_warned_when_the_sweep_widens():
    # Controlling in TOTAL POWER: base −7.38 = the chain max, held across a --bw widen. Total power
    # is bandwidth-invariant, so it stays deliverable at bw 20 → no warning.
    set_total = _set_power_view(-7.38, 5.0, "fbw_power")
    double_bw = _set_bw(20, 10.0)
    assert tlm.achievability_warnings([_bar(), set_total, double_bw], _resolve) == []


def test_density_step_at_the_same_base_value_does_warn_when_the_sweep_widens():
    # The contrast: the SAME base −7.38, but controlled in the DENSITY view, IS pushed out of range
    # at bw 20 (max ≈ −10.39) — so the ONLY difference from the total-power case is power_view.
    set_density = _set_power_view(-7.38, 5.0, "psd_live")
    double_bw = _set_bw(20, 10.0)
    issues = tlm.achievability_warnings([_bar(), set_density, double_bw], _resolve)
    assert len(issues) == 1
    assert issues[0].direction == "high"
    assert issues[0].bound == pytest.approx(_psd_max(20), abs=0.02)


def test_latest_set_view_wins_total_then_density():
    # Latest-set-wins: a total-power step (silent), then a LATER density step re-commands the same
    # base — from then on density is held, so a still-later widen clamps it.
    set_total = _set_power_view(-7.38, 5.0, "fbw_power")     # total power held → no warn on widen
    set_density = _set_power_view(-7.38, 10.0, "psd_live")   # now density is held
    double_bw = _set_bw(20, 15.0)
    issues = tlm.achievability_warnings([_bar(), set_total, set_density, double_bw], _resolve)
    assert len(issues) == 1
    assert issues[0].points == [(-1, -7.38, 15.0)]          # the held density, flagged at the widen


# ── the TimelineEditor surfaces the controlled-view law to the walk (wiring end-to-end) ─────────

def test_timeline_editor_surfaces_the_view_law_and_the_held_density_clamp():
    # The editor's _achievability_resolver must surface the controlling restates_measurement law
    # (psd_live) from the script's CAL_POWER_LAWS, so the walk folds the live density. Without that
    # the real chirp (bw-invariant base) would be skipped and the held-density clamp go unwarned.
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from ui.timeline_editor import TimelineEditor
    QApplication.instance() or QApplication([])

    ed = TimelineEditor()
    ed._task_commands = {"chirp": ["python3", "chirp.py", "--freq", "1575.42", "--power",
                                   "-25", "--bw", "10"]}
    ed._task_signals = {"chirp": "chirp"}
    ed._param_specs = {"chirp.py": _SPECS}
    ed._script_cal_freq_params = {"chirp.py": "freq"}
    ed._script_power_laws = {"chirp.py": [_PSD, _FBW]}
    ed._cal_hostname = "unit"
    ed._calibration = {"valid": True, "signals": {"chirp": {"artifact": _ART}}}

    ed._canvas.set_items([_bar(), _set_power(-7.38, 5.0), _set_bw(20, 10.0)])
    ed._update_achievability()
    assert "held" in ed._achv_warn.text()
    assert "clamped down" in ed._achv_warn.text()
    assert not ed._achv_warn.isHidden()

    # Remove the widen → the density is deliverable at bw 10 and the banner clears.
    ed._canvas.set_items([_bar(), _set_power(-7.38, 5.0)])
    ed._update_achievability()
    assert ed._achv_warn.text() == ""
    assert ed._achv_warn.isHidden()
