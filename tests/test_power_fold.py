"""Client-side re-fold of a resolved calibration artifact at a chosen transmit
frequency — the mirror of the agent's calkit PowerMap. Expectations track the agent's
tests/test_calibration_v2.py fixture so the two stay in step."""
import pytest

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


def test_per_limit_anchor_curve_inverts_the_limit_against_its_own_curve():
    # A reported operating plane: the operating point reads the main-lobe curve, but the
    # amp-output limit gauges on the full-band curve, published per-limit as anchor_curve.
    art = {
        "anchor_curve": [[40.0, -8.0], [74.0, 21.0]],          # reported (main-lobe) curve
        "passive_hops": [{"plane": "amp_out",
                          "delta_db_by_freq": [[1.0e9, 10.0], [2.0e9, 6.0]]}],
        "freq_dependent_limits": [
            {"plane": "amp_out", "max_dbm": 5.0,
             "delta_db_by_freq": [[1.0e9, 10.0], [2.0e9, 6.0]],
             "anchor_curve": [[40.0, -6.0], [74.0, 24.0]]}],   # limiting (full-band) curve
        "gain_ceiling_db": None, "min_gain_db": 40.0, "center_freq_hz": 1.5e9,
    }
    f = PowerFold.from_artifact(art)
    assert f.freq_dependent
    # 5 dBm full-band limit inverted through the full-band curve [[40,-6],[74,24]]:
    # @1 GHz amp +10 → target -5 → gain 41.133; @2 GHz amp +6 → target -1 → gain 45.667.
    assert f.max_gain_db(1.0e9) == pytest.approx(40 + 34 / 30, abs=1e-6)
    assert f.max_gain_db(2.0e9) == pytest.approx(40 + 34 * 5 / 30, abs=1e-6)
    assert f.max_gain_db(2.0e9) > f.max_gain_db(1.0e9)     # per-limit anchor honoured


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


# ── active components: the fold mirrors the agent's achievable-level resolver ──────

# SDR: 1 dB gain ⇒ 1 dB power over 0..40 dB (−40..0 dBm), then a 0..95 dB / 0.25 dB step
# attenuator (an active component) ⇒ effective −135..0 dBm. Matches the agent's
# tests/test_calibration_active.py so the client fold and the resolver stay in step.
def _active_artifact(**over):
    ac = {"plane": "atten_out", "task": "atten_set", "param": "attenuation",
          "sense": "attenuation", "min_db": 0.0, "max_db": 95.0, "step_db": 0.25,
          "engage_pct": 0.0}
    ac.update(over)
    return {"curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0, "max_gain_db": 40.0,
            "gain_step_db": 1.0, "active_components": [ac]}


def test_active_component_extends_the_range():
    fold = PowerFold.from_artifact(_active_artifact())
    assert fold.has_active
    b = fold.bounds_at(None)
    assert b["min_power_dbm"] == pytest.approx(-135.0)
    assert b["max_power_dbm"] == pytest.approx(0.0)


def test_active_snap_and_quantize_match_the_resolver():
    fold = PowerFold.from_artifact(_active_artifact())
    assert fold.snap_power(-19.1) == pytest.approx(-19.0)
    assert fold.snap_power(-55.3) == pytest.approx(-55.25)
    assert fold.quantize_down(0.0) == pytest.approx(-0.25)
    assert fold.quantize_up(-55.0) == pytest.approx(-54.75)
    assert fold.finest_step() == pytest.approx(0.25)


def test_active_realize_commands_the_attenuator_sdr_first():
    fold = PowerFold.from_artifact(_active_artifact())
    # SDR carries the signal down to the floor; the attenuator stays at rest above it.
    r = fold.realize(-20.0)
    assert r["sdr_gain_db"] == pytest.approx(20.0)
    assert r["settings"][0]["task"] == "atten_set" and r["settings"][0]["param"] == "attenuation"
    assert r["settings"][0]["value"] == pytest.approx(0.0)
    # Below the floor the SDR pins at min gain and the attenuator fills the rest.
    r = fold.realize(-100.0)
    assert r["sdr_gain_db"] == pytest.approx(0.0)
    assert r["settings"][0]["value"] == pytest.approx(60.0)          # 60 dB attenuation


def test_engage_threshold_keeps_the_sdr_higher():
    fold = PowerFold.from_artifact(_active_artifact(engage_pct=50.0))
    assert fold.bounds_at(None)["min_power_dbm"] == pytest.approx(-115.0)   # −20 − 95
    r = fold.realize(-60.0)
    assert r["sdr_gain_db"] == pytest.approx(20.0)
    assert r["settings"][0]["value"] == pytest.approx(40.0)


