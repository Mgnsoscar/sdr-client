"""A derived field that STANDS IN FOR a law-keyed parameter hidden by a mode (`provides`):
a chirp in start/stop mode hides --bw and shows a derived sweep span; the span must be what
the calibration power-law fold keys on, so --power tracks the real sweep (not the stale --bw).
Regression for: start/stop mode folding total power at the --bw default instead of stop−start."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])

# A spectral-density reading that tracks the live sweep bandwidth (keys on `bw`): the fold
# reads the current bw so a bridged --power moves with the sweep. dBm/MHz measured at 10 MHz.
PSD = {"id": "psd", "name": "Spectral density", "in": "density", "out": "density",
       "param": "bw", "coeff": -10.0, "ref": 10.0, "rep": 10.0}
FBW = {"id": "fbw", "name": "Full-bandwidth (total) power", "in": "density", "out": "abs",
       "k": 10.0, "rep": 10.0}


def _artifact():
    return {
        "schema_version": 1, "signal_id": "fm_chirp", "operating_plane": "sdr_output",
        "quantity": "spectral density", "amplitude": 0.5,
        "min_gain_db": 60.0, "max_gain_db": 70.0,
        "min_power_dbm": -26.76, "max_power_dbm": -16.71,
        "curve": {"interp": "linear", "points": [[60, -26.76], [70, -16.71]]},
        "operating_unit": "dBm/MHz",
        "anchor_curve": [[60, -26.76], [70, -16.71]], "passive_hops": [],
        "readings": {"reported": {"kind": "law", "unit": "dBm/MHz", "law": PSD},
                     "limiting": {"kind": "same"},
                     "reported_delta_db": 0.0, "limiting_delta_db": 0.0},
    }


def _bounds():
    return {"min_power_dbm": -26.76, "max_power_dbm": -16.71, "quantity": "spectral density",
            "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": _artifact()}


def _specs(mode="center_bw"):
    # A chirp-shaped schema: a band mode toggles between centre+width (--freq, --bw) and
    # start/stop (--start, --stop with a derived carrier + span). The span provides "bw".
    # ``mode`` sets the default so the form renders that mode directly (a live combo switch
    # re-renders in the real app; the tests render each mode to check the fold per mode).
    return [
        {"dest": "band_mode", "flags": ["--band-mode"], "type": "str",
         "choices": ["center_bw", "start_stop"], "default": mode},
        {"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz",
         "default": 1575.42, "is_freq": True, "show_when": {"band_mode": "center_bw"}},
        {"dest": "start", "flags": ["--start"], "type": "float", "unit": "MHz",
         "default": 1570.42, "show_when": {"band_mode": "start_stop"}},
        {"dest": "stop", "flags": ["--stop"], "type": "float", "unit": "MHz",
         "default": 1580.42, "show_when": {"band_mode": "start_stop"}},
        {"dest": "band_center", "flags": ["-Carrier"], "kind": "derived", "unit": "MHz",
         "formula": {"center": ["start", "stop"]}, "is_freq": True,
         "show_when": {"band_mode": "start_stop"}},
        {"dest": "band_span", "flags": ["-Sweep-width"], "kind": "derived", "unit": "MHz",
         "formula": {"span": ["start", "stop"]}, "provides": "bw",
         "show_when": {"band_mode": "start_stop"}},
        {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
         "unit": "dBm/MHz", "snap_role": "power"},
        {"dest": "bw", "flags": ["--bw"], "type": "float", "unit": "MHz",
         "default": 20.0, "min": 0.001, "max": 55.0, "show_when": {"band_mode": "center_bw"}},
    ]


def _form(mode="center_bw"):
    f = ParamForm()
    f.set_params(_specs(mode), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq", power_laws=[PSD, FBW])
    return f


def test_center_bw_mode_keys_on_the_bw_field():
    f = _form("center_bw")
    f.set_values(["--band-mode", "center_bw", "--freq", "1575.42", "--bw", "10",
                  "--power", "-20"])
    _app.processEvents()
    # --bw is the active source in centre+width mode
    assert f._live_params() == {"bw": 10.0}


def test_start_stop_mode_keys_on_the_span_not_the_hidden_bw():
    f = _form("start_stop")
    # start/stop span = 10 MHz; the hidden --bw still holds its 20 MHz default. The fold must
    # use the SPAN (10), not the stale --bw (20) — the reported bug.
    f.set_values(["--start", "1570.42", "--stop", "1580.42", "--power", "-20"])
    _app.processEvents()
    assert f._live_params() == {"bw": 10.0}


def test_start_stop_span_tracks_edits():
    f = _form("start_stop")
    f.set_values(["--start", "1570.42", "--stop", "1580.42", "--power", "-20"])
    _app.processEvents()
    assert f._live_params() == {"bw": 10.0}
    # widen the sweep to 20 MHz — the fold follows the span
    f.set_values(["--start", "1565.42", "--stop", "1585.42", "--power", "-20"])
    _app.processEvents()
    assert f._live_params() == {"bw": 20.0}


def test_provider_inactive_in_center_bw_mode():
    # In centre+width the span field is hidden, so --bw is the source (not band_span).
    f = _form("center_bw")
    f.set_values(["--freq", "1575.42", "--bw", "42", "--power", "-20"])
    _app.processEvents()
    assert f._live_params() == {"bw": 42.0}
