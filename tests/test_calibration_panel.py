"""Offscreen widget tests for the CalibrationPanel: it renders the resolved summary,
handles the not-calibrated (404) case, and surfaces an agent validation rejection
without saving. A fake hub runs the async calls synchronously and emits task_done."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from api.client import AgentHTTPError
from ui.calibration_panel import CalibrationPanel, _fmt_range

_app = QApplication.instance() or QApplication([])


class FakeClient:
    def __init__(self, cal=None, upload=None):
        self._cal = cal
        self._upload = upload or {"saved": "calibration.json", "calibration": {}}
        self.uploaded = []

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def upload_file(self, name, content):
        if isinstance(self._upload, Exception):
            raise self._upload
        self.uploaded.append((name, content))
        return self._upload


class FakeFleet:
    def __init__(self, client):
        self._c = client

    def get(self, host):
        return self._c


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


def test_fmt_range():
    assert _fmt_range(0.0, 74.0, "dB") == "0 – 74 dB"
    assert _fmt_range(None, 1, "dB") == "—"


def test_panel_renders_calibrated_summary():
    cal = {
        "unit_type": "broadcaster", "valid": True,
        "document": {"schema_version": 1},
        "signals": {"gps_l1_mcode": {
            "operating_plane": "antenna_eirp", "quantity": "EIRP",
            "min_gain_db": 0.0, "max_gain_db": 74.0,
            "min_power_dbm": -40.0, "max_power_dbm": 28.2}},
    }
    p = CalibrationPanel("unit_x", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "gps_l1_mcode"
    assert p._table.item(0, 1).text() == "antenna_eirp"
    assert "calibrated" in p._status.text()
    assert p._download_btn.isEnabled()


def test_panel_not_calibrated_shows_hint():
    hub = FakeHub(FakeClient(cal=AgentHTTPError("unit_x", 404, "none")))
    p = CalibrationPanel("unit_x", hub)
    p.on_shown()
    assert p._table.rowCount() == 0
    assert "not calibrated" in p._status.text()
    assert not p._download_btn.isEnabled()


def test_panel_invalid_stored_document():
    cal = {"unit_type": "broadcaster", "valid": False,
           "document": {"schema_version": 1}, "error": "curve not invertible"}
    p = CalibrationPanel("unit_x", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert p._table.rowCount() == 0
    assert "INVALID" in p._status.text()


def test_panel_save_rejection_surfaces_reason(monkeypatch):
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("msg", a)))
    client = FakeClient(upload=AgentHTTPError("unit_x", 400, "curve not invertible"))
    p = CalibrationPanel("unit_x", FakeHub(client))
    p._view.setPlainText('{"schema_version": 1}')
    p._on_save()
    assert "rejected" in p._status.text()
    assert client.uploaded == []                    # nothing stored
    assert "curve not invertible" in seen["msg"][2] # the agent's reason reached the dialog


def test_panel_save_invalid_json_is_local_guard():
    client = FakeClient()
    p = CalibrationPanel("unit_x", FakeHub(client))
    p._view.setPlainText("{ not json ")
    p._on_save()
    assert "not valid JSON" in p._status.text()
    assert client.uploaded == []                    # never sent to the unit
