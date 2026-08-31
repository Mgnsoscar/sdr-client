"""LiveTuneDialog reflects the unit's resolved --power range while retuning a
running task (reads SDR_CAL_SIGNAL_ID, fetches /calibration, bounds the field)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from ui.live_tune_dialog import LiveTuneDialog

_app = QApplication.instance() or QApplication([])

YAML = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"-30\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
)
PARAMS = {"params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "live": True,
     "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "help": "power"},
]}
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeClient:
    def __init__(self, cal=CAL, snapshot=None):
        self._cal = cal
        self._snapshot = snapshot if snapshot is not None else {"current": {}, "applied": {}}

    def get_tasks_yaml(self):
        return YAML

    def get_script_params(self, name):
        return PARAMS

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def get_task_params(self, name):
        return self._snapshot


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


def _power_spec(dlg):
    return dlg._form._widgets["power"][1]


def test_livetune_bounds_power_when_calibrated():
    dlg = LiveTuneDialog(FakeHub(FakeClient()), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-1.8, 28.2)
    assert sp["unit"] == "dBm EIRP"


def test_livetune_keeps_schema_when_uncalibrated():
    dlg = LiveTuneDialog(FakeHub(FakeClient(cal=AgentHTTPError("u", 404, "none"))), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-140.0, 60.0)


# ── seeding the running task's current --power ──────────────────────────────────────

_ACTIVE_ARTIFACT = {
    "curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0, "max_gain_db": 40.0,
    "gain_step_db": 1.0,
    "active_components": [{"plane": "atten_out", "task": "atten_set", "param": "attenuation",
                           "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
                           "step_db": 0.25, "engage_pct": 0.0, "baseline_delta_by_freq": []}],
}
_ACTIVE_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "atten_out", "quantity": "EIRP",
    "min_power_dbm": -135.0, "max_power_dbm": 0.0, "artifact": _ACTIVE_ARTIFACT}}}


def test_livetune_active_seeds_requested_power_not_the_gain_derived_value():
    # On an active chain the script reports --power from its SDR gain alone (attenuator
    # omitted), so `applied` reads high. The dialog must seed the accepted request (`current`),
    # or opening Tune would stage a power tens of dB above what's actually set.
    client = FakeClient(cal=_ACTIVE_CAL,
                        snapshot={"current": {"power": -100.0}, "applied": {"power": -80.0}})
    dlg = LiveTuneDialog(FakeHub(client), "u", "mocktask")
    assert dlg._form.values()["power"] == pytest.approx(-100.0)


def test_livetune_nonactive_seeds_the_applied_power():
    # With no active components `applied` is the real (gain-quantised) power, so it still wins.
    cal = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
        "operating_plane": "antenna_eirp", "quantity": "EIRP",
        "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}
    client = FakeClient(cal=cal, snapshot={"current": {"power": 5.0}, "applied": {"power": 10.0}})
    dlg = LiveTuneDialog(FakeHub(client), "u", "mocktask")
    assert dlg._form.values()["power"] == pytest.approx(10.0)
