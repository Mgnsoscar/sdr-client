"""When a task's script declares a CAL_SIGNAL_ID, the task editor shows a dedicated
"Calibration signal" field that owns the SDR_CAL_SIGNAL_ID env var: it defaults to the
script's declared signal, keeps the var out of the raw Environment box, honors a value
the operator already set, and writes the chosen id (or nothing, for Off) back on save.
A script with no calibration signal shows no field and leaves the env untouched."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.fleet import LIBRARY_HOST
from ui.task_editor import TaskEditorDialog

_app = QApplication.instance() or QApplication([])

PARAMS_CAL = {"params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float",
     "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "help": "p"}],
    "calibration_signal": "mock"}
PARAMS_PLAIN = {"params": [
    {"dest": "freq", "flags": ["-f", "--freq"], "type": "float", "help": "f"}]}


class FakeLibClient:
    def __init__(self, params):
        self._params = params

    def list_scripts(self):
        return ["mock_tx.py"]

    def get_script_params(self, name):
        return self._params

    def get_tasks_yaml(self):
        return "tasks: []\n"

    def info(self):
        raise RuntimeError("no info in library")


class FakeFleet:
    def __init__(self, client):
        self._c = client

    def get(self, host):
        return self._c

    def __contains__(self, host):
        return True


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, client):
        super().__init__()
        self.fleet = FakeFleet(client)

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)

    def refresh_now(self, host):
        pass


def _env_dict(dlg):
    return dict(
        line.split("=", 1) for line in dlg._env.toPlainText().splitlines() if "=" in line)


def test_calibration_script_shows_field_defaulting_to_declared():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST)
    dlg._select_script("mock_tx.py")
    assert dlg._cal_wrap.isVisibleTo(dlg)                 # field shown for a cal script
    assert dlg._current_cal_value() == "mock"            # defaults to the declared signal
    assert "SDR_CAL_SIGNAL_ID" not in _env_dict(dlg)     # the field owns it, not the box
    # …and it's folded back into env on save
    assert dlg._apply_cal_signal({}).get("SDR_CAL_SIGNAL_ID") == "mock"


def test_off_choice_sends_raw_power():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST)
    dlg._select_script("mock_tx.py")
    dlg._cal_combo.setCurrentIndex(0)                     # "Off — raw power"
    assert dlg._current_cal_value() == ""
    assert "SDR_CAL_SIGNAL_ID" not in dlg._apply_cal_signal({})


def test_plain_script_hides_field_and_leaves_env_untouched():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_PLAIN)), LIBRARY_HOST)
    dlg._env.setPlainText("SDR_CAL_SIGNAL_ID=manual")    # a hand-set var on a plain script
    dlg._select_script("mock_tx.py")
    assert not dlg._cal_wrap.isVisibleTo(dlg)             # no field
    assert _env_dict(dlg).get("SDR_CAL_SIGNAL_ID") == "manual"   # left as a plain env line
    assert dlg._apply_cal_signal(_env_dict(dlg)).get("SDR_CAL_SIGNAL_ID") == "manual"


def test_honors_operator_value_and_pulls_it_into_the_field():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST)
    dlg._env.setPlainText("SDR_CAL_SIGNAL_ID=custom")
    dlg._select_script("mock_tx.py")
    assert dlg._current_cal_value() == "custom"           # taken over by the field
    assert "SDR_CAL_SIGNAL_ID" not in _env_dict(dlg)      # and removed from the raw box
    assert dlg._apply_cal_signal({}).get("SDR_CAL_SIGNAL_ID") == "custom"


def test_edit_opens_on_the_saved_signal():
    # An edited task's env prefills SDR_CAL_SIGNAL_ID into the box; the field takes it over.
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST,
                           existing_name="tx")
    dlg._env.setPlainText("SDR_CAL_SIGNAL_ID=gnss_l5")    # as _prefill_from_yaml would set
    dlg._select_script("mock_tx.py")
    assert dlg._current_cal_value() == "gnss_l5"          # override picked up from the box
    assert "SDR_CAL_SIGNAL_ID" not in _env_dict(dlg)


def test_edit_without_the_var_opens_off():
    # A calibratable task edited with no SDR_CAL_SIGNAL_ID was deliberately raw → Off.
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST,
                           existing_name="tx")
    dlg._select_script("mock_tx.py")
    assert dlg._current_cal_value() == ""                # Off, not the script default
    assert "SDR_CAL_SIGNAL_ID" not in dlg._apply_cal_signal({})
