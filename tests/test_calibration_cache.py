"""Last-known calibration cache: the store itself, and the TimelineEditor falling
back to it when the target unit is OFFLINE (but not when it's online-but-404)."""
import json
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import state.calibration_cache as cc
from api.client import AgentConnectionError, AgentHTTPError
from state.calibration_cache import CalibrationCache
from ui.timeline_editor import TimelineEditor, task_signals_from_yaml

_app = QApplication.instance() or QApplication([])

YAML = ("tasks:\n  - name: mocktask\n    command: [python3, mock_tx.py]\n"
        "    env: { SDR_CAL_SIGNAL_ID: mock }\n")
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, result):
        super().__init__()
        def _get(self_):
            if isinstance(result, Exception):
                raise result
            return result
        client = type("C", (), {"get_calibration": _get})()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


# cal_cache (a tmp-backed CalibrationCache) is provided autouse by tests/conftest.py.


# ── the store ────────────────────────────────────────────────────────────────────

def test_store_put_get_persist(tmp_path):
    path = tmp_path / "c.json"
    c = CalibrationCache(path)
    c.put("unit-1", CAL)
    assert c.get("unit-1") == CAL
    assert c.fetched_at("unit-1") is not None
    # a new instance reads it back from disk
    assert CalibrationCache(path).get("unit-1") == CAL


def test_store_ignores_invalid():
    c = CalibrationCache.__new__(CalibrationCache)
    c._data = {}; c._path = None
    c.put("u", {"valid": False, "error": "bad"})
    assert c.get("u") is None


# ── timeline fallback ──────────────────────────────────────────────────────────────

def _editor():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    return t


def test_online_valid_populates_cache(cal_cache):
    t = _editor()
    t.set_calibration(FakeHub(CAL), "unit-1")
    assert t.cal_bounds_for_task("mocktask") == CAL["signals"]["mock"]
    assert t.cal_is_stale() is False
    assert cal_cache.get("unit-1") == CAL          # cached for later


def test_offline_uses_cache_and_marks_stale(cal_cache):
    cal_cache.put("unit-1", CAL)                    # seen earlier, while online
    t = _editor()
    t.set_calibration(FakeHub(AgentConnectionError("unit-1", "offline")), "unit-1")
    assert t.cal_bounds_for_task("mocktask") == CAL["signals"]["mock"]
    assert t.cal_is_stale() is True                 # served from cache


def test_online_404_ignores_cache(cal_cache):
    cal_cache.put("unit-1", CAL)                    # stale entry exists…
    t = _editor()
    t.set_calibration(FakeHub(AgentHTTPError("unit-1", 404, "none")), "unit-1")
    # …but the unit is reachable and now uncalibrated → no absolute, no stale bounds
    assert t.cal_bounds_for_task("mocktask") is None
    assert t.cal_is_stale() is False
