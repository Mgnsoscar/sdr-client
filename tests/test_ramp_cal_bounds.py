"""The ramp editor's From/To fields conform to a unit's power calibration: when the
ramped parameter is the calibrated --power field, they render as bounded fields (spinbox
+ range rail + limit chip) whose min/max ARE the unit's resolved dBm bounds (from the
task's calibration signal), not the script's wider declared min/max."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import BoundedNumberField
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


def test_calibrated_range_is_the_field_bound():
    # From/To render as bounded fields (spinbox + rail + limit chip) whose min/max ARE the
    # unit's calibrated dBm range, not the script's wider -140..60 — so out-of-range values
    # can't be dialled in at all (the widget clamps).
    dlg = _dialog(_CAL_BOUNDS)
    for field in (dlg._start_field, dlg._stop_field):
        assert isinstance(field, BoundedNumberField)
        assert (field._spin.minimum(), field._spin.maximum()) == (-1.8, 28.2)
        assert field._chip is not None and field._rail is not None   # min/max chip + slider
    dlg._start_field.setValue(-20)       # below the calibrated floor
    assert dlg._start_field.value() == -1.8            # clamped to the calibrated min
    dlg._stop_field.setValue(60)         # above the calibrated ceiling
    assert dlg._stop_field.value() == 28.2             # clamped to the calibrated max


def test_calibrated_field_labels_the_quantity():
    dlg = _dialog(_CAL_BOUNDS)
    assert dlg._start_field._spin.suffix().strip() == "dBm EIRP"


def test_without_calibration_uses_the_script_range():
    dlg = _dialog(None)                  # uncalibrated / no bounds
    field = dlg._start_field
    assert isinstance(field, BoundedNumberField)
    assert (field._spin.minimum(), field._spin.maximum()) == (-140.0, 60.0)
    field.setValue(999)
    assert field.value() == 60.0                       # clamped to the script max
