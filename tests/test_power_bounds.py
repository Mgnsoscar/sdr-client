"""apply_power_bounds() rewrites a task's --power spec to a unit's resolved
calibration range so the form shows the real acceptable min/max."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.param_form import apply_power_bounds, find_power_index, range_hint


def _specs():
    return [
        {"dest": "prn", "flags": ["-PRN", "--prn"], "type": "int", "min": 1, "max": 63},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float",
         "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0,
         "help": "Target output power."},
    ]


def _bounds():
    return {"min_power_dbm": -1.8, "max_power_dbm": 28.2,
            "quantity": "EIRP", "operating_plane": "antenna_eirp"}


def test_find_power_index():
    assert find_power_index(_specs()) == 1
    assert find_power_index([{"dest": "prn", "flags": ["--prn"]}]) is None


def test_bounds_applied_to_power_only():
    out = apply_power_bounds(_specs(), _bounds())
    p = out[1]
    assert (p["min"], p["max"]) == (-1.8, 28.2)
    assert p["unit"] == "dBm EIRP"
    assert "antenna_eirp" in p["help"] and "-1.8" in p["help"]
    assert p["step"] == 0.5 and p["type"] == "float"
    # other params untouched; original list not mutated
    assert out[0] == _specs()[0]
    assert _specs()[1]["min"] == -140.0


def test_default_is_clamped_into_range():
    specs = _specs(); specs[1]["default"] = -20.0     # below the calibrated floor
    out = apply_power_bounds(specs, _bounds())
    assert out[1]["default"] == -1.8                  # clamped up to min
    specs[1]["default"] = 999.0
    out = apply_power_bounds(specs, _bounds())
    assert out[1]["default"] == 28.2                  # clamped down to max


def test_noop_without_bounds_or_power():
    assert apply_power_bounds(_specs(), None) == _specs()
    assert apply_power_bounds(_specs(), {}) == _specs()
    no_power = [{"dest": "prn", "flags": ["--prn"]}]
    assert apply_power_bounds(no_power, _bounds()) == no_power


def test_incomplete_bounds_is_noop():
    assert apply_power_bounds(_specs(), {"min_power_dbm": -1.8}) == _specs()


def test_range_hint_reflects_new_bounds():
    out = apply_power_bounds(_specs(), _bounds())
    assert range_hint(out[1]) == "-1.8..28.2"
