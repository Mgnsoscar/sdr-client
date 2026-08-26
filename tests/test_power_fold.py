"""Client-side re-fold of a resolved calibration artifact at a chosen transmit
frequency — the mirror of the agent's calkit PowerMap. Expectations track the agent's
tests/test_calibration_v2.py fixture so the two stay in step."""
from state.power_fold import PowerFold, refold_bounds, clamp_warning


# A v2 artifact: amp anchor tops at 24 dBm @ gain 74, min gain 40 @ -6 dBm; a
# frequency-dependent cable (−2→−3 dB) and antenna (+5→+7 dB) over 1→2 GHz; amp-protection
# ceiling at gain 74 (frequency-independent).
def _v2_artifact():
    return {
        "anchor_curve": [[40.0, -6.0], [74.0, 24.0]],
        "passive_hops": [
            {"plane": "cable_output", "delta_db_by_freq": [[1.0e9, -2.0], [2.0e9, -3.0]]},
            {"plane": "antenna_eirp", "delta_db_by_freq": [[1.0e9, 5.0], [2.0e9, 7.0]]},
        ],
        "freq_dependent_limits": [],
        "gain_ceiling_db": 74.0,
        "min_gain_db": 40.0,
        "center_freq_hz": 1.0e9,
    }


def test_bounds_move_with_frequency():
    fold = PowerFold.from_artifact(_v2_artifact())
    assert fold.freq_dependent
    # max = anchor(74)=24 + (cable+antenna)(f);  min = anchor(40)=-6 + (cable+antenna)(f)
    at1 = fold.bounds_at(1.0e9)
    assert at1["max_power_dbm"] == 27.0 and at1["min_power_dbm"] == -3.0
    at2 = fold.bounds_at(2.0e9)
    assert at2["max_power_dbm"] == 28.0 and at2["min_power_dbm"] == -2.0
    mid = fold.bounds_at(1.5e9)
    assert mid["max_power_dbm"] == 27.5


def test_freq_dependent_ceiling_tightens_the_gain():
    # An EIRP cap of 26 dBm at the (frequency-dependent) antenna plane: at 1 GHz the summed
    # delta is +3 → amp 23 → gain ~73.13; at 2 GHz +4 → amp 22 → gain ~72.  The tighter
    # (lower) ceiling at 2 GHz gives a lower max gain, hence a lower max power.
    art = _v2_artifact()
    art["freq_dependent_limits"] = [
        {"plane": "antenna_eirp", "max_dbm": 26.0,
         "delta_db_by_freq": [[1.0e9, 3.0], [2.0e9, 4.0]]}]
    fold = PowerFold.from_artifact(art)
    assert fold.max_gain_db(2.0e9) < fold.max_gain_db(1.0e9)


def test_v1_flat_curve_is_frequency_independent():
    art = {"curve": [[40.0, -6.0], [74.0, 24.0]], "min_gain_db": 40.0, "max_gain_db": 74.0}
    fold = PowerFold.from_artifact(art)
    assert not fold.freq_dependent
    assert fold.bounds_at(1.0e9)["max_power_dbm"] == fold.bounds_at(9.0e9)["max_power_dbm"] == 24.0


def test_refold_bounds_rewrites_the_power_range():
    bounds = {"min_power_dbm": -3.0, "max_power_dbm": 27.0, "quantity": "EIRP",
              "operating_plane": "antenna_eirp", "artifact": _v2_artifact()}
    out = refold_bounds(bounds, 2.0e9)
    assert out["max_power_dbm"] == 28.0 and out["min_power_dbm"] == -2.0
    assert out["quantity"] == "EIRP"                 # non-power fields carried through
    assert bounds["max_power_dbm"] == 27.0           # original dict untouched


def test_clamp_warning_fires_when_power_exceeds_the_ceiling_at_a_frequency():
    art = _v2_artifact()
    # max EIRP is 27 @1 GHz, 28 @2 GHz. A target of 27.5 is fine at 2 GHz…
    assert clamp_warning(art, 2.0e9, 27.5) is None
    # …but exceeds the ceiling at 1 GHz → warn it will be clamped down.
    msg = clamp_warning(art, 1.0e9, 27.5)
    assert msg and "clamped down" in msg and "27.00" in msg
    # below the floor → warn it will be raised.
    lo = clamp_warning(art, 1.0e9, -10.0)
    assert lo and "raised to it" in lo


def test_clamp_warning_silent_when_not_frequency_dependent_or_unknown():
    flat = {"curve": [[40.0, -6.0], [74.0, 24.0]], "min_gain_db": 40.0, "max_gain_db": 74.0}
    assert clamp_warning(flat, 1.0e9, 999.0) is None       # constant chain → never clamps by freq
    assert clamp_warning(_v2_artifact(), None, 27.5) is None   # unknown frequency
    assert clamp_warning(_v2_artifact(), 1.0e9, None) is None  # unknown power
    assert clamp_warning(None, 1.0e9, 27.5) is None            # no artifact


def test_refold_bounds_is_a_noop_without_frequency_or_artifact():
    bounds = {"min_power_dbm": -3.0, "max_power_dbm": 27.0, "artifact": _v2_artifact()}
    assert refold_bounds(bounds, None) is bounds            # no frequency → unchanged
    flat = {"min_power_dbm": 0.0, "max_power_dbm": 24.0,
            "artifact": {"curve": [[40.0, -6.0], [74.0, 24.0]], "min_gain_db": 40.0,
                         "max_gain_db": 74.0}}
    assert refold_bounds(flat, 2.0e9) is flat              # constant chain → unchanged
    assert refold_bounds({"min_power_dbm": 1.0}, 2.0e9) == {"min_power_dbm": 1.0}  # no artifact
