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


# ── §14.1: per-signal + source-bias de-embed (agent 1.14.0) ──────────────────────────────────────

def _doc_ps(signal_deembed="sa_cable", source_bias_deembed=None):
    """A doc whose de-embed lives on the SIGNAL's own curve (not the plane), optionally with a
    source-bias de-embed too."""
    curve = {"interp": "linear", "points": [{"gain_db": 40, "power_dbm": -31},
                                            {"gain_db": 74, "power_dbm": 3}]}
    if signal_deembed is not None:
        curve["measurement_deembed"] = signal_deembed
    doc = {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "sdr_output",
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "power"}},
        },
        "signals": {"sig": {"center_freq_hz": 1.5e9,
                            "curves": {"sdr_output": curve}}},
    }
    if source_bias_deembed is not None:
        doc["source_bias"] = {"power_by_freq": [[1e9, 0.0], [2e9, -1.0]],
                              "measurement_deembed": source_bias_deembed}
    return doc


def test_per_signal_curve_deembed_round_trips():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc_ps(signal_deembed="sa_cable"))
    out = p._read_form(strict=False)
    assert out["signals"]["sig"]["curves"]["sdr_output"]["measurement_deembed"] == "sa_cable"
    # and it is NOT written onto the plane (per-signal placement, not plane-level)
    assert "measurement_deembed" not in out["chain"]["planes"]["sdr_output"]


def test_per_signal_picker_sets_and_clears():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc_ps(signal_deembed=None))                 # no cable to start
    p._f["signals"]["sig"]["deembed"]["sdr_output"] = "sa_cable"   # picker chooses one
    assert p._read_form(strict=False)["signals"]["sig"]["curves"]["sdr_output"][
        "measurement_deembed"] == "sa_cable"
    p._f["signals"]["sig"]["deembed"]["sdr_output"] = ""      # picker → "(none)"
    assert "measurement_deembed" not in \
        p._read_form(strict=False)["signals"]["sig"]["curves"]["sdr_output"]


def test_source_bias_deembed_round_trips():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc_ps(signal_deembed=None, source_bias_deembed="sa_cable"))
    out = p._read_form(strict=False)
    assert out["source_bias"]["measurement_deembed"] == "sa_cable"


def test_per_signal_and_source_bias_gate_on_the_new_capability():
    # per-signal curve de-embed
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_doc_ps(signal_deembed="sa_cable"))
    assert p._doc_uses_deembed_per_signal(p._doc) is True
    assert p._blocks_on_deembed_per_signal() is True
    ok = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-deembed-per-signal"])))
    ok._set_doc(_doc_ps(signal_deembed="sa_cable"))
    assert ok._blocks_on_deembed_per_signal() is False
    # source-bias de-embed gates the same way
    sb = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    sb._set_doc(_doc_ps(signal_deembed=None, source_bias_deembed="sa_cable"))
    assert sb._doc_uses_deembed_per_signal(sb._doc) is True
    assert sb._blocks_on_deembed_per_signal() is True


def test_no_per_signal_deembed_not_gated():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_doc_ps(signal_deembed=None))
    assert p._doc_uses_deembed_per_signal(p._doc) is False
    assert p._blocks_on_deembed_per_signal() is False


def test_deleting_a_deembed_cable_keeps_it_on_the_unit_that_uses_it():
    # The owner's requirement: delete a measurement cable from the shared library and it must still
    # persist on a unit whose signal was measured through it. _push_components pushes the catalog but
    # KEEPS a referenced part the catalog no longer has, taken from the unit's own components.yaml.
    from state import ComponentCatalog, dump_components

    class _Unit(FakeClient):
        def get_components(self):                    # the unit already stores the cable
            return dump_components({"sig_cable": {"kind": "cable", "delta_db_by_freq": [[0, -0.5]]}})

    c = _Unit()
    p = CalibrationPanel("u", FakeHub(c))
    p._catalog.put("other", "pad", [[0, -1.0]])      # the client library has NO sig_cable anymore
    p._push_components(c, _doc_ps(signal_deembed="sig_cable"))
    assert c.components_uploaded, "components should have been pushed"
    uploaded = ComponentCatalog.parse_wire(c.components_uploaded[-1])
    assert "sig_cable" in uploaded                   # kept from the unit despite the library delete
    assert "other" in uploaded                       # the current catalog is still pushed
