"""The client display fold (state/power_fold.PowerFold) honours a measured curve's
``extrapolate`` flag, so the --power range the form shows matches what the unit's calkit
delivers (agent 1.14.0, docs/calibration.md §7.4). Consumes the artifact dict directly."""
import pytest

from state.power_fold import PowerFold


def _art(extrapolate=None):
    # v1 (flat) artifact: power == gain - 50 over the measured gain [30, 50]; device gain [0, 60].
    art = {
        "schema_version": 2, "signal_id": "sig", "operating_plane": "sdr_output",
        "quantity": "dBm", "min_gain_db": 0.0, "max_gain_db": 60.0,
        "curve": [[30, -20.0], [40, -10.0], [50, 0.0]],
    }
    if extrapolate is not None:
        art["extrapolate"] = extrapolate
    return art


def _bounds(extrapolate=None):
    fold = PowerFold.from_artifact(_art(extrapolate))
    b = fold.bounds_at(None)
    return b["min_power_dbm"], b["max_power_dbm"]


def test_none_clamps_to_the_measured_span():
    for ex in (None, "none"):
        lo, hi = _bounds(ex)
        assert lo == pytest.approx(-20.0)
        assert hi == pytest.approx(0.0)


def test_down_extends_the_low_end():
    lo, hi = _bounds("down")
    assert lo == pytest.approx(-50.0)     # gain 0 at slope 1, 30 dB below the low point
    assert hi == pytest.approx(0.0)


def test_up_extends_the_high_end_to_the_ceiling():
    lo, hi = _bounds("up")
    assert lo == pytest.approx(-20.0)
    assert hi == pytest.approx(10.0)      # gain 60, 10 dB above the top point


def test_both_extends_both_ends():
    lo, hi = _bounds("both")
    assert lo == pytest.approx(-50.0)
    assert hi == pytest.approx(10.0)


def test_realize_delivers_the_extrapolated_power():
    fold = PowerFold.from_artifact(_art("down"))
    assert fold.realize(-40.0)["power_dbm"] == pytest.approx(-40.0)
    # Without the flag the same command clamps up to the measured floor.
    assert PowerFold.from_artifact(_art(None)).realize(-40.0)["power_dbm"] == pytest.approx(-20.0)
