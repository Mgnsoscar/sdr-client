"""Client support for the measurement de-embed (docs/calibration-v2.md §14): a source
plane's `measurement_deembed` survives a doc → form → doc round-trip, and saving one to an
agent that lacks the capability is gated."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import CalibrationPanel
from tests.test_calibration_panel import FakeHub, FakeClient

_app = QApplication.instance() or QApplication([])


def _doc(deembed="sa_cable"):
    plane = {"type": "measured", "quantity": "power"}
    if deembed is not None:
        plane["measurement_deembed"] = deembed
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "sdr_output",
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
            "planes": {"sdr_output": plane},
        },
        "signals": {"sig": {"curves": {"sdr_output": {"interp": "linear", "points": [
            {"gain_db": 40, "power_dbm": -31}, {"gain_db": 74, "power_dbm": 3}]}}}},
    }


def test_deembed_round_trips():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc("sa_cable"))
    out = p._read_form(strict=False)
    assert out["chain"]["planes"]["sdr_output"]["measurement_deembed"] == "sa_cable"


def test_save_gated_without_capability():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_doc("sa_cable"))
    assert p._doc_uses_deembed(p._doc) is True
    assert p._blocks_on_deembed() is True
    p2 = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-measurement-deembed"])))
    p2._set_doc(_doc("sa_cable"))
    assert p2._blocks_on_deembed() is False


def test_no_deembed_not_gated():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_doc(None))
    assert p._doc_uses_deembed(p._doc) is False
    assert p._blocks_on_deembed() is False
