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


def _comb_specs():
    """A first/spacing + range-vs-count comb schema, exercising the arithmetic-progression
    derived ops (count / span_to / term / extent)."""
    def p(name, flag, **kw):
        base = {"name": name, "dest": name, "flags": [flag], "type": "float",
                "kind": "number", "show_when": None, "formula": None, "is_freq": False}
        base.update(kw)
        return base
    return [
        {"name": "comb_mode", "dest": "comb_mode", "flags": ["--comb-mode"], "kind": "choice",
         "type": "str", "choices": ["range", "count"],
         "choice_labels": {"range": "First → last", "count": "First + count"},
         "choice_values": {"range": "range", "count": "count"},
         "default": "range", "show_when": None, "formula": None, "is_freq": False},
        p("first", "--first", unit="MHz", default=1560.0),
        p("spacing", "--spacing", unit="MHz", default=2.0, min=0.01, max=50.0, step=0.1),
        p("last", "--last", unit="MHz", default=1590.0, min=70.0, max=6000.0, step=0.1,
          show_when={"comb_mode": "range"}),
        p("comb_count", "-Knife-count", kind="derived", unit="knives",
          formula={"count": ["first", "last", "spacing"]}, show_when={"comb_mode": "range"}),
        p("count", "--count", type="int", kind="integer", default=16, min=1, max=512, step=1,
          show_when={"comb_mode": "count"}),
        p("comb_last", "-Last-knife", kind="derived", unit="MHz",
          formula={"term": ["first", "count", "spacing"]}, show_when={"comb_mode": "count"}),
        p("comb_span", "-Span", kind="derived", unit="MHz", min=0.0, max=50.0,
          formula={"extent": ["count", "spacing"]}, show_when={"comb_mode": "count"}),
    ]


def test_comb_range_and_count_modes_derive_the_other():
    f = ParamForm()
    f.set_params(_comb_specs())
    # range: first 1560, spacing 2, last 1590 → 16 knives.
    assert "last" in f._widgets and "count" not in f._widgets
    assert f._derived["comb_count"]["value_lbl"].text().startswith("16")

    w = f._widgets["comb_mode"][0]
    w.setCurrentIndex(w.findData("count"))
    # count: first 1560, spacing 2, count 16 → last 1590, span 30.
    assert "count" in f._widgets and "last" not in f._widgets
    assert f._derived["comb_last"]["value_lbl"].text().startswith("1590")
    assert f._derived["comb_span"]["value_lbl"].text().startswith("30")
    assert f.validate() is None

    # too many knives for the band → span over the 50 MHz max blocks submission.
    f._widgets["count"][0].setValue(100)          # span (100-1)*2 = 198 MHz
    err = f.validate()
    assert err is not None and "50" in err


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
