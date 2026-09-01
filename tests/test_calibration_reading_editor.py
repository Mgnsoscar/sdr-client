"""The calibration panel's reported/limiting bridge editor (docs/calibration-v2.md §13):
the reading blocks on the operating (last) plane survive a doc → form → doc round-trip, and
the _reading_block normalizer drops trivial defaults while preserving laws + caps."""
import os

import pytest

from ui.calibration_panel import _reading_block

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import CalibrationPanel
from tests.test_calibration_panel import FakeHub, FakeClient

_app = QApplication.instance() or QApplication([])


# ── _reading_block normalizer ─────────────────────────────────────────────────

def test_trivial_same_is_dropped():
    assert _reading_block({}) is None
    assert _reading_block({"kind": "same"}) is None
    assert _reading_block(None) is None


def test_same_with_offset_kept():
    assert _reading_block({"kind": "same", "k": 60.0, "unit": "dBm/MHz"}) == {
        "kind": "same", "k": 60.0, "unit": "dBm/MHz"}


def test_law_block_keeps_embedded_law_and_cap():
    law = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
           "param": "bw", "coeff": 10.0, "ref": 1.0}
    b = _reading_block({"kind": "law", "law": law, "unit": "dBm",
                        "quantity": "total power", "max_dbm": 30.0})
    assert b["kind"] == "law" and b["law"]["id"] == "fbw"
    assert b["unit"] == "dBm" and b["quantity"] == "total power" and b["max_dbm"] == 30.0


def test_law_without_law_dict_is_dropped():
    assert _reading_block({"kind": "law"}) is None


# ── round-trip through the panel ────────────────────────────────────────────────

FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}


def _doc_with_reading():
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "antenna",
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "spectral density"},
                "antenna": {"type": "derived", "from": "sdr_output", "delta_db": 3.0,
                            "reported": {"kind": "law", "law": FBW, "unit": "dBm",
                                         "quantity": "full-bandwidth power"},
                            "limiting": {"kind": "same", "max_dbm": 30.0}},
            },
        },
        "signals": {"fm_chirp": {"curves": {
            "sdr_output": {"interp": "linear", "points": [
                {"gain_db": 40, "power_dbm": -30}, {"gain_db": 74, "power_dbm": 4}]}}}},
    }


def test_reading_blocks_round_trip():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc_with_reading())
    out = p._read_form(strict=False)
    ant = out["chain"]["planes"]["antenna"]
    assert ant["reported"]["kind"] == "law"
    assert ant["reported"]["law"]["id"] == "fbw"
    assert ant["reported"]["unit"] == "dBm"
    assert ant["reported"]["quantity"] == "full-bandwidth power"
    assert ant["limiting"]["max_dbm"] == 30.0


def test_save_gated_on_capability():
    # old agent (no capability) → a bridged document is blocked from saving
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_doc_with_reading())
    assert p._doc_uses_power_bridges(p._doc) is True
    assert p._blocks_on_power_bridges() is True
    # a capable agent → not blocked
    p2 = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-power-bridges"])))
    p2._set_doc(_doc_with_reading())
    assert p2._blocks_on_power_bridges() is False


def test_plain_document_not_gated():
    doc = _doc_with_reading()
    for k in ("reported", "limiting"):
        doc["chain"]["planes"]["antenna"].pop(k, None)
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(doc)
    assert p._doc_uses_power_bridges(p._doc) is False
    assert p._blocks_on_power_bridges() is False
