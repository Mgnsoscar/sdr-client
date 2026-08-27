"""Absolute power authoring in the Library (calibration v2 follow-up).

A task/sequence authored in the Library has no target unit, but absolute power (dBm) is
the portable, plan-faithful quantity — so it's offered there (free-form, default), with a
soft achievable-range hint aggregated from units seen before. Runs/tunes against a
specific unit are unaffected (they stay bounded, or relative-only when uncalibrated)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from state.calibration_cache import CalibrationCache
from PyQt6.QtWidgets import QLabel

from ui.param_form import (
    ParamForm, _compute_power_modes, apply_power_hint, calibration_caution,
    find_power_index,
)

_app = QApplication.instance() or QApplication([])

_POWER = {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.5}
_GAIN = {"dest": "gain", "flags": ["--gain"], "type": "float", "step": 1.0}
_OTHER = {"dest": "dwell", "flags": ["--dwell"], "type": "int"}


# ── which modes are offered ─────────────────────────────────────────────────────

def test_library_offers_absolute_first():
    # No unit targeted (Library): absolute is offered free-form AND is the default.
    modes = _compute_power_modes([_POWER, _GAIN, _OTHER], cal_bounds=None,
                                 absolute_allowed=False)
    assert modes == ["absolute", "relative"]


def test_targeted_calibrated_unit_bounds_absolute():
    modes = _compute_power_modes([_POWER, _GAIN], cal_bounds={"min_power_dbm": -30,
                                                              "max_power_dbm": 20},
                                 absolute_allowed=True)
    assert modes == ["absolute", "relative"]


def test_targeted_uncalibrated_unit_is_relative_only():
    # A specific unit that isn't calibrated for the signal → nothing to convert against.
    modes = _compute_power_modes([_POWER, _GAIN], cal_bounds=None, absolute_allowed=True)
    assert modes == ["relative"]


def test_power_only_script_always_absolute():
    for allowed in (True, False):
        assert _compute_power_modes([_POWER], cal_bounds=None,
                                    absolute_allowed=allowed) == ["absolute"]


# ── aggregate hint from the cache ───────────────────────────────────────────────

def _cal(lo, hi, sid="sig"):
    return {"valid": True, "signals": {sid: {"min_power_dbm": lo, "max_power_dbm": hi}}}


def test_aggregate_power_bounds_union_and_intersection(tmp_path):
    c = CalibrationCache(path=tmp_path / "cache.json")
    c.put("unit_a", _cal(-30, 20))
    c.put("unit_b", _cal(-24, 28))
    agg = c.aggregate_power_bounds("sig")
    assert agg["n_units"] == 2
    assert (agg["any_min"], agg["any_max"]) == (-30, 28)      # union: at least one
    assert (agg["all_min"], agg["all_max"]) == (-24, 20)      # intersection: all


def test_aggregate_none_when_signal_unknown(tmp_path):
    c = CalibrationCache(path=tmp_path / "cache.json")
    c.put("unit_a", _cal(-30, 20, sid="other"))
    assert c.aggregate_power_bounds("sig") is None


def test_apply_power_hint_does_not_bound_the_field():
    agg = {"n_units": 2, "any_min": -30, "any_max": 28, "all_min": -24, "all_max": 20}
    out = apply_power_hint(dict(_POWER), agg)
    assert "_hint" in out and "min" not in out and "max" not in out    # free-form, hinted
    assert "20" in out["_hint"] and "dBm" in out["help"]


def test_apply_power_hint_flags_non_overlapping_ranges():
    agg = {"n_units": 2, "any_min": -30, "any_max": 28, "all_min": 10, "all_max": 5}
    out = apply_power_hint(dict(_POWER), agg)
    assert "don't overlap" in out["_hint"]


# ── the form actually defaults to absolute + shows the hint in the Library ──────

def test_form_library_defaults_absolute_with_hint():
    form = ParamForm()
    agg = {"n_units": 1, "any_min": -30, "any_max": 20, "all_min": -30, "all_max": 20}
    form.set_params([_POWER, _GAIN], absolute_allowed=False, hint_bounds=agg)
    assert form.power_mode() == "absolute"
    # the absolute field is present (power), the relative one dropped
    args = form.build_args()
    # default power spec has no default value → no arg emitted, but the widget exists
    assert "gain" not in form._widgets and "power" in form._widgets


def test_form_run_uncalibrated_is_relative():
    # absolute_allowed=True + no cal_bounds (uncalibrated run) → relative only, as before.
    form = ParamForm()
    form.set_params([_POWER, _GAIN], absolute_allowed=True, cal_bounds=None)
    assert form.power_mode() == "relative"


# ── no-safeguard caution ────────────────────────────────────────────────────────

def test_caution_text_cases():
    # calibratable script, no signal assigned → raw (actionable)
    assert "no calibration signal" in calibration_caution(False, targeted=True, calibrated=True)
    assert "no calibration signal" in calibration_caution(False, targeted=False, calibrated=False)
    # signal, targeted unit that isn't calibrated → raw
    assert "isn't calibrated" in calibration_caution(True, targeted=True, calibrated=False)
    # signal + calibrated unit → safe; signal + open Library authoring → safe (limited later)
    assert calibration_caution(True, targeted=True, calibrated=True) is None
    assert calibration_caution(True, targeted=False, calibrated=False) is None


def test_caution_none_for_non_calibratable_script():
    # A script that declares no calibration signal takes raw power/gain BY DESIGN — there
    # is no missing safeguard, so no caution (this was the noisy false-positive).
    assert calibration_caution(False, targeted=False, calibrated=False,
                               script_calibratable=False) is None
    assert calibration_caution(False, targeted=True, calibrated=False,
                               script_calibratable=False) is None
    assert calibration_caution(True, targeted=True, calibrated=False,
                               script_calibratable=False) is None


def _warning_labels(form):
    # The form lays fields out as nested frames now, so walk the layout tree and
    # collect every warning label (⚠) wherever it sits.
    out = []

    def walk(layout):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            w = item.widget()
            if isinstance(w, QLabel) and w.text().startswith("⚠"):
                out.append(w.text())
            if item.layout() is not None:
                walk(item.layout())
            elif w is not None and w.layout() is not None:
                walk(w.layout())

    walk(form._body)
    return out


def test_form_shows_caution_when_raw():
    form = ParamForm()
    form.set_params([_POWER, _GAIN], absolute_allowed=True, cal_bounds=None,
                    caution="This unit isn't calibrated — power/gain go out raw.")
    assert any("raw" in t for t in _warning_labels(form))


def test_no_caution_without_a_power_or_gain_field():
    # A task with no power/gain param has nothing to be careful about → no banner.
    form = ParamForm()
    form.set_params([_OTHER], caution="ignored — no power field here")
    assert _warning_labels(form) == []
