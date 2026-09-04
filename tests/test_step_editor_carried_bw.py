"""Temporal LIMITS: a tune/ramp step editing only --power must fold the operator's spectral-
density view at the CARRIED (fire-time) sweep bandwidth — not the schema default.

The owner's report: on a chirp whose --power is controlled in LIVE spectral density (dBm/MHz),
setting the density to bw-10's max at a step whose earlier sibling widened the sweep to 20 MHz was
allowed with no limit, because the density view folded at the bw SCHEMA DEFAULT (10) rather than
the carried 20. The catch that fixture tests missed: the real chirp's operating/base quantity is
the FIXED-reference measured density (bandwidth-INVARIANT, ``param_dependent`` False); the bw
dependence lives entirely in the ``restates_measurement`` psd_live VIEW law. And --bw is ``live``,
so a power-only tune step neither renders it as a field nor carried it as fold context → the view
folded at bw's default. This pins the real structure (artifact copied verbatim from the agent
resolver) and asserts the density limit now tracks the carried bandwidth.

Client-only. See docs/sequence-power-achievability.md §8 (companion-quantity temporal limits).
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QLabel

from ui import timeline_model as tlm
from ui.timeline_editor import StepEditorDialog, TimelineEditor
from tests.test_step_editor_power_units import FakeHub

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    """Drain Qt's DeferredDelete queue after each dialog test (headless-Qt teardown SIGABRT)."""
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# ── the REAL chirp laws: density restatements carry restates_measurement (psd_live leads, the raw
# measured density is dropped from the picker), total power is a distinct reading. ──
PSD = {"id": "psd_live", "name": "Spectral density", "unit": "dBm/MHz", "in": "density",
       "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0,
       "restates_measurement": True}
FBW = {"id": "fbw_power", "name": "Full-bandwidth (total) power", "unit": "dBm", "in": "density",
       "out": "abs", "k": 10.0, "rep": 10.0}
PSD_HZ = {"id": "psd_hz", "name": "Spectral density", "unit": "dBm/Hz", "in": "density",
          "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "k": -60.0, "rep": 10.0,
          "restates_measurement": True}

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
_SIGNAL = {"min_power_dbm": -27.38, "max_power_dbm": -7.38, "quantity": "spectral density",
           "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": _ART}
_SPECS = [
    {"dest": "freq", "flags": ["--freq"], "type": "float", "step": 0.01, "unit": "MHz",
     "default": 1575.42, "is_freq": True},
    {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01, "unit": "dBm/MHz",
     "snap_role": "power", "default": -7.38, "live": True},
    # --bw is LIVE (the operator CAN retune it), and the density view keys on it.
    {"dest": "bw", "flags": ["--bw"], "type": "float", "step": 0.1, "unit": "MHz",
     "default": 10.0, "min": 0.001, "max": 55.0, "live": True},
]
_CMD = ["python3", "chirp.py", "--freq", "1575.42", "--power", "-7.38", "--bw", "10"]


def _editor(items):
    ed = TimelineEditor()
    ed.set_context(FakeHub(), "unit")
    ed.set_task_commands({"chirp": list(_CMD)})
    ed.set_task_signals({"chirp": "chirp"})
    ed._param_specs = {"chirp.py": _SPECS}
    ed._script_cal_freq_params = {"chirp.py": "freq"}
    ed._script_power_laws = {"chirp.py": [PSD, FBW, PSD_HZ]}
    ed._script_cal_signals = {"chirp.py": "chirp"}
    ed._cal_hostname = "unit"
    ed._calibration = {"unit_type": "broadcaster", "valid": True, "signals": {"chirp": _SIGNAL}}
    ed._canvas.set_items(items)
    return ed


def _bar(bw=10):
    return tlm.BarItem(task_name="chirp",
                       args=["--freq", "1575.42", "--power", "-7.38", "--bw", str(bw)],
                       start_offset=0.0, stop_offset=600.0)


def _set_bw(bw, offset):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"bw": bw})


def _power_step(offset=310.0):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"power": -7.38})


def _dep_values(dlg):
    return [l.text() for l in dlg._form.findChildren(QLabel) if l.objectName() == "depValue"]


# density max = base max (−7.38) + view_delta(bw) where view_delta = −10·log10(bw/10).
def _psd_max(bw):
    return -7.38 - 10 * math.log10(bw / 10.0)


