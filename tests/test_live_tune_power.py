"""LiveTuneDialog reflects the unit's resolved --power range while retuning a
running task (reads SDR_CAL_SIGNAL_ID, fetches /calibration, bounds the field)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QPushButton

from api.client import AgentHTTPError
from ui.live_tune_dialog import LiveTuneDialog
from tests.test_param_form_power_units import (
    FBW, PSD, _artifact, _density_reported,
)

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


# ── the redesigned multi-quantity power card is offered in Tune, not just Run ─────────
# A signal that declares CAL_POWER_LAWS gets the same power card in the live-tune form as in
# the run form: an "ALSO READS AS" companion read-out per other quantity, each with a
# "Control in this →" switch. Regression: the dialog used to omit power_laws from set_params,
# so the companions/switch never rendered while retuning.

_CHIRP_YAML = (
    "tasks:\n"
    "  - name: chirp\n"
    "    command: [python3, fm_chirp_tx.py, --power, \"-20\", --bw, \"20\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: fm_chirp }\n"
)
# freq/power/bw are all live — a chirp retunes its sweep bandwidth live, so the density↔total
# companions track --bw exactly as in the run form.
_CHIRP_PARAMS = {
    "calibration_freq_param": "freq",
    "calibration_power_laws": [FBW, PSD],
    "params": [
        {"dest": "freq", "flags": ["--freq"], "type": "float", "step": 0.01, "unit": "MHz",
         "default": 1575.42, "is_freq": True, "live": True},
        {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
         "unit": "dBm/MHz", "snap_role": "power", "default": -20.0, "live": True},
        {"dest": "bw", "flags": ["--bw"], "type": "float", "step": 0.1, "unit": "MHz",
         "default": 20.0, "min": 0.001, "max": 55.0, "live": True},
    ],
}
_CHIRP_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"fm_chirp": {
    "min_power_dbm": -26.76, "max_power_dbm": -16.71, "quantity": "spectral density",
    "operating_plane": "sdr_output", "amplitude": 0.5,
    "artifact": _artifact(_density_reported())}}}


class ChirpClient(FakeClient):
    def get_tasks_yaml(self):
        return _CHIRP_YAML

    def get_script_params(self, name):
        return _CHIRP_PARAMS


def _companion_cards(dlg):
    return [c for c in dlg._form.findChildren(QFrame)
            if c.objectName() == "pwrCompanionCard"]


def test_livetune_offers_power_companions_for_declared_laws():
    dlg = LiveTuneDialog(FakeHub(ChirpClient(cal=_CHIRP_CAL)), "u", "chirp")
    cards = _companion_cards(dlg)
    btns = [b for b in dlg._form.findChildren(QPushButton)
            if b.objectName() == "pwrControlIn"]
    # controlled in the measured density → the full-bandwidth-power quantity is a companion.
    assert len(cards) == 1
    assert len(btns) == len(cards)                     # each companion promotable to primary
    names = {v["name"] for v in dlg._form._power_views()}
    assert "Full-bandwidth (total) power" in names
    assert "spectral density" in names


def test_livetune_control_in_switches_the_power_quantity():
    dlg = LiveTuneDialog(FakeHub(ChirpClient(cal=_CHIRP_CAL)), "u", "chirp")
    assert dlg._form._selected_view()["name"] == "spectral density"   # base (measured) axis
    btn = next(b for b in dlg._form.findChildren(QPushButton)
               if b.objectName() == "pwrControlIn")
    btn.click()
    assert dlg._form._selected_view()["name"] == "Full-bandwidth (total) power"
