"""Client gate for the stage-limit-through-limiting-reading feature (agent >= 1.13.0,
capability calibration-limit-through-reading). A document whose stage limit is gauged through a
NON-TRIVIAL limiting reading must not be saved to an older agent — it would compare the dBm
ceiling against the measured quantity and resolve a ceiling that is too high (over-power)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import CalibrationPanel
from tests.test_calibration_panel import FakeHub, FakeClient

_app = QApplication.instance() or QApplication([])

FBW = {"id": "fbw", "name": "total power", "in": "density", "out": "abs", "k": 10.0}
# every capability the doc needs EXCEPT the new one, so the new gate is what fires in isolation.
_OTHER_CAPS = ["calibration-power-bridges", "calibration-measurement-quantity"]
_ALL_CAPS = _OTHER_CAPS + ["calibration-limit-through-reading"]


def _doc(*, limiting=None, with_limit=True):
    sig = {"curves": {"sdr_output": {"interp": "linear", "points": [
        {"gain_db": 40, "power_dbm": -30}, {"gain_db": 74, "power_dbm": 4}]}},
        "center_freq_hz": 1.5e9}
    if limiting is not None:
        sig["limiting"] = limiting
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "sdr_output",
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "limits": ([{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}]
                       if with_limit else []),
            "planes": {"sdr_output": {"type": "measured", "quantity": "spectral density"}},
        },
        "signals": {"sig": sig},
    }


def test_stage_limit_via_reading_gated_without_capability():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_OTHER_CAPS)))
    p._set_doc(_doc(limiting={"kind": "law", "law": FBW}))
    assert p._doc_uses_limit_through_reading(p._doc) is True
    assert p._blocks_on_limit_through_reading() is True
    p2 = CalibrationPanel("u", FakeHub(FakeClient(caps=_ALL_CAPS)))
    p2._set_doc(_doc(limiting={"kind": "law", "law": FBW}))
    assert p2._blocks_on_limit_through_reading() is False


def test_no_stage_limit_is_not_gated():
    # a nontrivial limiting reading but no stage limit → nothing to gauge through it
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_OTHER_CAPS)))
    p._set_doc(_doc(limiting={"kind": "law", "law": FBW}, with_limit=False))
    assert p._doc_uses_limit_through_reading(p._doc) is False
    assert p._blocks_on_limit_through_reading() is False


def test_trivial_limiting_is_not_gated():
    # a plain "same as measured" limiting doesn't change the gauging → no newer agent needed
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_OTHER_CAPS)))
    p._set_doc(_doc(limiting={"kind": "same"}))
    assert p._doc_uses_limit_through_reading(p._doc) is False
    assert p._blocks_on_limit_through_reading() is False
