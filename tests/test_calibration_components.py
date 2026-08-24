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
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["cable_output"]["component"] == "cable_a"
    assert "delta_db" not in planes["cable_output"]  # component supplies Δ dB, not a constant
    assert planes["antenna_eirp"]["component"] == "patch_a"


def test_center_freq_round_trips(tmp_path):
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"}))
    out = p._read_form(strict=True)
    assert out["signals"]["mock"]["center_freq_hz"] == 1.575e9


def test_constant_delta_still_supported(tmp_path):
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "delta_db": -1.8}))
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["cable_output"]["delta_db"] == -1.8
    assert "component" not in planes["cable_output"]


def test_missing_center_freq_is_omitted(tmp_path):
    p = _panel(tmp_path)
    doc = _doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"})
    doc["signals"]["mock"].pop("center_freq_hz")
    p._set_doc(doc)
    assert "center_freq_hz" not in p._read_form(strict=True)["signals"]["mock"]


def test_save_blocked_when_agent_lacks_component_capability(tmp_path):
    # FakeClient.supports() is False for everything → the agent can't resolve component
    # refs, so Save must refuse with a clear "update the agent" message, not push a doc
    # the unit would reject confusingly.
    p = _panel(tmp_path)
    p._set_doc(_doc({"type": "derived", "from": "amplifier_output", "component": "cable_a"}))
    p._on_save()
    assert "too old" in p._status.text().lower()


def test_constant_delta_not_blocked_without_capability(tmp_path):
    p = _panel(tmp_path)
    doc = _doc({"type": "derived", "from": "amplifier_output", "delta_db": -1.8})
    doc["chain"]["planes"]["antenna_eirp"] = {"type": "derived", "from": "cable_output",
                                              "delta_db": 6.0, "quantity": "EIRP"}
    p._set_doc(doc)
    assert p._blocks_on_components() is False        # no component ref → never blocked


def test_component_dialog_enter_saves_not_new(tmp_path):
    # Pressing Enter in a header field must SAVE the component being edited — not fire the
    # dialog's default button (which used to be "New", discarding the edit into a fresh
    # blank component).
    from ui.component_library_dialog import ComponentLibraryDialog
    cat = ComponentCatalog(path=tmp_path / "components.json")
    dlg = ComponentLibraryDialog(cat)
    dlg._new()
    dlg._id.setText("cable_x")
    dlg._table.set_rows([[1.0e9, -2.0]])
    dlg._desc.returnPressed.emit()                   # Enter in the description field
    assert "cable_x" in cat.ids()                    # saved…
    assert dlg._current == "cable_x"                 # …and still the current component
    # the New/default button must not be the Enter-default that stole the keystroke
    assert dlg._save_btn.isDefault()
