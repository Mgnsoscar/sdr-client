"""When a task's script declares a CAL_SIGNAL_ID, the task editor must stamp
SDR_CAL_SIGNAL_ID into the task's env automatically — otherwise no per-unit view
can offer absolute (calibrated) power. It must not clobber an operator-set value,
and must leave scripts with no calibration signal untouched."""
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


def test_calibration_script_stamps_optin_env():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST)
    dlg._select_script("mock_tx.py")
    assert _env_dict(dlg).get("SDR_CAL_SIGNAL_ID") == "mock"


def test_plain_script_leaves_env_empty():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_PLAIN)), LIBRARY_HOST)
    dlg._select_script("mock_tx.py")
    assert "SDR_CAL_SIGNAL_ID" not in _env_dict(dlg)


def test_does_not_clobber_operator_value():
    dlg = TaskEditorDialog(FakeHub(FakeLibClient(PARAMS_CAL)), LIBRARY_HOST)
    dlg._env.setPlainText("SDR_CAL_SIGNAL_ID=custom")
    dlg._select_script("mock_tx.py")
    assert _env_dict(dlg).get("SDR_CAL_SIGNAL_ID") == "custom"
