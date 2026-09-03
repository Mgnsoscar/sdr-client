"""The always-visible limit rail under a bounded numeric field: it renders for a bounded
field, tracks the value, and re-folds (via the form re-render) when the frequency changes.
The calibrated --power field surfaces the fold frequency in its DEPENDS ON row instead of a
rail note; --gain keeps the note. Its handle position is value-proportional (a mid-range value
sits at the middle, whatever the step distribution)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from ui.param_form import ParamForm, RangeRail

_app = QApplication.instance() or QApplication([])


def _artifact():
    return {
        "anchor_curve": [[40.0, -8.0], [89.75, 21.0]],
        "passive_hops": [{"plane": "amp",
                          "delta_db_by_freq": [[1.2276e9, 26.7], [1.57542e9, 28.2]]}],
        "freq_dependent_limits": [], "gain_ceiling_db": 89.75,
        "min_gain_db": 40.0, "center_freq_hz": 1.57542e9,
    }


def _bounds():
    return {"min_power_dbm": -1.8, "max_power_dbm": 28.2, "quantity": "EIRP",
            "operating_plane": "amp", "artifact": _artifact()}


def _specs():
    return [
        {"dest": "freq", "flags": ["-Frequency", "--freq"], "type": "float", "unit": "Hz",
         "min": 70e6, "max": 6e9, "default": 1.57542e9,
         "presets": [{"label": "L1", "key": "l1", "value": 1.57542e9},
                     {"label": "L2", "key": "l2", "value": 1.2276e9}]},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": 24.0},
        {"dest": "rate", "flags": ["--rate"], "type": "float", "unit": "MHz",
         "min": 1.0, "max": 61.44, "step": 0.01, "default": 40.0},
        {"dest": "mode", "flags": ["--mode"], "type": "str", "choices": ["a", "b"]},
    ]


def _rails(form):
    return form.findChildren(RangeRail)


def test_bounded_fields_get_a_rail_unbounded_do_not():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    # power (bounded to cal) and rate (bounded number) get a rail; the freq preset combo
    # and the choice field do not.
    assert len(_rails(f)) == 2


def test_bounded_field_without_a_step_still_gets_a_rail():
    # A number with min/max but no `step` renders as a text box (not a spinbox); it must
    # still get a rail, so limits show on plain bounded fields too — not only spinboxes.
    specs = _specs() + [{"dest": "dur", "flags": ["--dur"], "type": "float",
                         "unit": "s", "min": 1.0, "max": 1200.0, "default": 600.0}]
    f = ParamForm()
    f.set_params(specs, cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    assert len(_rails(f)) == 3                                    # power, rate, dur


def _dep_values(f):
    return [l.text() for l in f.findChildren(QLabel) if l.objectName() == "depValue"]


def test_power_field_surfaces_the_fold_frequency_in_depends_on():
    # The --power redesign replaces the rail's "moves with frequency" note with a DEPENDS ON
    # row that names the fold frequency (in MHz), the fold input the range moves with.
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    notes = [l.text() for rail in _rails(f) for l in rail.findChildren(QLabel)]
    assert not any("moves with frequency" in t for t in notes)     # note replaced by DEPENDS ON
    assert "1575.42" in _dep_values(f)                             # the fold frequency


def test_depends_on_frequency_refolds_when_frequency_changes():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    f.set_values(["--freq", "1.2276e9", "--power", "24"])         # switch to L2
    deps = _dep_values(f)
    assert "1227.6" in deps                                       # re-folded to L2
    assert "1575.42" not in deps


def test_no_frequency_note_without_a_freq_dependent_calibration():
    # A flat calibration (no freq-dependent artifact) still shows the rail, but no note.
    flat = {"min_power_dbm": -1.8, "max_power_dbm": 28.2, "quantity": "EIRP",
            "operating_plane": "amp",
            "artifact": {"curve": [[40.0, -8.0], [89.75, 28.2]], "min_gain_db": 40.0,
                         "max_gain_db": 89.75}}
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=flat, absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    notes = [l.text() for rail in _rails(f) for l in rail.findChildren(QLabel)]
    assert not any("moves with frequency" in t for t in notes)
    assert len(_rails(f)) == 2                                    # rails still present


# ── handle position is value-proportional (not step-index) ───────────────────────────

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
    r.set_value((-184.75 + 0.0) / 2)          # -92.375
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
    r.set_value(10.0);   assert r.track._fraction == pytest.approx(1.0)   # above max → full
    r.set_value(-200.0); assert r.track._fraction == pytest.approx(0.0)   # below min → empty


def test_power_lineedit_rail_drag_rounds_to_the_finest_step_decimals():
    # A --power field with no default renders as a QLineEdit (not a spinbox). A rail drag lands on
    # a real achievable level (full float precision); the field must show it at the chain's finest
    # DEVICE-step resolution — a 0.25 dB gain grid → 2 decimals — never raw ':g' precision.
    import re

    from PyQt6.QtWidgets import QLineEdit

    from state.power_fold import PowerFold
    # slope ≈ 1.0008 over a 0.25 dB grid ⇒ achievable levels 0.2502 dB apart — genuinely 4-decimal
    # values, so ':g' and a 2-decimal round visibly differ.
    art = {"anchor_curve": [[0.0, -50.0], [89.75, 39.82]], "min_gain_db": 0.0,
           "max_gain_db": 89.75, "gain_ceiling_db": 89.75, "gain_step_db": 0.25,
           "readings": {"limiting": {"kind": "same"}}, "center_freq_hz": 1.57542e9}
    bounds = {"min_power_dbm": -50.0, "max_power_dbm": 39.82, "quantity": "spectral density",
              "operating_plane": "sdr_output", "artifact": art}
    specs = [{"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz",
              "default": 1575.42, "is_freq": True},
             {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
              "unit": "dBm/MHz", "snap_role": "power"}]                # NO default → QLineEdit
    f = ParamForm()
    f.set_params(specs, cal_bounds=bounds, absolute_allowed=True,
                 default_power_mode="absolute", cal_freq_param="freq")
    w = f._widgets["power"][0]
    assert isinstance(w, QLineEdit)                                    # the crux: a text box
    assert f._power_decimals() == 2                                    # 0.25 dB grid → 2 decimals

    drag = -5.3137
    snapped = PowerFold.from_artifact(art).snap_power(drag, 1.57542e9)
    # premise: the snapped level really does carry more than 2 decimals (so ':g' would leak them).
    assert len(repr(round(snapped, 6)).split(".")[1].rstrip("0")) > 2
    _rails(f)[0].valueChanged.emit(drag)                              # a raw drag value (pre-snap)
    assert re.fullmatch(r"-?\d+\.\d{2}", w.text().replace("−", "-"))  # exactly 2 decimals, not ':g'
    assert w.text().replace("−", "-") == f"{snapped:.2f}"            # …the snapped level, rounded
