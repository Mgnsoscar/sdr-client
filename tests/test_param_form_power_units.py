"""ParamForm --power unit views (script CAL_POWER_LAWS): one spectral-density measurement lets
the operator control --power in either spectral density (dBm/MHz) or full-bandwidth power (dBm)
and see the other as a live companion read-out, both tracking the live sweep bandwidth. The
value SENT as --power is always the embedded reported (base) quantity — the dropdown and
read-outs are a display/entry convenience.

Physics anchor: a constant-amplitude chirp has bandwidth-invariant TOTAL power, so from a
density measured at CAL_MEAS_BW (10 MHz):
    total = density + 10*log10(10)          (constant; the fbw law)
    density(bw) = density - 10*log10(bw/10) (tracks the sweep; the psd law)
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox, QLabel

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])

# The two declared laws, anchored to a 10 MHz measurement bandwidth.
FBW = {"id": "fbw_power", "name": "Full-bandwidth (total) power", "in": "density",
       "out": "abs", "k": 10.0, "rep": 10.0}
PSD = {"id": "psd_live", "name": "Spectral density (at live sweep bw)", "in": "density",
       "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0}


def _artifact(reported):
    return {
        "schema_version": 1, "signal_id": "fm_chirp", "operating_plane": "sdr_output",
        "quantity": "spectral density", "amplitude": 0.5,
        "min_gain_db": 60.0, "max_gain_db": 70.0,
        "min_power_dbm": -26.76, "max_power_dbm": -16.71,
        "curve": {"interp": "linear", "points": [[60, -26.76], [70, -16.71]]},
        "operating_unit": reported["unit"],
        "anchor_curve": [[60, -26.76], [70, -16.71]], "passive_hops": [],
        "readings": {"reported": reported, "limiting": {"kind": "same"},
                     "reported_delta_db": 0.0, "limiting_delta_db": 0.0},
    }


def _density_reported():
    return {"kind": "law", "unit": "dBm/MHz", "law": PSD}


def _total_reported():
    return {"kind": "law", "unit": "dBm", "law": {**FBW}}


def _bounds(reported, lo, hi, unit):
    art = _artifact(reported)
    return {"min_power_dbm": lo, "max_power_dbm": hi, "quantity": "spectral density",
            "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": art}


def _specs(power_unit):
    return [
        {"dest": "freq", "flags": ["--freq"], "type": "float", "step": 0.01, "unit": "MHz",
         "default": 1575.42, "is_freq": True},
        {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
         "unit": power_unit, "snap_role": "power"},
        {"dest": "bw", "flags": ["--bw"], "type": "float", "step": 0.1, "unit": "MHz",
         "default": 20.0, "min": 0.001, "max": 55.0},
    ]


def _form(reported, lo, hi, unit):
    f = ParamForm()
    f.set_params(_specs(unit), cal_bounds=_bounds(reported, lo, hi, unit),
                 absolute_allowed=True, default_power_mode="absolute",
                 cal_freq_param="freq", power_laws=[FBW, PSD])
    return f


def _companions(f):
    return [w.text() for w in f.findChildren(QLabel) if w.text().startswith("=")]


def _view_combo(f):
    for w in f.findChildren(QComboBox):
        if any(w.itemData(i) == "fbw_power" for i in range(w.count())):
            return w
    return None


def _select(combo, view_id):
    combo.setCurrentIndex(next(i for i in range(combo.count())
                               if combo.itemData(i) == view_id))


# ── companion read-out ────────────────────────────────────────────────────────

def test_density_base_shows_total_companion_tracking_bw():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-16.71"])
    _app.processEvents()
    # density -16.71 dBm/MHz at bw 10 -> total -6.71 dBm
    assert any("−6.71 dBm" in t and "Full-bandwidth" in t for t in _companions(f))
    f._widgets["bw"][0].setValue(20.0)
    f._widgets["bw"][0].editingFinished.emit()
    _app.processEvents()
    # at bw 20 the same commanded density is +3.01 dB more total
    assert any("−3.7 dBm" in t for t in _companions(f))


def test_total_base_shows_density_companion_tracking_bw():
    f = _form(_total_reported(), -16.76, -6.71, "dBm")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-6.71"])
    _app.processEvents()
    assert any("−16.71 dBm/MHz" in t for t in _companions(f))
    f._widgets["bw"][0].setValue(20.0)
    f._widgets["bw"][0].editingFinished.emit()
    _app.processEvents()
    # total held (bandwidth-invariant); density drops 10*log10(20/10)=3.01 dB
    assert any("−19.72 dBm/MHz" in t for t in _companions(f))


# ── control-unit dropdown ─────────────────────────────────────────────────────

def test_dropdown_lists_both_views():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    combo = _view_combo(f)
    assert combo is not None
    ids = {combo.itemData(i) for i in range(combo.count())}
    assert ids == {None, "fbw_power"}       # base (density) + total companion


def test_swapping_control_unit_keeps_sent_value_in_base_quantity():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-16.71"])
    _app.processEvents()
    assert "-16.71" in f.build_args()       # density base sent

    _select(_view_combo(f), "fbw_power")    # control in total power
    _app.processEvents()
    sp = f._widgets["power"][1]
    assert sp["unit"] == "dBm"
    assert sp["max"] == pytest.approx(-6.71, abs=1e-3)   # density max -16.71 + 10 dB
    # the value SENT is still the base density, unchanged by the display swap
    assert "-16.71" in f.build_args()

    _select(_view_combo(f), None)           # back to density
    _app.processEvents()
    assert f._widgets["power"][1]["unit"] == "dBm/MHz"
    assert "-16.71" in f.build_args()


def test_editing_in_total_unit_sends_converted_base_value():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-16.71"])
    _app.processEvents()
    _select(_view_combo(f), "fbw_power")
    _app.processEvents()
    # type a total-power value; at bw 10 the base density is total - 10 dB
    f._widgets["power"][0].setText("-6.71")
    _app.processEvents()
    assert "-16.71" in f.build_args()


def test_no_laws_means_no_dropdown_or_companion():
    f = ParamForm()
    f.set_params(_specs("dBm/MHz"), cal_bounds=_bounds(_density_reported(), -26.76, -16.71, "dBm/MHz"),
                 absolute_allowed=True, default_power_mode="absolute", cal_freq_param="freq",
                 power_laws=[])
    _app.processEvents()
    assert _view_combo(f) is None
    assert _companions(f) == []
