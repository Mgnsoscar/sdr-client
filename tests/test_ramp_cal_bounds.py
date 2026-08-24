"""The ramp editor's From/To range check conforms to a unit's power calibration:
when the ramped parameter is the calibrated --power field, its allowed range is
the unit's resolved dBm bounds (from the task's calibration signal), not just the
script's wider declared min/max."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.ramp_editor import RampEditorDialog

_app = QApplication.instance() or QApplication([])

# A --power param whose SCRIPT range is wide; calibration narrows it hard.
_POWER_SPEC = {"dest": "power", "name": "power", "flags": ["-Power", "--power"],
               "type": "float", "unit": "dBm", "min": -140.0, "max": 60.0,
               "default": -20.0}
_CAL_BOUNDS = {"min_power_dbm": -1.8, "max_power_dbm": 28.2,
               "quantity": "EIRP", "operating_plane": "antenna_eirp"}


class _Src:
    ramp = None
    task_name = "tx"
    anchor = "start"
    offset = 0.0
    offset_end = 0.0
    args: list = []
    uid = "u1"


class _FakeEditor:
    """The slice of the timeline editor the ramp dialog talks to (hub=None so it
    reads params from the seeded cache synchronously, no async fetch)."""
    def __init__(self, specs, bounds):
        self._hub = None
        self._hostname = "u"
        self._params_inflight = set()
        self._cache = {"tx.py": list(specs)}
        self._bounds = bounds

    def available_tasks(self):
        return ["tx"]

    def sequence_task_names(self):
        return ["tx"]

    def script_for_task(self, task):
        return ("tx.py", [])

    def param_cache(self):
        return self._cache

    def task_spans(self, task):
        return [(0.0, 60.0)]

    def cal_bounds_for_task(self, task):
        return self._bounds


def _dialog(bounds):
    dlg = RampEditorDialog(_Src(), _FakeEditor([_POWER_SPEC], bounds), new=True)
    dlg._run_chk.setChecked(True)        # run mode: any numeric param is rampable
    dlg._param.setCurrentText("power")
    return dlg


def test_calibrated_range_is_enforced():
    dlg = _dialog(_CAL_BOUNDS)
    dlg._start.setText("-20")            # within the script range, below the cal floor
    dlg._stop.setText("10")
    err = dlg._range_error()
    assert err is not None
    assert "From" in err and "-1.8..28.2" in err   # the calibrated range, not -140..60
    # A sweep inside the calibrated range is fine.
    dlg._start.setText("0")
    dlg._stop.setText("25")
    assert dlg._range_error() is None


def test_without_calibration_falls_back_to_schema_range():
    dlg = _dialog(None)                  # uncalibrated / no bounds
    dlg._start.setText("-20")
    dlg._stop.setText("10")
    assert dlg._range_error() is None    # both within the script's -140..60
    dlg._stop.setText("999")
    err = dlg._range_error()
    assert err is not None and "-140..60" in err


def test_calibrated_unit_shows_dbm_and_bounds_in_message():
    dlg = _dialog(_CAL_BOUNDS)
    dlg._start.setText("50")             # above the calibrated ceiling
    dlg._stop.setText("60")
    err = dlg._range_error()
    assert err is not None
    assert "dBm" in err and "28.2" in err and "power" in err
