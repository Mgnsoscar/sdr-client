"""The always-visible limit rail under a bounded numeric field: it renders for a bounded
field, tracks the value, carries the frequency note on a frequency-dependent power field,
and re-folds (via the form re-render) when the frequency changes."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui.param_form import ParamForm, RangeRail

_app = QApplication.instance() or QApplication([])


def _artifact():
    return {
        "anchor_curve": [[40.0, -8.0], [89.75, 21.0]],
        "passive_hops": [{"plane": "amp",
                          "delta_db_by_freq": [[1.2276e9, 26.7], [1.57542e9, 28.2]]}],
        "freq_dependent_limits": [], "gain_ceiling_db": 89.75,
        "min_gain_db": 40.0, "center_freq_hz": 1.57542e9,
    }


def _bounds():
    return {"min_power_dbm": -1.8, "max_power_dbm": 28.2, "quantity": "EIRP",
            "operating_plane": "amp", "artifact": _artifact()}


def _specs():
    return [
        {"dest": "freq", "flags": ["-Frequency", "--freq"], "type": "float", "unit": "Hz",
         "min": 70e6, "max": 6e9, "default": 1.57542e9,
         "presets": [{"label": "L1", "key": "l1", "value": 1.57542e9},
                     {"label": "L2", "key": "l2", "value": 1.2276e9}]},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": 24.0},
        {"dest": "rate", "flags": ["--rate"], "type": "float", "unit": "MHz",
         "min": 1.0, "max": 61.44, "step": 0.01, "default": 40.0},
        {"dest": "mode", "flags": ["--mode"], "type": "str", "choices": ["a", "b"]},
    ]


def _rails(form):
    return form.findChildren(RangeRail)


def test_bounded_fields_get_a_rail_unbounded_do_not():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    # power (bounded to cal) and rate (bounded number) get a rail; the freq preset combo
    # and the choice field do not.
    assert len(_rails(f)) == 2


def test_power_rail_carries_the_frequency_note():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    notes = [l.text() for rail in _rails(f) for l in rail.findChildren(QLabel)]
    assert any("moves with frequency" in t and "1575.42 MHz" in t for t in notes)


def test_rail_note_refolds_when_frequency_changes():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    f.set_values(["--freq", "1.2276e9", "--power", "24"])         # switch to L2
    notes = [l.text() for rail in _rails(f) for l in rail.findChildren(QLabel)]
    assert any("1227.60 MHz" in t for t in notes)                 # note re-folded
    assert not any("1575.42 MHz" in t for t in notes)


def test_no_frequency_note_without_a_freq_dependent_calibration():
    # A flat calibration (no freq-dependent artifact) still shows the rail, but no note.
    flat = {"min_power_dbm": -1.8, "max_power_dbm": 28.2, "quantity": "EIRP",
            "operating_plane": "amp",
            "artifact": {"curve": [[40.0, -8.0], [89.75, 28.2]], "min_gain_db": 40.0,
                         "max_gain_db": 89.75}}
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=flat, absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    notes = [l.text() for rail in _rails(f) for l in rail.findChildren(QLabel)]
    assert not any("moves with frequency" in t for t in notes)
    assert len(_rails(f)) == 2                                    # rails still present
