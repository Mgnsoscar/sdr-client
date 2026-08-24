"""RunTaskDialog reflects a unit's resolved --power range: it reads the task's
SDR_CAL_SIGNAL_ID env, fetches /calibration, and bounds the power field — or falls
back to the script's schema range when the unit is uncalibrated."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from ui.run_task_dialog import RunTaskDialog

_app = QApplication.instance() or QApplication([])

YAML = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"-30\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
)
YAML_NO_OPTIN = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"-30\"]\n"
)
PARAMS = {"params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float",
     "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "help": "power"},
]}
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_gain_db": 0.0, "max_gain_db": 74.0,
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeClient:
    def __init__(self, yaml=YAML, cal=CAL):
        self._yaml, self._cal = yaml, cal

    def get_tasks_yaml(self):
        return self._yaml

    def get_script_params(self, name):
        return PARAMS

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, client):
        super().__init__()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)

    def refresh_now(self, *a, **k):
        pass


def _power_spec(dlg):
    return dlg._form._widgets["power"][1]


def test_calibrated_unit_bounds_power_field():
    dlg = RunTaskDialog(FakeHub(FakeClient()), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-1.8, 28.2)
    assert sp["unit"] == "dBm EIRP"
    assert "antenna_eirp" in sp["help"]


def test_uncalibrated_unit_keeps_schema_range():
    client = FakeClient(cal=AgentHTTPError("u", 404, "none"))
    dlg = RunTaskDialog(FakeHub(client), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-140.0, 60.0)     # unchanged


def test_task_without_optin_keeps_schema_range():
    dlg = RunTaskDialog(FakeHub(FakeClient(yaml=YAML_NO_OPTIN)), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-140.0, 60.0)
