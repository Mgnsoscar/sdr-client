"""Client side of measured-curve EXTRAPOLATION (agent 1.14.0): the per-signal picker on
the measured-points dialog serializes signals.<id>.curves.<plane>.extrapolate, and saving
a document that uses it is gated on the agent capability (else the unit would clamp and
deliver a different power than the client showed)."""
import json

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import CalibrationPanel, CAL_EXTRAPOLATE_CAPABILITY

_app = QApplication.instance() or QApplication([])


class FakeClient:
    def __init__(self, caps=()):
        self._caps = list(caps)
        self.uploaded = []

    def get_calibration(self):
        return None

    def get_components(self):
        return ""

    def upload_file(self, name, content):
        self.uploaded.append((name, content))
        return {"saved": "calibration.json", "calibration": {}}

    def upload_components(self, content):
        return {"saved": "components.yaml"}

    def supports(self, cap):
        return cap in self._caps


class FakeFleet:
    def __init__(self, c):
        self._c = c

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
        except Exception as exc:                     # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _doc(extrapolate=None):
    curve = {"points": [{"gain_db": 40, "power_dbm": -36}, {"gain_db": 74, "power_dbm": -2.5}]}
    if extrapolate is not None:
        curve["extrapolate"] = extrapolate
    return {
        "schema_version": 1, "unit_id": "u1", "unit_type": "broadcaster",
        "chain": {"gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
                  "operating_plane": "sdr_output",
                  "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp"}],
                  "planes": {"sdr_output": {"type": "measured", "quantity": "dBm"}}},
        "defaults": {"amplitude": 0.5},
        "signals": {"mock": {"curves": {"sdr_output": curve}}},
    }


# ── the doc-uses helper (static, pure) ────────────────────────────────────────

def test_doc_uses_extrapolate_detects_a_non_none_mode():
    assert CalibrationPanel._doc_uses_extrapolate(_doc("down")) is True
    assert CalibrationPanel._doc_uses_extrapolate(_doc("both")) is True
    assert CalibrationPanel._doc_uses_extrapolate(_doc(None)) is False
    assert CalibrationPanel._doc_uses_extrapolate(_doc("none")) is False


# ── form round-trip: the picker seeds the table and serializes back ───────────

def test_form_seeds_and_serializes_extrapolate():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc("down"))
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    assert tbl._extrapolate == "down"                       # seeded from the stored curve
    out = p._read_form(strict=True)
    assert out["signals"]["mock"]["curves"]["sdr_output"]["extrapolate"] == "down"

    # Changing the picker to None drops the key (keeps the doc clean); "up" is written.
    tbl._extrapolate = "none"
    assert "extrapolate" not in p._read_form(strict=True)["signals"]["mock"]["curves"]["sdr_output"]
    tbl._extrapolate = "up"
    assert p._read_form(strict=True)["signals"]["mock"]["curves"]["sdr_output"]["extrapolate"] == "up"


def test_none_document_never_writes_the_key():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc(None))
    assert "extrapolate" not in p._read_form(strict=True)["signals"]["mock"]["curves"]["sdr_output"]


# ── the save-time capability gate ─────────────────────────────────────────────

def test_save_blocks_when_agent_lacks_capability():
    client = FakeClient(caps=())                            # agent predates 1.14.0
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc("down"))
    assert p._on_save() is False
    assert not client.uploaded
    assert "1.14.0" in p._status.text()


def test_save_allowed_when_agent_supports_it():
    client = FakeClient(caps=[CAL_EXTRAPOLATE_CAPABILITY])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc("down"))
    assert p._on_save() is True
    assert len(client.uploaded) == 1
    sent = json.loads(client.uploaded[0][1])
    assert sent["signals"]["mock"]["curves"]["sdr_output"]["extrapolate"] == "down"
