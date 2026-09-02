"""The per-signal calibration signal editor (docs/calibration-ui-redesign §5): each signal
owns its Measurement (quantity + unit) and its Limiting reading; there is no Reported bridge
and no per-signal ceiling (the stage limits list is the dBm cap for every signal).

Covers the _reading_block / _measurement_block normalizers, the doc → form → doc round-trip
on the SIGNAL (not the plane), the migration of a legacy operating-plane limiting default
into each signal, and the "always resolves to dBm" law gating for the Limiting reading."""
import os

import pytest

from ui.calibration_panel import _measurement_block, _reading_block, _unit_family

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.calibration_panel import CalibrationPanel
from tests.test_calibration_panel import FakeHub, FakeClient

_app = QApplication.instance() or QApplication([])

FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}


# ── _reading_block normalizer ─────────────────────────────────────────────────

def test_trivial_same_is_dropped():
    assert _reading_block({}) is None
    assert _reading_block({"kind": "same"}) is None
    assert _reading_block(None) is None


def test_same_with_offset_kept():
    assert _reading_block({"kind": "same", "k": 60.0, "unit": "dBm/MHz"}) == {
        "kind": "same", "k": 60.0, "unit": "dBm/MHz"}


def test_law_block_keeps_embedded_law_and_cap():
    # The normalizer preserves a max_dbm it is handed; _read_form strips the per-signal
    # ceiling separately (see test_per_signal_ceiling_is_dropped_on_save).
    b = _reading_block({"kind": "law", "law": FBW, "unit": "dBm",
                        "quantity": "total power", "max_dbm": 30.0})
    assert b["kind"] == "law" and b["law"]["id"] == "fbw"
    assert b["unit"] == "dBm" and b["quantity"] == "total power" and b["max_dbm"] == 30.0


def test_law_without_law_dict_is_dropped():
    assert _reading_block({"kind": "law"}) is None


def test_own_block_keeps_curve():
    b = _reading_block({"kind": "own",
                        "curve": {"points": [{"gain_db": 40, "power_dbm": -30}]}})
    assert b == {"kind": "own", "curve": {"points": [{"gain_db": 40, "power_dbm": -30}]}}


def test_own_block_without_a_curve_is_dropped():
    assert _reading_block({"kind": "own"}) is None
    assert _reading_block({"kind": "own", "curve": {"points": []}}) is None


# ── _measurement_block normalizer ─────────────────────────────────────────────

def test_measurement_dbm_default_is_dropped():
    assert _measurement_block({}) is None
    assert _measurement_block({"unit": "dBm"}) is None
    assert _measurement_block({"unit": "dBm", "quantity": ""}) is None


def test_measurement_density_kept():
    assert _measurement_block({"unit": "dBm/MHz", "quantity": "Peak density"}) == {
        "quantity": "Peak density", "unit": "dBm/MHz"}


def test_measurement_dbm_with_quantity_keeps_only_quantity():
    # dBm is the default unit — omit it, but keep an operator-facing quantity label.
    assert _measurement_block({"unit": "dBm", "quantity": "Full-band power"}) == {
        "quantity": "Full-band power"}


def test_unit_family():
    assert _unit_family("dBm") == "abs"
    assert _unit_family("dBm/Hz") == _unit_family("dBm/MHz") == _unit_family("dBm/kHz") \
        == "density"
    assert _unit_family("mystery") == "abs"          # unknown ⇒ absolute


# ── round-trips through the panel ─────────────────────────────────────────────

def _base_doc(signal_extra=None):
    sig = {"curves": {"sdr_output": {"points": [
        {"gain_db": 40, "power_dbm": -30}, {"gain_db": 74, "power_dbm": 4}]}}}
    sig.update(signal_extra or {})
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "operating_plane": "sdr_output",
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 74.0},
            "limits": [{"plane": "sdr_output", "max_dbm": 4.0, "reason": "amp"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "power"}},
        },
        "signals": {"fm_chirp": sig},
    }


def _legacy_plane_reading_doc():
    # A pre-redesign document with a shared reported/limiting bridge on the operating plane.
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
                            "limiting": {"kind": "law", "law": FBW, "max_dbm": 30.0}},
            },
        },
        "signals": {"fm_chirp": {
            "measurement": {"unit": "dBm/MHz", "quantity": "Peak density"},
            "curves": {"sdr_output": {"points": [
                {"gain_db": 40, "power_dbm": -30}, {"gain_db": 74, "power_dbm": 4}]}}}},
    }


