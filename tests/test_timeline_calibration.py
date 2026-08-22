"""TimelineEditor calibration context: task→signal parsing, per-task resolved
bounds from the target unit's /calibration, and absolute_allowed gating. This is
what lets a plan's / sequence's tune steps offer absolute (calibrated) power."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from ui.timeline_editor import TimelineEditor, task_signals_from_yaml

_app = QApplication.instance() or QApplication([])

YAML = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
    "  - name: plain\n"
    "    command: [python3, other.py]\n"
)
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, cal=CAL):
        super().__init__()
        client = type("C", (), {"get_calibration": lambda self_: (
            (_ for _ in ()).throw(cal) if isinstance(cal, Exception) else cal)})()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def test_task_signals_from_yaml():
    assert task_signals_from_yaml(YAML) == {"mocktask": "mock"}
    assert task_signals_from_yaml("") == {}


def test_calibrated_bounds_for_task():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(), "unit-1")      # fetch resolves synchronously here
    assert t.absolute_allowed() is True
    assert t.cal_bounds_for_task("mocktask") == CAL["signals"]["mock"]
    assert t.cal_bounds_for_task("plain") is None      # no signal → no bounds


def test_uncalibrated_unit_no_bounds():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(cal=AgentHTTPError("u", 404, "none")), "unit-1")
    assert t.absolute_allowed() is True                # a unit is targeted…
    assert t.cal_bounds_for_task("mocktask") is None   # …but it isn't calibrated


def test_no_unit_means_relative_only():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(), "")                    # library: no target unit
    assert t.absolute_allowed() is False
    assert t.cal_bounds_for_task("mocktask") is None
