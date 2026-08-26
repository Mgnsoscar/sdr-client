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
    # Persisted as an env FALLBACK — the command KEEPS its --power so calibrating the unit
    # auto-reverts to the authored dBm value.
    assert client.updated, "the gain should be persisted to the task"
    _, spec = client.updated[-1]
    assert spec["env"]["SDR_CAL_FALLBACK_GAIN"] == "55"
    assert any(f in spec["command"] for f in ("--power", "-Power"))   # authored power retained


def test_persisted_fallback_prefills_and_does_not_reprompt(monkeypatch):
    # A task that already carries SDR_CAL_FALLBACK_GAIN pre-fills the relative gain, so
    # Start runs without prompting again.
    yaml = (
        "tasks:\n"
        "  - name: mocktask\n"
        "    command: [python3, mock_tx.py, --power, \"40\"]\n"
        "    env: { SDR_CAL_SIGNAL_ID: mock, SDR_CAL_FALLBACK_GAIN: \"42\" }\n")
    client = FakeClient(yaml=yaml, params=PARAMS_PG, cal=AgentHTTPError("u", 404, "none"))
    dlg = RunTaskDialog(FakeHub(client), "u", "mocktask")
    from PyQt6.QtWidgets import QInputDialog
    called = {"n": 0}
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (called.__setitem__("n", called["n"] + 1), (0.0, False))[1]))
    dlg._on_run()
    assert called["n"] == 0, "should not prompt when a fallback gain is already persisted"
    assert client.started
    _, req = client.started[-1]
    assert "42" in req.args and any(f in req.args for f in ("--gain", "-Gain"))


def test_start_uncalibrated_cancel_does_not_run(monkeypatch):
    dlg, client = _uncal_dialog()
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (0.0, False)))   # cancelled
    dlg._on_run()
    assert client.started == [] and client.updated == []


# ── quick-start (the unit's play button, headless RunTaskDialog) ─────────────────────
# The play button must behave like Run…: an uncalibrated absolute-power task can't run its
# authored --power (the script refuses it), so quick mode prompts + persists a stop-gap gain.
# FakeHub.run_async is synchronous, so constructing with quick=True drives the whole flow.

def test_quick_start_uncalibrated_prompts_and_persists(monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (55.0, True)))
    client = FakeClient(yaml=YAML_ABS40, params=PARAMS_PG,
                        cal=AgentHTTPError("u", 404, "none"))
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert client.started, "quick-start should have started the task"
    _, req = client.started[-1]
    assert req is not None and any(f in req.args for f in ("--gain", "-Gain")) and "55" in req.args
    assert not any(f in req.args for f in ("--power", "-Power"))   # absolute never sent
    _, spec = client.updated[-1]
    assert spec["env"]["SDR_CAL_FALLBACK_GAIN"] == "55"
    assert any(f in spec["command"] for f in ("--power", "-Power"))   # authored power retained


def test_quick_start_uncalibrated_cancel_does_not_start(monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getDouble",
                        staticmethod(lambda *a, **k: (0.0, False)))   # cancelled
    client = FakeClient(yaml=YAML_ABS40, params=PARAMS_PG,
                        cal=AgentHTTPError("u", 404, "none"))
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert client.started == [] and client.updated == []


def test_quick_start_persisted_fallback_starts_without_prompt(monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    called = {"n": 0}
    monkeypatch.setattr(QInputDialog, "getDouble", staticmethod(
        lambda *a, **k: (called.__setitem__("n", called["n"] + 1), (0.0, False))[1]))
    yaml = (
        "tasks:\n"
        "  - name: mocktask\n"
        "    command: [python3, mock_tx.py, --power, \"40\"]\n"
        "    env: { SDR_CAL_SIGNAL_ID: mock, SDR_CAL_FALLBACK_GAIN: \"42\" }\n")
    client = FakeClient(yaml=yaml, params=PARAMS_PG, cal=AgentHTTPError("u", 404, "none"))
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert called["n"] == 0, "a persisted fallback should not re-prompt on quick-start"
    assert client.started
    _, req = client.started[-1]
    assert "42" in req.args and any(f in req.args for f in ("--gain", "-Gain"))


_YAML_INRANGE = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"10\"]\n"     # within CAL's -1.8..28.2
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n")


def test_quick_start_calibrated_in_range_starts_stored_command():
    # Calibrated task whose stored --power is IN range: quick-start runs the stored
    # command/env untouched — start_task called with no StartRequest.
    client = FakeClient(yaml=_YAML_INRANGE, params=PARAMS_PG, cal=CAL)
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert client.started == [("mocktask", None)]
    assert client.updated == []


def _clamp_yaml(power):
    return ("tasks:\n"
            "  - name: mocktask\n"
            f"    command: [python3, mock_tx.py, --power, \"{power}\"]\n"
            "    env: { SDR_CAL_SIGNAL_ID: mock }\n")


def test_quick_start_calibrated_over_range_clamps_and_persists():
    # #Clipping: a calibrated task whose stored --power exceeds the unit's max (28.2) is
    # clamped to the limit for the run (replace_args) AND the stored command is healed to it,
    # so the deployed task no longer holds a level the unit can't produce.
    client = FakeClient(yaml=_clamp_yaml("40"), params=PARAMS_PG, cal=CAL)
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert client.started, "should still start"
    _, req = client.started[-1]
    assert req is not None and "--power" in req.args
    assert req.args[req.args.index("--power") + 1] == "28.2"       # clamped to the max
    _, spec = client.updated[-1]                                    # stored command healed
    assert "28.2" in spec["command"] and "40" not in spec["command"]


def test_quick_start_calibrated_under_range_clamps_up_to_min():
    client = FakeClient(yaml=_clamp_yaml("-30"), params=PARAMS_PG, cal=CAL)
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    _, req = client.started[-1]
    assert req.args[req.args.index("--power") + 1] == "-1.8"        # clamped up to the min


def test_quick_start_no_optin_starts_stored_command():
    # A task that doesn't opt into calibration starts from its stored command as before.
    client = FakeClient(yaml=YAML_NO_OPTIN, params=PARAMS_PG, cal=CAL)
    RunTaskDialog(FakeHub(client), "u", "mocktask", quick=True)
    assert client.started == [("mocktask", None)]
