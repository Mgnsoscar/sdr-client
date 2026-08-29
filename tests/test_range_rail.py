"""The RangeRail handle position reflects the VALUE within [min, max] linearly — a value in
the middle of the range sits in the middle of the rail, regardless of how the achievable
steps are distributed (more steps above than below, non-uniform vernier, etc.). Dragging the
rail maps the handle position back to a value the same way."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_widgets import RangeRail

_app = QApplication.instance() or QApplication([])


def _rail(lo, hi):
    r = RangeRail(); r.set_bounds(lo, hi); return r


def test_handle_position_is_value_proportional():
    r = _rail(-135.0, 0.0)                    # the SDR+attenuator extended range
    r.set_value(-67.5)                        # exact middle
    assert r.track._fraction == pytest.approx(0.5)
    r.set_value(-135.0); assert r.track._fraction == pytest.approx(0.0)
    r.set_value(0.0);    assert r.track._fraction == pytest.approx(1.0)
    r.set_value(-33.75); assert r.track._fraction == pytest.approx(0.75)   # value, not step index


def test_position_ignores_step_distribution():
    # An asymmetric range where far more achievable steps live below the midpoint than above
    # must STILL place a mid-range value at the middle — position tracks value, not step count.
    r = _rail(-184.75, 0.0)
    mid = (-184.75 + 0.0) / 2                 # -92.375
    r.set_value(mid)
    assert r.track._fraction == pytest.approx(0.5)


def test_drag_maps_position_back_to_value_linearly():
    got = []
    r = _rail(-135.0, 0.0)
    r.valueChanged.connect(got.append)
    r._on_drag(0.5)                           # thumb dragged to the middle
    assert got[-1] == pytest.approx(-67.5)
    r._on_drag(0.25)
    assert got[-1] == pytest.approx(-101.25)


def test_out_of_range_value_clamps_the_handle():
    r = _rail(-135.0, 0.0)
    r.set_value(10.0);  assert r.track._fraction == pytest.approx(1.0)   # above max → full
    r.set_value(-200.0); assert r.track._fraction == pytest.approx(0.0)  # below min → empty
