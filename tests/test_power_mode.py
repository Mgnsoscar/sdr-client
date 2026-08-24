"""ParamForm power mode: relative (raw --gain) vs absolute (--power, dBm).
Library (no unit) → absolute is offered free-form and is the default (portable, plan-
faithful); a targeted-but-uncalibrated unit → relative only; a calibrated unit → a
bounded toggle. values() emits only the active field's flag."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm, _compute_power_modes

_app = QApplication.instance() or QApplication([])


def _specs():
    return [
        {"dest": "prn", "flags": ["-PRN", "--prn"], "type": "int", "min": 1, "max": 32,
         "default": 1},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "live": True, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 89.75, "live": True, "help": "rel"},
    ]


def _bounds():
    return {"min_power_dbm": -1.8, "max_power_dbm": 28.2,
            "quantity": "EIRP", "operating_plane": "antenna_eirp"}


# ── mode computation ─────────────────────────────────────────────────────────────

def test_modes_library_offers_absolute_default():
    # Library (no unit): absolute is offered free-form AND first (the default).
    assert _compute_power_modes(_specs(), None, False) == ["absolute", "relative"]


def test_modes_uncalibrated_unit_relative_only():
    assert _compute_power_modes(_specs(), None, True) == ["relative"]


def test_modes_calibrated_unit_both():
    assert _compute_power_modes(_specs(), _bounds(), True) == ["absolute", "relative"]


def test_modes_power_only_script_absolute():
    only_power = [s for s in _specs() if s["dest"] != "gain"]
    assert _compute_power_modes(only_power, None, False) == ["absolute"]


# ── rendering + values ───────────────────────────────────────────────────────────

def _dests(form):
    return set(form._widgets.keys())


def test_library_defaults_to_absolute_free_form():
    f = ParamForm()
    f.set_params(_specs())                      # library: no unit context
    assert f.power_mode() == "absolute"
    assert "power" in _dests(f) and "gain" not in _dests(f)
    # free-form: the --power field keeps its schema bounds (no unit's range clamped in)
    assert f._widgets["power"][1]["max"] == 60.0


def test_calibrated_defaults_to_absolute_with_bounds():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True)
    assert f.power_mode() == "absolute"
    assert "power" in _dests(f) and "gain" not in _dests(f)
    assert f._widgets["power"][1]["max"] == 28.2      # bounds applied
    args = f.build_args()
    assert "-Power" in args and "-Gain" not in args   # power has a default → emitted


def test_default_power_mode_relative():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="relative")
    assert f.power_mode() == "relative"
    assert "gain" in _dests(f) and "power" not in _dests(f)
    assert f._widgets["gain"][1].get("required") is True   # must be filled


def test_apply_gain_bounds_limits_the_field():
    from ui.param_form import apply_gain_bounds
    specs = [{"dest": "gain", "flags": ["--gain"], "type": "float"}]
    out = apply_gain_bounds(specs, {"min_gain_db": 0.0, "max_gain_db": 74.0})
    assert out[0]["min"] == 0.0 and out[0]["max"] == 74.0 and out[0]["type"] == "float"


def test_relative_mode_bounds_gain_to_calibration():
    # On a calibrated unit, relative gain is clamped to the usable [min,max] gain range,
    # so you can't dial a gain that breaks a limit.
    f = ParamForm()
    b = dict(_bounds()); b["min_gain_db"] = 0.0; b["max_gain_db"] = 74.0
    f.set_params(_specs(), cal_bounds=b, absolute_allowed=True, default_power_mode="relative")
    assert f.power_mode() == "relative"
    assert f._widgets["gain"][1]["max"] == 74.0 and f._widgets["gain"][1]["min"] == 0.0


def test_toggle_switches_field_and_keeps_other_params():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True)
    # set a non-power param, then flip to relative
    f.set_values(["-PRN", "7"])
    f._on_mode_changed(f._power_modes.index("relative"))
    assert f.power_mode() == "relative"
    assert "gain" in _dests(f) and "power" not in _dests(f)
    args = f.build_args()
    assert "-PRN" in args and args[args.index("-PRN") + 1] == "7"   # preserved
