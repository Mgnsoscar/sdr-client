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
FBW = {"id": "fbw_power", "name": "Full-bandwidth (total) power", "unit": "dBm",
       "in": "density", "out": "abs", "k": 10.0, "rep": 10.0}
PSD = {"id": "psd_live", "name": "Spectral density", "unit": "dBm/MHz", "in": "density",
       "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0}
PSD_HZ = {"id": "psd_hz", "name": "Spectral density", "unit": "dBm/Hz", "in": "density",
          "out": "density", "param": "bw", "coeff": -10.0, "ref": 10.0, "k": -60.0, "rep": 10.0}


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
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-22"])   # in range at both bw
    _app.processEvents()
    # density -22 dBm/MHz at bw 10 -> total -22 + 10*log10(10) = -12 dBm
    assert any("−12 dBm" in t and "Full-bandwidth" in t for t in _companions(f))
    f._widgets["bw"][0].setValue(20.0)
    f._widgets["bw"][0].editingFinished.emit()
    _app.processEvents()
    # density held at -22 (selected unit stays put); total companion = -22 + 10*log10(20) = -8.99
    assert any("−8.99 dBm" in t for t in _companions(f))


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


def test_companion_read_outs_track_bw_live_without_a_commit():
    # A drag/keystroke on --bw updates the companion read-outs continuously — no editingFinished
    # commit and no re-render — the same live feedback the --power value already gets.
    f = _form(_total_reported(), -16.76, -6.71, "dBm")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-6.71"])
    _app.processEvents()
    assert any("−16.71 dBm/MHz" in t for t in _companions(f))
    f._widgets["bw"][0].setValue(20.0)          # ← what a rail drag / keystroke does; no commit
    assert any("−19.72 dBm/MHz" in t for t in _companions(f))
    f._widgets["bw"][0].setValue(40.0)
    assert any("−22.73 dBm/MHz" in t for t in _companions(f))


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


# ── hold the SELECTED unit's value across a bandwidth change ───────────────────

def _power_value(f):
    w = f._widgets["power"][0]           # re-fetch: a re-fold rebuilds the widget
    return float(w.value()) if hasattr(w, "value") else float(w.text())


def _set_power(f, val):
    w = f._widgets["power"][0]
    if hasattr(w, "value"):
        w.setValue(float(val))
    else:
        w.setText(str(val))
    _app.processEvents()


def _set_bw(f, b):
    w = f._widgets["bw"][0]
    w.setValue(float(b))
    w.editingFinished.emit()
    _app.processEvents()


def test_density_value_held_across_bandwidth_change():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-20"])
    _app.processEvents()
    _set_bw(f, 20)
    assert _power_value(f) == pytest.approx(-20.0, abs=1e-3)   # dBm/MHz stays put
    assert "-20" in f.build_args()                            # base density held too


def test_total_value_held_across_bandwidth_change():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-20"])
    _app.processEvents()
    _select(_view_combo(f), "fbw_power")     # control in total power
    _app.processEvents()
    _set_power(f, -10.0)
    _set_bw(f, 20)
    assert _power_value(f) == pytest.approx(-10.0, abs=1e-3)   # total (bw-invariant) stays put
    # base density re-maps: total -10 at bw 20 → density -10 - 10*log10(20) = -23.01
    args = f.build_args()
    dens = float(args[args.index("--power") + 1])
    assert dens == pytest.approx(-23.01, abs=1e-2)


def test_held_value_clamps_to_new_range():
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-16.71"])   # density at max
    _app.processEvents()
    _set_bw(f, 20)   # density range drops to [-29.77, -19.72]; -16.71 is now above max
    assert _power_value(f) == pytest.approx(-19.72, abs=1e-2)


def test_declared_unit_adds_a_dbm_per_hz_view():
    f = ParamForm()
    f.set_params(_specs("dBm/MHz"),
                 cal_bounds=_bounds(_density_reported(), -26.76, -16.71, "dBm/MHz"),
                 absolute_allowed=True, default_power_mode="absolute", cal_freq_param="freq",
                 power_laws=[FBW, PSD, PSD_HZ])
    f.set_values(["--freq", "1575.42", "--bw", "10", "--power", "-22"])
    _app.processEvents()
    combo = _view_combo(f)
    units = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert "psd_hz" in units and "dBm/Hz" in units["psd_hz"]
    # control in dBm/Hz: dBm/Hz = dBm/MHz − 60, so the range shifts down 60 dB
    _select(combo, "psd_hz")
    _app.processEvents()
    sp = f._widgets["power"][1]
    assert sp["unit"] == "dBm/Hz"
    assert sp["max"] == pytest.approx(-76.71, abs=1e-2)   # -16.71 − 60
    assert "-22" in f.build_args()                        # base density unchanged


def test_no_laws_means_no_dropdown_or_companion():
    f = ParamForm()
    f.set_params(_specs("dBm/MHz"), cal_bounds=_bounds(_density_reported(), -26.76, -16.71, "dBm/MHz"),
                 absolute_allowed=True, default_power_mode="absolute", cal_freq_param="freq",
                 power_laws=[])
    _app.processEvents()
    assert _view_combo(f) is None
    assert _companions(f) == []


# ── restates_measurement: drop the raw measured density from the control views ──

def _retired_bounds(quantity="Passband spectral density", unit="dBm/MHz"):
    # Reported retired: no reported reading, so the measured quantity IS the base --power axis.
    art = {
        "operating_unit": unit, "quantity": quantity,
        "min_gain_db": 60.0, "max_gain_db": 70.0,
        "min_power_dbm": -26.76, "max_power_dbm": -16.71,
        "anchor_curve": [[60, -26.76], [70, -16.71]], "passive_hops": [],
        "readings": {"limiting": {"kind": "same"}},
    }
    return {"min_power_dbm": -26.76, "max_power_dbm": -16.71, "quantity": quantity,
            "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": art}


def _view_ids(power_laws, bounds):
    f = ParamForm()
    f.set_params(_specs("dBm/MHz"), cal_bounds=bounds, absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq", power_laws=power_laws)
    return [v["id"] for v in f._power_views()]


_FLAG_PSD = {**PSD, "restates_measurement": True}
_FLAG_PSD_HZ = {**PSD_HZ, "restates_measurement": True}


def test_restates_measurement_drops_the_raw_measured_view():
    # The flagged psd laws re-express the measured density → the raw measured quantity (id None)
    # is dropped; only the live restatements + the distinct total-power reading remain.
    ids = _view_ids([_FLAG_PSD, FBW, _FLAG_PSD_HZ], _retired_bounds())
    assert None not in ids
    assert set(ids) == {"psd_live", "fbw_power", "psd_hz"}


def test_unflagged_laws_keep_the_measured_view():
    # Without the flag, today's behaviour: the measured quantity stays as a control view.
    ids = _view_ids([PSD, FBW, PSD_HZ], _retired_bounds())
    assert None in ids                                   # the raw measured quantity is offered


def test_same_unit_distinct_reading_is_not_dropped():
    # A DIFFERENT reading that merely shares the measured unit (main-lobe vs total-in-band power,
    # both dBm) must NOT drop the measured view — the drop is explicit (the law isn't flagged).
    total = {"id": "total_in_band", "name": "Total in-band power", "unit": "dBm",
             "in": "abs", "out": "abs", "k": 0.4}
    ids = _view_ids([total], _retired_bounds(quantity="Main-lobe power", unit="dBm"))
    assert None in ids and "total_in_band" in ids        # measured (main-lobe) view kept


# ── the "quantity [unit]" label beside POWER (review item #5) ───────────────────

def test_power_chip_is_quantity_bracket_unit_with_real_spaces():
    # The label beside POWER reads "quantity [unit]" with the quantity's real spaces, not the
    # old "dBm · dotted · text" form, and it tracks the selected control-in view.
    f = _form(_density_reported(), -26.76, -16.71, "dBm/MHz")
    _app.processEvents()
    labels = [w.text() for w in f.findChildren(QLabel)]
    assert "spectral density [dBm/MHz]" in labels
    assert not any("·" in t and "spectral" in t for t in labels)   # quantity not dotted
    combo = _view_combo(f); _select(combo, "fbw_power"); _app.processEvents()
    labels = [w.text() for w in f.findChildren(QLabel)]
    assert "Full-bandwidth (total) power [dBm]" in labels


def test_power_chip_when_operating_unit_absent_falls_back_to_dbm():
    # A bridge-less calibration (no operating_unit) keeps the quantity and shows [dBm].
    art = _artifact(_density_reported()); art.pop("operating_unit"); art["quantity"] = "Total in-band power"
    bounds = {"min_power_dbm": -136.61, "max_power_dbm": -49.18, "quantity": "Total in-band power",
              "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": art}
    f = ParamForm()
    f.set_params(_specs("dBm/MHz"), cal_bounds=bounds, absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq", power_laws=[FBW, PSD])
    _app.processEvents()
    labels = [w.text() for w in f.findChildren(QLabel)]
    assert "Total in-band power [dBm]" in labels
