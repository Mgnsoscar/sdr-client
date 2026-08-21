"""Offscreen widget tests for the CalibrationPanel: the resolved summary, the
not-calibrated / invalid states, agent-rejection handling, and the Editor⇄JSON form
model (round-trip, curve-grid edits, add/remove signal). A fake hub runs the async
calls synchronously and emits task_done."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

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


def _doc():
    return {
        "schema_version": 1, "unit_id": "u1", "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "total in-band power"}},
        },
        "defaults": {"amplitude": 0.8},
        "signals": {"mock": {"amplitude": 0.8, "curves": {
            "sdr_output": {"points": [{"gain_db": 40, "power_dbm": -36},
                                      {"gain_db": 74, "power_dbm": -2.5}]}}}},
    }


# ── display states ───────────────────────────────────────────────────────────────

def test_fmt_range():
    assert _fmt_range(0.0, 74.0, "dB") == "0 – 74 dB"
    assert _fmt_range(None, 1, "dB") == "—"


def test_renders_calibrated_summary():
    cal = {"unit_type": "broadcaster", "valid": True, "document": _doc(),
           "signals": {"mock": {"operating_plane": "sdr_output", "quantity": "total in-band power",
                                 "min_gain_db": 0.0, "max_gain_db": 74.0,
                                 "min_power_dbm": -36.0, "max_power_dbm": -2.5}}}
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "mock"
    assert "calibrated" in p._status.text()
    assert p._download_btn.isEnabled()


def test_not_calibrated_hint():
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=AgentHTTPError("u", 404, "none"))))
    p.on_shown()
    assert p._table.rowCount() == 0
    assert "not calibrated" in p._status.text()
    assert not p._download_btn.isEnabled()


def test_invalid_stored_document():
    cal = {"unit_type": "broadcaster", "valid": False, "document": _doc(),
           "error": "curve not invertible"}
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert "INVALID" in p._status.text()


# ── save paths ───────────────────────────────────────────────────────────────────

def test_save_from_json_rejection_surfaces_reason(monkeypatch):
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("msg", a)))
    client = FakeClient(upload=AgentHTTPError("u", 400, "curve not invertible"))
    p = CalibrationPanel("u", FakeHub(client))
    p._tabs.setCurrentIndex(1)                          # JSON tab authoritative
    p._view.setPlainText('{"schema_version": 1}')
    p._on_save()
    assert "rejected" in p._status.text()
    assert client.uploaded == []
    assert "curve not invertible" in seen["msg"][2]


def test_save_invalid_json_is_local_guard():
    client = FakeClient()
    p = CalibrationPanel("u", FakeHub(client))
    p._tabs.setCurrentIndex(1)
    p._view.setPlainText("{ not json ")
    p._on_save()
    assert "cannot save" in p._status.text()
    assert client.uploaded == []


def test_save_from_form_serializes_document():
    client = FakeClient()
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())                                  # populates the form
    p._tabs.setCurrentIndex(0)                          # Editor authoritative
    p._on_save()
    assert len(client.uploaded) == 1
    import json
    name, content = client.uploaded[0]
    sent = json.loads(content)
    assert sent["chain"]["operating_plane"] == "sdr_output"
    assert sent["signals"]["mock"]["curves"]["sdr_output"]["points"][0]["gain_db"] == 40


# ── form model ───────────────────────────────────────────────────────────────────

def test_form_round_trips_through_widgets():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    out = p._read_form(strict=True)
    assert out["chain"]["gain_limits"] == {"min_gain_db": 0.0, "max_gain_db": 89.75}
    assert out["chain"]["operating_plane"] == "sdr_output"
    assert out["chain"]["limits"] == [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}]
    assert out["signals"]["mock"]["amplitude"] == 0.8
    pts = out["signals"]["mock"]["curves"]["sdr_output"]["points"]
    assert [(pt["gain_db"], pt["power_dbm"]) for pt in pts] == [(40.0, -36.0), (74.0, -2.5)]
    # plane topology is preserved from the model even though the form doesn't edit it
    assert out["chain"]["planes"]["sdr_output"]["type"] == "measured"


def test_curve_grid_edit_is_read_back():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.add_blank_row()
    r = tbl.rowCount() - 1
    tbl.item(r, 0).setText("60")
    tbl.item(r, 1).setText("-16")
    pts = p._read_form(strict=True)["signals"]["mock"]["curves"]["sdr_output"]["points"]
    assert {"gain_db": 60.0, "power_dbm": -16.0} in pts


def test_bad_curve_cell_blocks_save_strictly():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.add_blank_row()
    tbl.item(tbl.rowCount() - 1, 0).setText("not-a-number")
    with pytest.raises(ValueError):
        p._read_form(strict=True)


def test_add_and_remove_signal(monkeypatch):
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("gps_l1", True)))
    p._on_add_signal()
    assert "gps_l1" in p._f["signals"]
    p._remove_signal("gps_l1")
    assert "gps_l1" not in p._f["signals"]


def _full_doc():
    d = _doc()
    d["chain"]["operating_plane"] = "antenna_eirp"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "total in-band power"},
        "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
        "cable_output": {"type": "derived", "from": "amplifier_output", "delta_db": -1.8},
        "antenna_eirp": {"type": "derived", "from": "cable_output", "delta_db": 6.0, "quantity": "EIRP"},
    }
    d["signals"]["mock"]["curves"]["amplifier_output"] = {
        "points": [{"gain_db": 40, "power_dbm": -6}, {"gain_db": 74, "power_dbm": 24}]}
    return d


def test_plane_topology_round_trips_through_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_full_doc())
    out = p._read_form(strict=True)["chain"]["planes"]
    assert out["sdr_output"] == {"type": "measured", "quantity": "total in-band power"}
    assert out["cable_output"] == {"type": "derived", "from": "amplifier_output", "delta_db": -1.8}
    assert out["antenna_eirp"]["type"] == "derived"
    assert out["antenna_eirp"]["from"] == "cable_output"
    assert out["antenna_eirp"]["quantity"] == "EIRP"
    # measured planes drive which curve grids exist per signal
    assert set(p._f["signals"]["mock"]["curves"]) == {"sdr_output", "amplifier_output"}


def test_add_and_remove_plane():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())                                   # one plane: sdr_output
    p._on_add_plane()
    planes = p._read_form(strict=False)["chain"]["planes"]
    assert len(planes) == 2 and "plane" in planes
    # remove the added one via its row
    row = next(r for r in p._f["planes"] if r["name"].text() == "plane")
    p._remove_plane(row)
    assert list(p._read_form(strict=False)["chain"]["planes"]) == ["sdr_output"]


def test_derived_plane_without_delta_blocks_save():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    row = p._f["planes"][0]
    row["type"].setCurrentText("derived")               # sdr_output → derived, no Δ
    # the type change triggers a rebuild; grab the (rebuilt) row and clear Δ
    row = p._f["planes"][0]
    row["delta"].setText("")
    with pytest.raises(ValueError):
        p._read_form(strict=True)


def test_template_seeds_empty_unit():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._on_new_template()
    assert p._doc is not None
    assert p._download_btn.isEnabled()
    assert "mock" in p._f["signals"]