def test_legacy_operating_plane_reading_migrates_per_signal():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_legacy_plane_reading_doc())
    out = p._read_form(strict=False)
    # no stage-level shared defaults: every plane's reading block is gone
    for pl in out["chain"]["planes"].values():
        assert "reported" not in pl and "limiting" not in pl
    sig = out["signals"]["fm_chirp"]
    # the operating-plane limiting migrated to the signal, minus the removed ceiling
    assert sig["limiting"]["kind"] == "law" and sig["limiting"]["law"]["id"] == "fbw"
    assert "max_dbm" not in sig["limiting"]
    # Reported is removed entirely
    assert "reported" not in sig
    # the per-signal measurement survives untouched
    assert sig["measurement"] == {"quantity": "Peak density", "unit": "dBm/MHz"}


def test_per_signal_limiting_law_round_trips():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_base_doc({"measurement": {"unit": "dBm/MHz", "quantity": "density"},
                          "limiting": {"kind": "law", "law": FBW}}))
    sig = p._read_form(strict=False)["signals"]["fm_chirp"]
    assert sig["limiting"]["kind"] == "law"
    assert sig["limiting"]["law"]["id"] == "fbw"


def test_per_signal_own_limiting_round_trips():
    own = {"kind": "own", "curve": {"points": [
        {"gain_db": 40, "power_dbm": -20}, {"gain_db": 74, "power_dbm": 10}]}}
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_base_doc({"limiting": own}))
    sig = p._read_form(strict=False)["signals"]["fm_chirp"]
    assert sig["limiting"]["kind"] == "own"
    assert sig["limiting"]["curve"]["points"][0] == {"gain_db": 40.0, "power_dbm": -20.0}


def test_per_signal_measurement_round_trips():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_base_doc({"measurement": {"unit": "dBm/MHz", "quantity": "Peak density"}}))
    sig = p._read_form(strict=False)["signals"]["fm_chirp"]
    assert sig["measurement"] == {"quantity": "Peak density", "unit": "dBm/MHz"}


def test_per_signal_ceiling_is_dropped_on_save():
    # A legacy per-signal limiting cap is removed — the stage limits list is the ceiling now.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_base_doc({"limiting": {"kind": "law", "law": FBW, "max_dbm": 12.0}}))
    sig = p._read_form(strict=False)["signals"]["fm_chirp"]
    assert "max_dbm" not in sig["limiting"]


def test_reported_bridge_is_dropped_on_save():
    # An authored per-signal reported bridge is removed entirely on a form round-trip.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_base_doc({"reported": {"kind": "law", "law": FBW, "unit": "dBm"}}))
    sig = p._read_form(strict=False)["signals"]["fm_chirp"]
    assert "reported" not in sig


# ── limiting law gating (always resolves to dBm) ──────────────────────────────

def test_limiting_laws_are_dbm_returning_and_input_matched(monkeypatch):
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    laws = {
        "fbw": {"id": "fbw", "name": "Full-bw", "in": "density", "out": "abs"},
        "d2d": {"id": "d2d", "name": "Density restate", "in": "density", "out": "density"},
        "peak": {"id": "peak", "name": "Peak→total", "in": "abs", "out": "abs"},
    }
    # the picker is scoped to the SIGNAL's own declared laws
    monkeypatch.setattr(p, "_declared_laws_for_signal", lambda sid: laws)
    # a density measurement offers only the density→dBm law (not the density→density one)
    assert set(p._limiting_laws_for("fm_chirp", "dBm/MHz")) == {"fbw"}
    # a dBm (absolute) measurement offers only the abs→dBm law
    assert set(p._limiting_laws_for("fm_chirp", "dBm")) == {"peak"}


def test_limiting_laws_are_scoped_to_the_signal(monkeypatch):
    # A law belonging to another signal's script must not appear in this signal's picker.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    by_signal = {
        "gps": {"fsp": {"id": "fsp", "name": "Full signal power", "in": "density", "out": "abs"}},
        "chirp": {"fbw": {"id": "fbw", "name": "Full-bandwidth power",
                          "in": "density", "out": "abs"}},
    }
    monkeypatch.setattr(p, "_declared_laws_for_signal", lambda sid: by_signal.get(sid, {}))
    assert set(p._limiting_laws_for("gps", "dBm/MHz")) == {"fsp"}      # not the chirp's law
    assert set(p._limiting_laws_for("chirp", "dBm/MHz")) == {"fbw"}