def test_density_limit_folds_at_the_carried_bandwidth():
    # The operator controls in psd_live (base measured density dropped by restates_measurement).
    pstep = _power_step()
    d10 = StepEditorDialog(pstep, _editor([_bar(10), pstep]), new=False)
    _app.processEvents()
    assert d10._form._selected_view()["id"] == "psd_live"     # base density dropped; live density leads
    assert d10._form._widgets["power"][0].maximum() == pytest.approx(_psd_max(10), abs=0.06)  # ≈ −7.38

    # Same power step, but an earlier tune widened the sweep to 20 MHz before it fires.
    pstep2 = _power_step()
    d20 = StepEditorDialog(pstep2, _editor([_bar(10), _set_bw(20, 5.0), pstep2]), new=False)
    _app.processEvents()
    assert "20" in _dep_values(d20)                            # carried --bw surfaced as 20
    hi20 = d20._form._widgets["power"][0].maximum()
    assert hi20 == pytest.approx(_psd_max(20), abs=0.06)       # ≈ −10.39, NOT −7.38
    # The owner's bug: the field used to cap at −7.38 at bw 20, letting an undeliverable density
    # through. Now the max tracks the carried bandwidth (≈ 3 dB lower for a doubled sweep).
    assert hi20 < _psd_max(10) - 2.5


def test_the_carried_bandwidth_limit_updates_when_the_step_moves():
    # The power step's carried --bw depends on where it sits: before the 5 s widen it carries 10,
    # after it carries 20. Dragging the offset past the widen must re-fold the density limit.
    pstep = _power_step(offset=2.0)                            # before the 5 s widen → carries bw 10
    dlg = StepEditorDialog(pstep, _editor([_bar(10), _set_bw(20, 5.0), pstep]), new=False)
    _app.processEvents()
    assert dlg._form._widgets["power"][0].maximum() == pytest.approx(_psd_max(10), abs=0.06)

    dlg._run_off.setValue(300.0)                              # drag it AFTER the widen
    _app.processEvents()
    assert "20" in _dep_values(dlg)
    assert dlg._form._widgets["power"][0].maximum() == pytest.approx(_psd_max(20), abs=0.06)


def test_tune_pill_shows_the_controlled_density_not_the_base():
    # Owner report: a density tune step's canvas pill showed the raw base --power (−7.something)
    # instead of the density the operator set. With a control view it now shows the live density at
    # the carried bandwidth; without one it still shows the raw param.
    ed = _editor([])
    pstep = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                        params={"power": -7.49}, power_view="psd_live")
    ed._canvas.set_items([_bar(10), _set_bw(20, 5.0), pstep])
    _app.processEvents()
    lbl = ed._canvas._run_label(pstep).replace("−", "-")
    assert "dBm/MHz" in lbl
    assert "-10.5" in lbl                    # density -10.5 at bw 20 (base -7.49 + view_delta(20))
    assert "-7.49" not in lbl
    raw = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=12.0,
                      params={"power": -7.49})
    assert "-7.49" in ed._canvas._run_label(raw)   # no control view → raw base


def test_tune_pill_shows_total_power_when_controlled_in_full_bandwidth_power():
    # Owner report: a tune step authored in FULL-BANDWIDTH (total) power showed the raw base
    # --power in the pill, not the total power. The total-power view (fbw_power) is a CONSTANT-offset
    # law (no bridge param), which the density-only pill path skipped. It now shows base + the law's
    # delta with its unit — total power ≈ base + 10 dB, bandwidth-invariant.
    ed = _editor([])
    pstep = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                        params={"power": -12.74}, power_view="fbw_power")
    ed._canvas.set_items([_bar(10), pstep])
    _app.processEvents()
    lbl = ed._canvas._run_label(pstep).replace("−", "-")
    assert "dBm" in lbl and "dBm/" not in lbl      # total power in plain dBm, not a density unit
    assert "-2.74" in lbl                          # base -12.74 + fbw delta (+10) = -2.74 dBm
    assert "-12.74" not in lbl
    # bandwidth-invariant: the same total power reads the same after a later widen.
    ed._canvas.set_items([_bar(10), _set_bw(20, 5.0), pstep])
    _app.processEvents()
    assert "-2.74" in ed._canvas._run_label(pstep).replace("−", "-")


def test_the_base_quantity_stays_bandwidth_invariant():
    # Guard the premise: this artifact's fold is NOT param-dependent — the base density range is the
    # same at every --bw (the bandwidth dependence lives only in the psd_live VIEW). So the temporal
    # limit could never come from the base fold; it must come from folding the view at carried bw.
    from state.power_fold import PowerFold
    fold = PowerFold.from_artifact(_ART)
    assert not fold.param_dependent
    assert fold.bounds_at(1575.42e6, {"bw": 10})["max_power_dbm"] == pytest.approx(-7.38, abs=1e-6)
    assert fold.bounds_at(1575.42e6, {"bw": 20})["max_power_dbm"] == pytest.approx(-7.38, abs=1e-6)
