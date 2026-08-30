"""Conditional visibility (show_when) + derived (computed) fields — the paramkit
mechanism a script uses to offer, e.g., a sweep band entered either as centre+width or
as start/stop edges. Covers: only the selected mode's fields render, a derived readout
tracks its sources and blocks an out-of-range value, and the calibration fold frequency
falls back to a derived is_freq midpoint when the centre field is hidden."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


def _band_specs():
    """A minimal center_bw / start_stop schema, in the shape the agent extractor emits."""
    def p(name, flag, **kw):
        base = {"name": name, "dest": name, "flags": [flag], "type": "float",
                "kind": "number", "show_when": None, "formula": None, "is_freq": False}
        base.update(kw)
        return base
    return [
        {"name": "band_mode", "dest": "band_mode", "flags": ["--band-mode"], "kind": "choice",
         "type": "str", "choices": ["center_bw", "start_stop"],
         "choice_labels": {"center_bw": "Centre + width", "start_stop": "Start / stop"},
         "choice_values": {"center_bw": "center_bw", "start_stop": "start_stop"},
         "default": "center_bw", "show_when": None, "formula": None, "is_freq": False},
        p("freq", "--freq", unit="MHz", default=1575.42, show_when={"band_mode": "center_bw"}),
        p("bw", "--bw", unit="MHz", default=20.0, min=0.001, max=55.0, step=0.1,
          show_when={"band_mode": "center_bw"}),
        p("start", "--start", unit="MHz", default=1570.42, min=70.0, max=6000.0, step=0.01,
          show_when={"band_mode": "start_stop"}),
        p("stop", "--stop", unit="MHz", default=1580.42, min=70.0, max=6000.0, step=0.01,
          show_when={"band_mode": "start_stop"}),
        p("band_center", "-Carrier", kind="derived", unit="MHz", is_freq=True,
          formula={"center": ["start", "stop"]}, show_when={"band_mode": "start_stop"}),
        p("band_span", "-Sweep-width", kind="derived", unit="MHz", min=0.001, max=55.0,
          formula={"span": ["start", "stop"]}, show_when={"band_mode": "start_stop"}),
    ]


def _switch(form, mode):
    w = form._widgets["band_mode"][0]
    w.setCurrentIndex(w.findData(mode))


def test_only_the_selected_modes_fields_render():
    f = ParamForm()
    f.set_params(_band_specs())
    # center_bw (default): freq + bw visible; start/stop + derived absent.
    assert "freq" in f._widgets and "bw" in f._widgets
    assert "start" not in f._widgets and "stop" not in f._widgets
    assert not f._derived
    args = f.build_args()
    assert "--freq" in args and "--start" not in args

    _switch(f, "start_stop")
    assert "start" in f._widgets and "stop" in f._widgets
    assert "freq" not in f._widgets and "bw" not in f._widgets
    assert set(f._derived) == {"band_center", "band_span"}
    args = f.build_args()
    assert "--start" in args and "--stop" in args
    assert "--freq" not in args and "--bw" not in args


def test_derived_readout_tracks_sources_and_blocks_over_range():
    f = ParamForm()
    f.set_params(_band_specs())
    _switch(f, "start_stop")
    # 1570.42 / 1580.42 → carrier 1575.42, width 10.
    assert f._derived["band_center"]["value_lbl"].text().startswith("1575.42")
    assert f._derived["band_span"]["value_lbl"].text().startswith("10")
    assert f.validate() is None

    # widen past the 55 MHz max → validate blocks with a width-specific message.
    f._widgets["start"][0].setValue(1500.0)
    f._widgets["stop"][0].setValue(1600.0)
    assert f._derived["band_span"]["value_lbl"].text().startswith("100")
    err = f.validate()
    assert err is not None and "55" in err and "width" in err.lower()

    # back within range clears it.
    f._widgets["stop"][0].setValue(1520.0)
    assert f.validate() is None


def test_fold_frequency_falls_back_to_derived_midpoint_when_centre_hidden():
    f = ParamForm()
    f.set_params(_band_specs())
    # center_bw: the freq field is the fold source.
    assert f._freq_source_dest() == "freq" or f._cal_freq_param is None
    _switch(f, "start_stop")
    # start_stop: the centre field is hidden, so the derived is_freq midpoint takes over.
    assert f._freq_source_dest() == "band_center"
    f._widgets["start"][0].setValue(1500.0)
    f._widgets["stop"][0].setValue(1600.0)
    assert f._current_freq_hz() == pytest.approx(1550.0)
