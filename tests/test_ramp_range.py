"""_ramp_range_error: a ramp's From/To must lie within the ramped parameter's
declared min/max, so an out-of-range sweep is caught in the editor instead of
only failing at runtime when the tune step fires."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.ramp_editor import _ramp_range_error


def _spec(**kw):
    base = {"name": "freq", "min": 100.0, "max": 200.0, "unit": "MHz"}
    base.update(kw)
    return base


def test_within_range_is_ok():
    assert _ramp_range_error(_spec(), 120.0, 180.0) is None
    # inclusive bounds
    assert _ramp_range_error(_spec(), 100.0, 200.0) is None


def test_from_below_min_flagged():
    err = _ramp_range_error(_spec(), 50.0, 180.0)
    assert err is not None
    assert "From" in err and "50" in err
    assert "100..200" in err and "MHz" in err and "freq" in err
    assert "To" not in err            # To was in range


def test_to_above_max_flagged():
    err = _ramp_range_error(_spec(), 120.0, 250.0)
    assert err is not None and "To" in err and "250" in err
    assert "From" not in err


def test_both_endpoints_out_of_range():
    err = _ramp_range_error(_spec(), 50.0, 250.0)
    assert "From" in err and "To" in err and "and" in err


def test_only_lower_bound_declared():
    spec = {"name": "gain", "min": 0.0}      # no max
    assert _ramp_range_error(spec, -5.0, 1e9) is not None   # From below 0
    assert _ramp_range_error(spec, 5.0, 1e9) is None        # no upper bound to break


def test_no_bounds_or_missing_spec_is_ok():
    assert _ramp_range_error({"name": "x"}, -1e9, 1e9) is None
    assert _ramp_range_error(None, 1.0, 2.0) is None


def test_none_endpoints_ignored():
    # A blank From/To (not yet typed) isn't a range violation on its own.
    assert _ramp_range_error(_spec(), None, 180.0) is None
    assert _ramp_range_error(_spec(), None, None) is None


def test_message_omits_unit_when_absent():
    spec = {"name": "count", "min": 1.0, "max": 10.0}   # no unit
    err = _ramp_range_error(spec, 0.0, 5.0)
    assert err == "From 0 outside allowed range 1..10 for count"
