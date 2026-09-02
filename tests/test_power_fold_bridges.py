"""Client-side re-fold of the reported/limiting power-quantity BRIDGES (docs/calibration-v2
§13): PowerFold applies the reported bridge to the operator power axis and re-folds it at the
live parameter value, mirroring calkit.PowerMap so the form shows exactly what the script
produces. Tracks sdr-agent/tests/test_calkit_bridges.py."""
import pytest

from state.power_fold import PowerFold, refold_bounds, clamp_warning


FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 1e7}     # rep 10 MHz → +70 dB


def _artifact(reported=None, limiting=None):
    # measured DENSITY anchor: gain 40→-30, 74→4 dBm/MHz (slope-1: power == gain - 70)
    art = {
        "anchor_curve": [[40.0, -30.0], [74.0, 4.0]],
        "passive_hops": [],
        "freq_dependent_limits": [],
        "gain_ceiling_db": 74.0,
        "min_gain_db": 40.0,
    }
    readings = {}
    if reported is not None:
        readings["reported"] = reported
    if limiting is not None:
        readings["limiting"] = limiting
    if readings:
        art["readings"] = readings
    return art


def test_reported_law_shifts_and_refolds():
    fold = PowerFold.from_artifact(
        _artifact(reported={"kind": "law", "unit": "dBm", "law": FBW}))
    assert fold.param_dependent and fold.keyed_params() == ["bw"]
    # bounds at rep (bw defaults to rep 1e7 → +70): max = 4 + 70 = 74
    b = fold.bounds_at(None)
    assert b["max_power_dbm"] == pytest.approx(74.0)
    # live bw: 10x bandwidth reads 10 dB higher for the same gain
    hi = fold.power_for_gain(60.0, params={"bw": 1e7})
    lo = fold.power_for_gain(60.0, params={"bw": 1e6})
    assert hi - lo == pytest.approx(10.0)


def test_no_readings_is_unshifted():
    fold = PowerFold.from_artifact(_artifact())
    assert not fold.param_dependent
    assert fold._reported_shift({"bw": 1e9}) == 0.0
    assert fold.power_for_gain(60.0) == pytest.approx(-10.0)   # gain 60 → -10 dBm/MHz


def test_limiting_cap_tightens_with_parameter():
    fold = PowerFold.from_artifact(_artifact(
        reported={"kind": "same", "unit": "dBm/Hz"},
        limiting={"kind": "law", "law": FBW, "max_dbm": 50.0}))
    c_narrow = fold._ceiling(None, {"bw": 1e6})   # target density 50-60 = -10 → gain 60
    c_wide = fold._ceiling(None, {"bw": 1e7})     # target density 50-70 = -20 → gain 50
    assert c_narrow - c_wide == pytest.approx(10.0)


def test_refold_bounds_uses_params():
    art = _artifact(reported={"kind": "law", "unit": "dBm", "law": FBW})
    bounds = {"artifact": art, "max_power_dbm": 74.0, "min_power_dbm": 40.0}
    out = refold_bounds(bounds, None, params={"bw": 1e6})   # 60 dB → max 4 + 60 = 64
    assert out["max_power_dbm"] == pytest.approx(64.0)


LAW_ENBW = {"id": "full_power", "name": "Full signal power", "in": "density", "out": "abs",
            "k": 60.0, "param": "enbw_mhz", "coeff": 10.0, "ref": 1.0, "rep": 0.988638}


def test_bridge_law_on_missing_param_folds_at_rep_not_crash():
    # A bridge law keyed on a script-INTERNAL parameter (e.g. GPS L1 C/A's full_power on
    # enbw_mhz, which has no form field): the form folds with whatever params it DOES have
    # (e.g. a chirp's {"bw": ...}). The fold must fall back to the law's representative value,
    # not raise — regression: opening the sweep Run form crashed with
    # "law 'full_power' needs parameter 'enbw_mhz'".
    for role in ("reported", "limiting"):
        art = _artifact(**{role: {"kind": "law", "law": LAW_ENBW, "max_dbm": 30.0}})
        fold = PowerFold.from_artifact(art)
        b = fold.bounds_at(None, {"bw": 1.0})          # params present, but no enbw_mhz
        assert "max_power_dbm" in b and "min_power_dbm" in b
        # identical to folding with no params at all (both use the representative value)
        assert fold._reading_delta(
            fold._limiting if role == "limiting" else fold._reported,
            {"bw": 1.0}) == pytest.approx(fold._reading_delta(
                fold._limiting if role == "limiting" else fold._reported, None))
    # and refold_bounds (the path the Run form actually takes) no longer raises
    bounds = {"artifact": _artifact(limiting={"kind": "law", "law": LAW_ENBW, "max_dbm": 30.0}),
              "max_power_dbm": 4.0, "min_power_dbm": -30.0}
    assert "max_power_dbm" in refold_bounds(bounds, 1.5e9, params={"bw": 20.0})


def test_clamp_warning_on_parameter():
    art = _artifact(reported={"kind": "law", "unit": "dBm", "law": FBW})
    # at bw=1e6 the max reported power is 64 dBm; asking 70 → clamp warning
    msg = clamp_warning(art, 1.5e9, 70.0, params={"bw": 1e6})
    assert msg and "clamp" in msg.lower()
    # in range at bw=1e7 (max 74) → no warning
    assert clamp_warning(art, 1.5e9, 70.0, params={"bw": 1e7}) is None
