"""The client adopts the agent's advertised capabilities from /info and feature-gates
on them (client.supports), and the calibration panel uses that instead of sniffing a
404 — while still tolerating an old agent that omits the field."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from api.client import AgentClient
from ui.calibration_panel import CalibrationPanel

_app = QApplication.instance() or QApplication([])


# ── client.supports() / capability parsing ──────────────────────────────────────

def test_info_adopts_capabilities(monkeypatch):
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    monkeypatch.setattr(c, "_request", lambda *a, **k: {
        "hostname": "unit_x", "unit_id": "u", "agent_version": "1.1.4",
        "python_version": "3.11", "tasks": [], "capabilities": ["calibration"]})
    info = c.info()
    assert info.capabilities == ["calibration"]
    assert c.supports("calibration") is True
    assert c.supports("nope") is False


def test_old_agent_without_field_parses(monkeypatch):
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    monkeypatch.setattr(c, "_request", lambda *a, **k: {   # no 'capabilities' key
        "hostname": "unit_x", "unit_id": "u", "agent_version": "1.0.0",
        "python_version": "3.11", "tasks": []})
    c.info()
    assert c.capabilities == []
    assert c.supports("calibration") is False


# ── panel uses the capability, not the 404 ──────────────────────────────────────

class _Client:
    def __init__(self, version, caps):
        self.agent_version = version
        self._caps = caps

    def supports(self, cap):
        return cap in self._caps

    def get_calibration(self):
        # A modern agent that IS calibrated returns a valid view; the panel should
        # never call this reasoning path when caps say calibration is unsupported.
        return {"unit_type": "broadcaster", "valid": True, "signals": {}}


class _Hub(QObject):
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


def test_panel_flags_old_agent_by_capability(monkeypatch):
    # Agent is reachable (agent_version set) but advertises no calibration capability.
    p = CalibrationPanel("u", _Hub(_Client("1.0.9", [])))
    p.on_shown()
    assert "out of date" in p._status.text()


def test_panel_ok_when_capability_present(monkeypatch):
    p = CalibrationPanel("u", _Hub(_Client("1.1.4", ["calibration"])))
    p.on_shown()
    assert "out of date" not in p._status.text()