def test_limiting_coerces_same_to_derived_for_a_density(monkeypatch):
    # "Same as measurement" is not a dBm reading for a density — rendering coerces it to the
    # available derived law.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    monkeypatch.setattr(p, "_declared_laws_for_signal",
                        lambda sid: {"fbw": {"id": "fbw", "name": "Full-bw",
                                             "in": "density", "out": "abs"}})
    p._set_doc(_base_doc({"measurement": {"unit": "dBm/MHz", "quantity": "density"},
                          "limiting": {"kind": "same"}}))
    entry = p._f["signals"]["fm_chirp"]
    p._limiting_section("fm_chirp", entry)                 # render performs the coercion
    assert entry["reading"]["limiting"]["kind"] == "law"
    assert entry["reading"]["limiting"]["law"]["id"] == "fbw"


def test_limiting_coerces_to_own_for_a_density_without_a_dbm_law(monkeypatch):
    # A density with no dBm-returning law can only limit via a separate dBm measurement.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    monkeypatch.setattr(p, "_declared_laws_for_signal", lambda sid: {})
    p._set_doc(_base_doc({"measurement": {"unit": "dBm/MHz", "quantity": "density"},
                          "limiting": {"kind": "same"}}))
    entry = p._f["signals"]["fm_chirp"]
    p._limiting_section("fm_chirp", entry)
    assert entry["reading"]["limiting"]["kind"] == "own"


def test_limiting_stays_same_for_dbm_measurement(monkeypatch):
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    monkeypatch.setattr(p, "_declared_laws_for_signal", lambda sid: {})
    p._set_doc(_base_doc({"measurement": {"unit": "dBm", "quantity": "power"}}))
    entry = p._f["signals"]["fm_chirp"]
    p._limiting_section("fm_chirp", entry)
    # a plain dBm measurement with an empty limiting stays the (trivial) "same"
    assert entry["reading"]["limiting"].get("kind", "same") == "same"


# ── capability gating (unchanged: per-signal readings need calibration-power-bridges) ──

def test_per_signal_reading_gates_on_capability():
    doc = _base_doc({"measurement": {"unit": "dBm/MHz"},
                     "limiting": {"kind": "law", "law": FBW}})
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(doc)
    assert p._doc_uses_power_bridges(p._doc) is True
    assert p._blocks_on_power_bridges() is True
    p2 = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-power-bridges"])))
    p2._set_doc(doc)
    assert p2._blocks_on_power_bridges() is False


def test_plain_document_not_gated():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_base_doc())
    assert p._doc_uses_power_bridges(p._doc) is False
    assert p._blocks_on_power_bridges() is False


# ── per-signal measurement quantity/unit gating (Phase 2 capability) ────────────

def test_measurement_quantity_gates_on_capability():
    doc = _base_doc({"measurement": {"unit": "dBm/MHz", "quantity": "psd"},
                     "limiting": {"kind": "law", "law": FBW}})
    # an agent with power-bridges but NOT measurement-quantity → blocked
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=("calibration-power-bridges",))))
    p._set_doc(doc)
    assert p._doc_uses_measurement_quantity(p._doc) is True
    assert p._blocks_on_measurement_quantity() is True
    # a capable agent → not blocked
    p2 = CalibrationPanel("u", FakeHub(FakeClient(
        caps=["calibration-power-bridges", "calibration-measurement-quantity"])))
    p2._set_doc(doc)
    assert p2._blocks_on_measurement_quantity() is False


def test_measurement_quantity_label_alone_still_counts():
    # A quantity label with the implied dBm unit is still a measurement block → gated.
    doc = _base_doc({"measurement": {"quantity": "Full-band power"}})
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(doc)
    assert p._doc_uses_measurement_quantity(p._doc) is True


def test_no_measurement_not_gated():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=())))
    p._set_doc(_base_doc())
    assert p._doc_uses_measurement_quantity(p._doc) is False
    assert p._blocks_on_measurement_quantity() is False
