"""Runtime auto-commanding of active components (docs/calibration-v2.md).

Requesting a calibrated absolute --power on a transmit task drives BOTH the SDR (via the
transmit script's own PowerMap) and each linked active-component task (e.g. a step
attenuator): the client sets the attenuator task's parameter to the SDR-first realization's
value alongside starting/tuning the transmit task. This covers the Run dialog (visible +
quick play) and the live Tune dialog."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from state.power_fold import active_settings
from ui.run_task_dialog import RunTaskDialog
from ui.live_tune_dialog import LiveTuneDialog

_app = QApplication.instance() or QApplication([])


# SDR −40..0 dBm + a 0..95 dB / 0.25 dB step attenuator (an active component) ⇒ −135..0 dBm.
ACTIVE_ART = {
    "curve": [[0.0, -40.0], [40.0, 0.0]],
    "min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0,
    "active_components": [{
        "plane": "atten_out", "task": "atten_set", "param": "attenuation",
        "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
        "step_db": 0.25, "engage_pct": 0.0}],
}
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "atten_out", "quantity": "power",
    "min_gain_db": 0.0, "max_gain_db": 40.0,
    "min_power_dbm": -135.0, "max_power_dbm": 0.0, "artifact": ACTIVE_ART}}}

PARAMS = {"calibration_signal": "mock", "params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
     "min": -140.0, "max": 60.0, "default": -20.0, "help": "power", "live": True},
    {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
     "min": 0.0, "max": 40.0, "help": "gain", "live": True},
]}


def _yaml(power="-100"):
    return ("tasks:\n"
            "  - name: tx\n"
            f"    command: [python3, mock_tx.py, --power, \"{power}\"]\n"
            "    env: { SDR_CAL_SIGNAL_ID: mock }\n")


class FakeClient:
    def __init__(self, yaml=None, cal=CAL, params=PARAMS):
        self._yaml, self._cal, self._params = yaml or _yaml(), cal, params
        self.started = []          # (name, StartRequest|None)
        self.set_params = []       # (name, values, wait)
        self.updated = []

    def get_tasks_yaml(self):
        return self._yaml

    def get_script_params(self, name):
        return self._params

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def get_task_params(self, name):
        return {"current": {"power": -100.0}, "applied": {}}

    def start_task(self, name, req=None):
        self.started.append((name, req))
        return {}

    def set_task_params(self, name, values, wait=1.0):
        self.set_params.append((name, dict(values), wait))
        return {"ok": True, "applied": values, "rejected": {}, "pending": []}

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


def _atten_sets(client):
    return [(n, v) for n, v, _w in client.set_params if n == "atten_set"]


# ── the pure helper ───────────────────────────────────────────────────────────────

def test_active_settings_realizes_the_attenuator_value():
    s = active_settings({"artifact": ACTIVE_ART}, -100.0)
    assert s == [{"plane": "atten_out", "task": "atten_set", "param": "attenuation",
                  "applied_db": -60.0, "value": 60.0}]
    assert active_settings({"artifact": ACTIVE_ART}, None) == []      # relative mode
    assert active_settings({}, -100.0) == []                         # no artifact
    passive = {"artifact": {"curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0,
                            "max_gain_db": 40.0, "gain_step_db": 1.0}}
    assert active_settings(passive, -20.0) == []                     # no active components


# ── Run dialog ────────────────────────────────────────────────────────────────────

def test_run_commands_attenuator_alongside_transmit():
    client = FakeClient()
    dlg = RunTaskDialog(FakeHub(client), "u", "tx")
    dlg._on_run()
    assert client.started and client.started[-1][0] == "tx"          # transmit started
    _, req = client.started[-1]
    pf = next(f for f in ("--power", "-Power") if f in req.args)
    assert req.args[req.args.index(pf) + 1] == "-100"
    assert _atten_sets(client) == [("atten_set", {"attenuation": 60.0})]  # attenuator set


def test_run_positions_attenuator_before_starting_transmit():
    # Safety: the attenuator must be commanded before the transmit task starts, so the SDR
    # never briefly transmits with the attenuator still at rest.
    client = FakeClient()
    order = []
    _orig_start, _orig_set = client.start_task, client.set_task_params
    client.start_task = lambda n, r=None: (order.append(("start", n)), _orig_start(n, r))[1]
    client.set_task_params = lambda n, v, w=1.0: (order.append(("set", n)), _orig_set(n, v, w))[1]
    dlg = RunTaskDialog(FakeHub(client), "u", "tx")
    dlg._on_run()
    assert order.index(("set", "atten_set")) < order.index(("start", "tx"))


def test_quick_play_commands_attenuator():
    # The unit's play button (quick mode) also positions the attenuator for the stored --power.
    client = FakeClient()
    RunTaskDialog(FakeHub(client), "u", "tx", quick=True)
    assert client.started and client.started[-1][0] == "tx"
    assert _atten_sets(client) == [("atten_set", {"attenuation": 60.0})]


def test_run_relative_gain_mode_commands_no_attenuator():
    # A relative --gain run has no absolute power to realize, so nothing is sent to the
    # component task (the SDR gain goes out raw).
    client = FakeClient(yaml=(
        "tasks:\n"
        "  - name: tx\n"
        "    command: [python3, mock_tx.py, --gain, \"20\"]\n"
        "    env: { SDR_CAL_SIGNAL_ID: mock }\n"))
    dlg = RunTaskDialog(FakeHub(client), "u", "tx")
    dlg._on_run()
    assert client.started
    assert _atten_sets(client) == []


def test_run_uncalibrated_commands_no_attenuator(monkeypatch):
    client = FakeClient(cal=AgentHTTPError("u", 404, "none"))
    dlg = RunTaskDialog(FakeHub(client), "u", "tx")
    # uncalibrated absolute → relative-gain prompt path; but no cal bounds ⇒ no attenuator.
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(lambda *a, **k: (20.0, True)))
    dlg._on_run()
    assert _atten_sets(client) == []


# ── Tune dialog ───────────────────────────────────────────────────────────────────

def test_tune_commands_attenuator_alongside_retune():
    client = FakeClient()
    dlg = LiveTuneDialog(FakeHub(client), "u", "tx")
    # Seeded from get_task_params → power -100. Applying retunes the transmit AND the attenuator.
    dlg._apply()
    tx_sets = [(n, v) for n, v, _w in client.set_params if n == "tx"]
    assert tx_sets and "power" in tx_sets[-1][1]
    assert _atten_sets(client) == [("atten_set", {"attenuation": 60.0})]
