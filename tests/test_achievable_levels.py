"""The universal achievable-level Power slider (docs/calibration-v2.md, active components).

When a task's --power field is backed by a resolved artifact, the numeric widget steps
through the chain's *true* achievable delivered-power levels — non-uniform across the range
(attenuator-only at the bottom, SDR-only at the top) — and a typed value snaps to the
nearest achievable level. This holds universally: a plain passive/SDR-only chain snaps to
the SDR's real gain grid too, not a decoupled fixed step."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import (
    ParamForm, _AchievableSpin, apply_power_bounds, find_power_index,
)

_app = QApplication.instance() or QApplication([])


# SDR: 1 dB gain ⇒ 1 dB power over 0..40 dB gain (−40..0 dBm), then a 0..95 dB / 0.25 dB
# step attenuator ⇒ effective −135..0 dBm.
def _active_art():
    return {
        "curve": [[0.0, -40.0], [40.0, 0.0]],
        "min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0,
        "active_components": [{
            "plane": "atten_out", "task": "atten_set", "param": "attenuation",
            "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
            "step_db": 0.25, "engage_pct": 0.0}],
    }


def _passive_art():
    art = _active_art()
    del art["active_components"]                     # a plain SDR-only chain
    return art


def _bounds(art, lo, hi):
    return {"min_power_dbm": lo, "max_power_dbm": hi, "quantity": "power",
            "operating_plane": "atten_out", "artifact": art}


def _specs():
    return [
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 40.0, "help": "rel"},
    ]


def _power_widget(art, lo, hi):
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(art, lo, hi), absolute_allowed=True,
                 default_power_mode="absolute")
    return f, f._widgets["power"][0], f._widgets["power"][1]


# ── apply_power_bounds marks the field + sets its display resolution ──────────────

def test_bounds_mark_the_field_and_set_finest_step():
    out = apply_power_bounds(_specs(), _bounds(_active_art(), -135.0, 0.0))
    p = out[find_power_index(out)]
    assert p["snap_role"] == "power"
    assert p["step"] == pytest.approx(0.25)          # the finest achievable increment
    assert (p["min"], p["max"]) == (-135.0, 0.0)


def test_passive_bounds_step_the_sdr_gain_grid():
    out = apply_power_bounds(_specs(), _bounds(_passive_art(), -40.0, 0.0))
    p = out[find_power_index(out)]
    assert p["snap_role"] == "power"                 # universal — SDR-only chains snap too
    assert p["step"] == pytest.approx(1.0)           # the SDR's own 1 dB gain grid


# ── the widget steps through / snaps to achievable levels ─────────────────────────

def test_power_widget_is_resolver_aware():
    _f, w, spec = _power_widget(_active_art(), -135.0, 0.0)
    assert isinstance(w, _AchievableSpin)
    assert w.decimals() == 2                          # 0.25 step → 2 decimals
    assert (spec["min"], spec["max"]) == (-135.0, 0.0)


def test_arrows_step_through_achievable_levels():
    _f, w, _s = _power_widget(_active_art(), -135.0, 0.0)
    # At the top the SDR sits at max gain (0 dBm) and the 0.25 dB attenuator trims the step.
    w.setValue(0.0)
    w.stepBy(-1)
    assert w.value() == pytest.approx(-0.25)
    # In the attenuator-only region the step is the 0.25 dB attenuator grid.
    w.setValue(-55.0)
    w.stepBy(-1)
    assert w.value() == pytest.approx(-55.25)
    w.setValue(-55.0)
    w.stepBy(1)
    assert w.value() == pytest.approx(-54.75)


def test_typed_value_snaps_to_nearest_achievable():
    _f, w, _s = _power_widget(_active_art(), -135.0, 0.0)
    w.setValue(-19.1)
    w._snap_typed()
    assert w.value() == pytest.approx(-19.0)          # nearest achievable
    w.setValue(-55.3)
    w._snap_typed()
    assert w.value() == pytest.approx(-55.25)


def test_passive_chain_snaps_to_the_gain_grid():
    _f, w, _s = _power_widget(_passive_art(), -40.0, 0.0)
    assert isinstance(w, _AchievableSpin)
    w.setValue(0.0)
    w.stepBy(-1)
    assert w.value() == pytest.approx(-1.0)           # the SDR's 1 dB grid, not a fixed 0.5
    w.setValue(-19.4)
    w._snap_typed()
    assert w.value() == pytest.approx(-19.0)


def test_default_lands_on_an_achievable_level():
    # The −20 dBm default is achievable (SDR gain 20) and opens exactly there.
    _f, w, _s = _power_widget(_active_art(), -135.0, 0.0)
    assert w.value() == pytest.approx(-20.0)
