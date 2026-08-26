"""ParamForm re-folds the --power / --gain range at the frequency the operator enters,
when the script declares CAL_FREQ_PARAM and the calibration is frequency-dependent (a
chirp / CW into a frequency-dependent chain). The range must track the chosen frequency,
so the operator always sees the real possible range."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


def _artifact():
    # amp anchor 24 dBm @ 74 / -6 @ 40; freq-dependent cable+antenna (+3 dB @1 GHz,
    # +4 dB @2 GHz); amp ceiling at gain 74. Max EIRP: 27 @1 GHz, 28 @2 GHz.
    return {
        "anchor_curve": [[40.0, -6.0], [74.0, 24.0]],
        "passive_hops": [
            {"plane": "cable_output", "delta_db_by_freq": [[1.0e9, -2.0], [2.0e9, -3.0]]},
            {"plane": "antenna_eirp", "delta_db_by_freq": [[1.0e9, 5.0], [2.0e9, 7.0]]},
        ],
        "freq_dependent_limits": [], "gain_ceiling_db": 74.0,
        "min_gain_db": 40.0, "center_freq_hz": 1.0e9,
    }


def _bounds():
    return {"min_power_dbm": -3.0, "max_power_dbm": 27.0, "quantity": "EIRP",
            "operating_plane": "antenna_eirp", "artifact": _artifact()}


def _specs():
    return [
        {"dest": "freq", "flags": ["-Frequency", "--freq"], "type": "float", "unit": "Hz",
         "min": 70e6, "max": 6e9, "default": 1.0e9,
         "presets": [{"label": "f1", "key": "f1", "value": 1.0e9},
                     {"label": "f2", "key": "f2", "value": 2.0e9}]},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "live": True, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 89.75, "live": True, "help": "rel"},
    ]


def _power_max(f):
    return f._widgets["power"][1]["max"]


def test_power_range_folds_at_default_frequency():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    assert _power_max(f) == 27.0                    # folded at the default 1 GHz


def test_changing_frequency_refolds_the_power_range():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    w = f._widgets["freq"][0]
    w.setCurrentText("f2")                          # operator picks 2 GHz
    f._on_freq_changed()
    assert _power_max(f) == 28.0                    # range moved with frequency
    # and back
    w = f._widgets["freq"][0]
    w.setCurrentText("f1")
    f._on_freq_changed()
    assert _power_max(f) == 27.0


def test_prefill_folds_at_the_loaded_frequency():
    # A saved step/task command whose --freq is 2 GHz must open with the 2 GHz range,
    # not the default-frequency range.
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    f.set_values(["--freq", "2e9", "--power", "20"])
    assert _power_max(f) == 28.0


def test_folds_at_carried_default_when_freq_field_not_set():
    # A sequence step that does NOT set --freq folds the --power range at the carried-forward
    # frequency (the effective freq at that offset), not the field's schema default.
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq",
                 cal_freq_default=2.0e9)                    # effective freq carried in
    assert _power_max(f) == 28.0                            # folded at 2 GHz, not the 1 GHz default


def test_selectable_freq_overrides_the_carried_default_only_when_ticked():
    # In a tune step (selectable), the freq field folds only when its box is ticked;
    # unticked, the carried-forward frequency governs the --power range.
    f = ParamForm()
    f.set_params(_specs(), selectable=True, cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq",
                 cal_freq_default=2.0e9)
    assert _power_max(f) == 28.0                            # unticked freq → carried 2 GHz
    # tick freq and set it to 1 GHz → now the step's own frequency governs
    f._checks["freq"].setChecked(True)
    f._widgets["freq"][0].setCurrentText("f1")
    f._on_freq_changed()
    assert _power_max(f) == 27.0


def test_no_refold_without_cal_freq_param():
    # Same schema/bounds but the script didn't declare CAL_FREQ_PARAM → the range stays at
    # the resolved (representative) value regardless of the freq field.
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")           # no cal_freq_param
    f.set_values(["--freq", "2e9"])
    assert _power_max(f) == 27.0                    # unchanged