def test_no_active_component_keeps_the_plain_gain_grid():
    art = {"curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0, "max_gain_db": 40.0,
           "gain_step_db": 1.0}
    fold = PowerFold.from_artifact(art)
    assert not fold.has_active
    assert fold.bounds_at(None)["min_power_dbm"] == pytest.approx(-40.0)
    assert fold.quantize_down(0.0) == pytest.approx(-1.0)            # the SDR's 1 dB grid
    assert fold.finest_step() == pytest.approx(1.0)


def test_no_active_nonlinear_curve_snaps_to_the_real_gain_grid():
    # NO active components: a nonlinear curve with fractional powers. The universal slider must
    # snap/quantize to the real (non-uniform) SDR gain grid, with the minimum-gain level
    # present — the same resolver fixes apply here as with an attenuator.
    art = {"curve": [[0.0, -63.7], [20.0, -41.7], [40.0, -24.7], [80.0, -6.7], [89.75, -3.7]],
           "min_gain_db": 0.0, "max_gain_db": 89.75, "gain_step_db": 0.25}
    fold = PowerFold.from_artifact(art)
    assert not fold.has_active
    b = fold.bounds_at(None)
    assert b["min_power_dbm"] == pytest.approx(-63.7)     # min-gain level present (not dropped)
    assert b["max_power_dbm"] == pytest.approx(-3.7)
    s = fold.snap_power(-30.3)
    assert fold.snap_power(s) == pytest.approx(s)         # snapped value is itself achievable
    assert fold.quantize_up(-63.7) > -63.7               # steps up the real grid from the floor
    assert fold.quantize_down(-3.7) < -3.7               # and down from the ceiling


def test_non_commensurate_steps_snap_and_quantize_to_true_levels():
    # SDR 1 dB gain grid (1 dB power) + a 0.3 dB attenuator — NOT multiples, so the true
    # achievable grid is a 0.1 dB vernier. snap/quantize must land on real levels, not skip
    # to the next attenuator step.
    art = {"curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0, "max_gain_db": 40.0,
           "gain_step_db": 1.0,
           "active_components": [{"plane": "atten_out", "task": "atten_set",
                                  "param": "attenuation", "sense": "attenuation",
                                  "min_db": 0.0, "max_db": 30.0, "step_db": 0.3,
                                  "engage_pct": 0.0}]}
    fold = PowerFold.from_artifact(art)
    assert fold.snap_power(-50.0) == pytest.approx(-50.0)            # exact level exists
    assert fold.quantize_down(-50.0) == pytest.approx(-50.1)         # true vernier neighbour
    assert fold.quantize_up(-50.2) == pytest.approx(-50.1)
    # the attenuator value commanded for a snapped level is a real 0.3 dB multiple
    val = fold.realize(-50.1)["settings"][0]["value"]
    assert abs(round(val / 0.3) - val / 0.3) < 1e-6


# ── source bias: the SDR flatness folds into the range (mirror of calkit) ─────────

def _bias_artifact():
    """SDR-only anchor + a per-unit source bias (+2 @1.0 GHz, 0 @1.5 GHz, -2 @2.0 GHz) and a
    source power limit at 4 dBm, so both delivered power and the gain ceiling move with freq."""
    return {
        "anchor_curve": [[40.0, -30.0], [60.0, -10.0], [74.0, 4.0]],
        "passive_hops": [],
        "source_bias_delta_by_freq": [[1.0e9, 2.0], [1.5e9, 0.0], [2.0e9, -2.0]],
        "freq_dependent_limits": [{"max_dbm": 4.0, "delta_db_by_freq": [[0.0, 0.0]]}],
        "gain_ceiling_db": 89.75,
        "min_gain_db": 0.0,
        "center_freq_hz": 1.5e9,
    }


def test_source_bias_shifts_power_with_frequency():
    fold = PowerFold.from_artifact(_bias_artifact())
    assert fold.freq_dependent
    assert fold.power_for_gain(60, freq=1.5e9) == pytest.approx(-10.0)   # zero at rep
    assert fold.power_for_gain(60, freq=1.0e9) == pytest.approx(-8.0)    # SDR +2 hot
    assert fold.power_for_gain(60, freq=2.0e9) == pytest.approx(-12.0)   # SDR -2 cold


def test_source_bias_tightens_the_gain_ceiling_where_hot():
    fold = PowerFold.from_artifact(_bias_artifact())
    # limit holds delivered power at 4 dBm, but the GAIN cap drops where the SDR runs hot.
    assert fold.max_gain_db(1.5e9) == pytest.approx(74.0)
    assert fold.max_gain_db(1.0e9) == pytest.approx(72.0)
    assert fold.bounds_at(1.0e9)["max_power_dbm"] == pytest.approx(4.0)
