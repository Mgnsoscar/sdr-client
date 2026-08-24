"""Calibration panel × component catalog (v2): a derived plane can reference a library
component instead of a constant Δ dB, and a signal carries center_freq_hz — both round
-trip through the Editor form."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from state import ComponentCatalog
from ui.calibration_panel import CalibrationPanel

_app = QApplication.instance() or QApplication([])


class FakeClient:
    unit_type = "broadcaster"
    unit_id = "unit_1"

    def supports(self, cap):
        return False


class FakeFleet:
    def __init__(self, c):
        self._c = c

    def get(self, h):
        return self._c


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.fleet = FakeFleet(FakeClient())


def _pts(pairs):
    return [{"gain_db": g, "power_dbm": p} for g, p in pairs]


def _doc(cable_spec):
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "antenna_eirp",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5}],
            "planes": {
                "sdr_output":       {"type": "measured", "quantity": "tp"},
                "amplifier_output": {"type": "measured", "quantity": "mlp"},
                "cable_output":     cable_spec,
                "antenna_eirp":     {"type": "derived", "from": "cable_output",
                                     "component": "patch_a", "quantity": "EIRP"},
            }},
        "signals": {"mock": {"amplitude": 0.8, "center_freq_hz": 1.575e9, "curves": {
            "sdr_output":       {"points": _pts([(40, -36), (74, -2.5)])},
            "amplifier_output": {"points": _pts([(40, -6), (74, 24)])},
        }}},
    }


def _panel(tmp_path):
    p = CalibrationPanel("u", FakeHub())
    p._catalog = ComponentCatalog(path=tmp_path / "components.json")
    p._catalog.put("cable_a", "cable", [[1e9, -2.0], [2e9, -3.0]])
    p._catalog.put("patch_a", "antenna", [[0, 6.0]])
    return p


def test_component_reference_round_trips(tmp_path):
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"}))
    p._tabs.setCurrentIndex(0)                       # Editor tab
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["cable_output"]["component"] == "cable_a"
    assert "delta_db" not in planes["cable_output"]  # component supplies Δ dB, not a constant
    assert planes["antenna_eirp"]["component"] == "patch_a"


def test_center_freq_round_trips(tmp_path):
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"}))
    p._tabs.setCurrentIndex(0)
    out = p._read_form(strict=True)
    assert out["signals"]["mock"]["center_freq_hz"] == 1.575e9


def test_constant_delta_still_supported(tmp_path):
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "delta_db": -1.8}))
    p._tabs.setCurrentIndex(0)
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["cable_output"]["delta_db"] == -1.8
    assert "component" not in planes["cable_output"]


def test_missing_center_freq_is_omitted(tmp_path):
    p = _panel(tmp_path)
    doc = _doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"})
    doc["signals"]["mock"].pop("center_freq_hz")
    p._set_doc(doc)
    p._tabs.setCurrentIndex(0)
    assert "center_freq_hz" not in p._read_form(strict=True)["signals"]["mock"]
