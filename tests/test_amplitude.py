"""Calibration amplitude: the gain→power curve is measured at a specific baseband
amplitude, so a calibrated task defaults its --amplitude to that value, and the form
flags a mismatch (power scales with amplitude, so an override makes --power inaccurate)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm, find_amplitude_index

_app = QApplication.instance() or QApplication([])

_POWER = {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.5}
_AMP = {"dest": "amplitude", "flags": ["--amplitude"], "type": "float", "step": 0.05,
        "default": 0.3}


def _bounds(amplitude=0.8):
    return {"min_power_dbm": -30.0, "max_power_dbm": 20.0, "amplitude": amplitude,
            "operating_plane": "antenna_eirp", "quantity": "EIRP"}


def test_find_amplitude_index():
    assert find_amplitude_index([_POWER, _AMP]) == 1
    assert find_amplitude_index([{"dest": "x", "flags": ["-Vector-Amplitude", "--amplitude"]}]) == 0
    assert find_amplitude_index([_POWER]) is None


def test_amplitude_defaults_to_calibrated_value():
    f = ParamForm()
    f.set_params([_POWER, _AMP], cal_bounds=_bounds(0.8), absolute_allowed=True)
    # the field's effective default is the calibrated amplitude, not the script's 0.3
    assert f._widgets["amplitude"][1]["default"] == 0.8
    assert f.values()["amplitude"] == pytest.approx(0.8)


def test_amplitude_not_touched_when_calibration_has_none():
    f = ParamForm()
    b = _bounds(); b.pop("amplitude")
    f.set_params([_POWER, _AMP], cal_bounds=b, absolute_allowed=True)
    assert f._widgets["amplitude"][1]["default"] == 0.3       # script default kept


def test_amplitude_mismatch_warning_toggles():
    # isHidden() reflects the explicit visibility flag regardless of the (unshown) parent.
    f = ParamForm()
    f.set_params([_POWER, _AMP], cal_bounds=_bounds(0.8), absolute_allowed=True)
    assert f._amp_warn is not None
    assert f._amp_warn.isHidden() is True                     # matches the calibrated 0.8
    f.set_values(["--amplitude", "0.5"])                      # override to a different value
    assert f._amp_warn.isHidden() is False
    assert "0.8" in f._amp_warn.text()
    f.set_values(["--amplitude", "0.8"])                      # back to the calibrated value
    assert f._amp_warn.isHidden() is True


def test_no_amplitude_warning_without_calibration():
    # Library / uncalibrated: no single calibrated amplitude to match → no caption.
    f = ParamForm()
    f.set_params([_POWER, _AMP])
    assert f._amp_warn is None
