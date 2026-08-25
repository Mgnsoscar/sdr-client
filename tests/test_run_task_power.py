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


# A script with BOTH absolute --power and relative --gain (the real broadcaster shape).
PARAMS_PG = {"calibration_signal": "mock", "params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
     "min": -140.0, "max": 60.0, "default": -20.0, "help": "power"},
    {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
     "min": 0.0, "max": 89.75, "help": "relative gain"},
]}
YAML_ABS40 = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"40\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
)


class FakeClient:
    def __init__(self, yaml=YAML, cal=CAL, params=PARAMS):
        self._yaml, self._cal, self._params = yaml, cal, params
        self.started = []          # (name, StartRequest)
        self.updated = []          # (name, spec)

    def get_tasks_yaml(self):
        return self._yaml

    def get_script_params(self, name):
        return self._params

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def start_task(self, name, req=None):
        self.started.append((name, req))
        return {}

    def update_task(self, name, spec):
        self.updated.append((name, spec))
        return {}


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


# ── uncalibrated absolute-power task (issues #1 + #5) ───────────────────────────────

def _uncal_dialog():
    client = FakeClient(yaml=YAML_ABS40, params=PARAMS_PG,
                        cal=AgentHTTPError("u", 404, "none"))
    return RunTaskDialog(FakeHub(client), "u", "mocktask"), client


def test_uncalibrated_absolute_power_not_shown_or_lingering():
    # #5: the authored --power 40 must NOT linger in Additional args; the form is relative.
    dlg, _ = _uncal_dialog()
    assert dlg._uncalibrated_absolute() is True
    assert "power" not in dlg._form._widgets            # no absolute field, relative-only
    assert "--power" not in dlg._extra.text() and "40" not in dlg._extra.text()


def test_start_uncalibrated_prompts_for_gain_then_persists(monkeypatch):
    # #1: pressing Start with no relative gain prompts, applies the gain, runs WITHOUT
    # --power, and persists the gain as the task's stored command (for quick-play/sequences).
    dlg, client = _uncal_dialog()
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (55.0, True)))
    dlg._on_run()
    assert client.started, "the task should have started"
    _, req = client.started[-1]
    assert any(f in req.args for f in ("--gain", "-Gain")) and "55" in req.args
    assert not any(f in req.args for f in ("--power", "-Power"))   # absolute never sent
    # persisted to the stored command so a plain Start/sequence reuses it
    assert client.updated, "the gain should be persisted to the task"
    _, spec = client.updated[-1]
    assert any(f in spec["command"] for f in ("--gain", "-Gain"))
    assert not any(f in spec["command"] for f in ("--power", "-Power"))


def test_start_uncalibrated_cancel_does_not_run(monkeypatch):
    dlg, client = _uncal_dialog()
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (0.0, False)))   # cancelled
    dlg._on_run()
    assert client.started == [] and client.updated == []
