"""ParamForm with a reported power-quantity bridge (docs/calibration-v2.md §13): the --power
field is labelled in the reported reading's script-defined unit (not always dBm), and its
range re-folds as the bridge's keyed parameter (e.g. --bw) is tuned — the same way it already
re-folds on a frequency change."""
import os

import pytest

from ui.param_form import apply_power_bounds

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 10.0}     # rep 10 MHz → +10 dB


def _artifact(operating_unit="dBm"):
    # measured DENSITY anchor: gain 40→-30, 74→4 dBm/MHz; reported = full-bandwidth power
    return {
        "anchor_curve": [[40.0, -30.0], [74.0, 4.0]],
        "passive_hops": [], "freq_dependent_limits": [],
        "gain_ceiling_db": 74.0, "min_gain_db": 40.0,
        "operating_unit": operating_unit,
        "readings": {"reported": {"kind": "law", "unit": operating_unit, "law": FBW},
                     "limiting": {"kind": "same"}},
    }


def _bounds(operating_unit="dBm"):
    return {"min_power_dbm": -20.0, "max_power_dbm": 14.0,
            "quantity": "full-bandwidth power", "operating_plane": "antenna",
            "artifact": _artifact(operating_unit)}


def _specs():
    return [
        {"dest": "bw", "flags": ["-Sweep-BW", "--bw"], "type": "float", "unit": "MHz",
         "min": 0.001, "max": 100.0, "default": 10.0,
         "presets": [{"label": "10", "key": "b10", "value": 10.0},
                     {"label": "100", "key": "b100", "value": 100.0}]},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "live": True, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 89.75, "live": True, "help": "rel"},
    ]


def _power_max(f):
    return f._widgets["power"][1]["max"]


# ── unit label ──────────────────────────────────────────────────────────────

def test_power_field_uses_reported_unit_not_dbm():
    out = apply_power_bounds(_specs(), _bounds(operating_unit="dBm/MHz"))
    sp = next(s for s in out if s["dest"] == "power")
    assert sp["unit"] == "dBm/MHz"        # script-defined reported unit, not the default dBm


def test_power_field_falls_back_to_dbm_without_operating_unit():
    b = _bounds()
    b["artifact"].pop("operating_unit")
    b["artifact"].pop("readings")
    out = apply_power_bounds(_specs(), b)
    sp = next(s for s in out if s["dest"] == "power")
    assert sp["unit"].startswith("dBm")   # plain calibration keeps the old label


# ── parameter re-fold ─────────────────────────────────────────────────────────

def test_power_range_folds_at_default_bandwidth():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    # reported max at rep bw 10 MHz: anchor top 4 dBm/MHz + 10*log10(10) = 14
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)


def test_changing_bandwidth_refolds_the_power_range():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    f._widgets["bw"][0].setCurrentText("100")      # operator widens the sweep to 100 MHz
    f._on_freq_changed()
    assert _power_max(f) == pytest.approx(24.0, abs=1e-6)   # 4 + 10*log10(100) = 24
    f._widgets["bw"][0].setCurrentText("10")
    f._on_freq_changed()
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)


def _specs_slider_bw():
    # --bw as a plain bounded number (no presets) → rendered as a spinbox + range rail.
    specs = _specs()
    specs[0] = {"dest": "bw", "flags": ["-Sweep-BW", "--bw"], "type": "float", "unit": "MHz",
                "min": 1.0, "max": 100.0, "default": 10.0, "step": 1.0}
    return specs


def test_bandwidth_slider_refolds_power_live_without_a_commit():
    # A drag on the --bw slider sets its spinbox value WITHOUT keyboard focus; that schedules a
    # debounced re-fold that fires once the drag settles — so --power re-folds without the
    # operator having to click elsewhere (only editingFinished did that before).
    from PyQt6.QtWidgets import QDoubleSpinBox
    f = ParamForm()
    f.set_params(_specs_slider_bw(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    bw = f._widgets["bw"][0]
    assert isinstance(bw, QDoubleSpinBox)          # a slider-backed spinbox, not a preset combo
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)
    assert not bw.hasFocus()                       # a rail drag doesn't focus the spinbox
    bw.setValue(100.0)                             # ← what RangeRail.valueChanged does on a drag
    assert f._refold_timer.isActive()              # a live change was queued (not fired per-tick)
    f._refold_timer_fire()                         # the drag settled (no mouse button held)
    assert _power_max(f) == pytest.approx(24.0, abs=1e-6)   # re-folded live, no commit needed


def test_typing_bandwidth_does_not_refold_until_commit():
    # A keystroke keeps focus on the field, so the live path is gated out (a mid-typing re-render
    # would steal focus); the commit path (editingFinished) still handles it.
    f = ParamForm()
    f.set_params(_specs_slider_bw(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    bw = f._widgets["bw"][0]
    bw.setFocus()
    if bw.hasFocus():                              # offscreen may not grant focus; guard it
        f._refold_timer.stop()
        f._schedule_live_refold(bw)
        assert not f._refold_timer.isActive()      # typing does not schedule a live re-fold
