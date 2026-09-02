"""ParamForm with a reported power-quantity bridge (docs/calibration-v2.md §13): the --power
field is labelled in the reported reading's script-defined unit (not always dBm), and its
range re-folds as the bridge's keyed parameter (e.g. --bw) is tuned — the same way it already
re-folds on a frequency change."""
import math
import os

import pytest

from ui.param_form import apply_power_bounds

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


FBW = {"id": "fbw", "name": "Full-bandwidth power", "in": "density", "out": "abs",
       "param": "bw", "coeff": 10.0, "ref": 1.0, "rep": 10.0}     # rep 10 MHz → +10 dB


def _artifact(operating_unit="dBm"):
    # measured DENSITY anchor: gain 40→-30, 74→4 dBm/MHz; reported = full-bandwidth power
    return {
        "anchor_curve": [[40.0, -30.0], [74.0, 4.0]],
        "passive_hops": [], "freq_dependent_limits": [],
        "gain_ceiling_db": 74.0, "min_gain_db": 40.0,
        "operating_unit": operating_unit,
        "readings": {"reported": {"kind": "law", "unit": operating_unit, "law": FBW},
                     "limiting": {"kind": "same"}},
    }


def _bounds(operating_unit="dBm"):
    return {"min_power_dbm": -20.0, "max_power_dbm": 14.0,
            "quantity": "full-bandwidth power", "operating_plane": "antenna",
            "artifact": _artifact(operating_unit)}


def _specs():
    return [
        {"dest": "bw", "flags": ["-Sweep-BW", "--bw"], "type": "float", "unit": "MHz",
         "min": 0.001, "max": 100.0, "default": 10.0,
         "presets": [{"label": "10", "key": "b10", "value": 10.0},
                     {"label": "100", "key": "b100", "value": 100.0}]},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "live": True, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 89.75, "live": True, "help": "rel"},
    ]


def _power_max(f):
    return f._widgets["power"][1]["max"]


# ── unit label ──────────────────────────────────────────────────────────────

def test_power_field_uses_reported_unit_not_dbm():
    out = apply_power_bounds(_specs(), _bounds(operating_unit="dBm/MHz"))
    sp = next(s for s in out if s["dest"] == "power")
    assert sp["unit"] == "dBm/MHz"        # script-defined reported unit, not the default dBm


def test_power_field_falls_back_to_dbm_without_operating_unit():
    b = _bounds()
    b["artifact"].pop("operating_unit")
    b["artifact"].pop("readings")
    out = apply_power_bounds(_specs(), b)
    sp = next(s for s in out if s["dest"] == "power")
    assert sp["unit"].startswith("dBm")   # plain calibration keeps the old label


# ── parameter re-fold ─────────────────────────────────────────────────────────

def test_power_range_folds_at_default_bandwidth():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    # reported max at rep bw 10 MHz: anchor top 4 dBm/MHz + 10*log10(10) = 14
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)


def test_changing_bandwidth_refolds_the_power_range():
    f = ParamForm()
    f.set_params(_specs(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    f._widgets["bw"][0].setCurrentText("100")      # operator widens the sweep to 100 MHz
    f._on_freq_changed()
    assert _power_max(f) == pytest.approx(24.0, abs=1e-6)   # 4 + 10*log10(100) = 24
    f._widgets["bw"][0].setCurrentText("10")
    f._on_freq_changed()
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)


def _specs_slider_bw():
    # --bw as a plain bounded number (no presets) → rendered as a spinbox + range rail.
    specs = _specs()
    specs[0] = {"dest": "bw", "flags": ["-Sweep-BW", "--bw"], "type": "float", "unit": "MHz",
                "min": 1.0, "max": 100.0, "default": 10.0, "step": 1.0}
    return specs


def test_bandwidth_slider_refolds_power_live_without_a_commit():
    # A drag on the --bw slider sets its spinbox value WITHOUT keyboard focus; that schedules a
    # debounced re-fold that fires once the drag settles — so --power re-folds without the
    # operator having to click elsewhere (only editingFinished did that before).
    from PyQt6.QtWidgets import QDoubleSpinBox
    f = ParamForm()
    f.set_params(_specs_slider_bw(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    bw = f._widgets["bw"][0]
    assert isinstance(bw, QDoubleSpinBox)          # a slider-backed spinbox, not a preset combo
    assert _power_max(f) == pytest.approx(14.0, abs=1e-6)
    assert not bw.hasFocus()                       # a rail drag doesn't focus the spinbox
    bw.setValue(100.0)                             # ← what RangeRail.valueChanged does on a drag
    assert f._refold_timer.isActive()              # a live change was queued (not fired per-tick)
    f._refold_timer_fire()                         # the drag settled (no mouse button held)
    assert _power_max(f) == pytest.approx(24.0, abs=1e-6)   # re-folded live, no commit needed


def test_typing_bandwidth_does_not_refold_until_commit():
    # A keystroke keeps focus on the field, so the live path is gated out (a mid-typing re-render
    # would steal focus); the commit path (editingFinished) still handles it.
    f = ParamForm()
    f.set_params(_specs_slider_bw(), cal_bounds=_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    bw = f._widgets["bw"][0]
    bw.setFocus()
    if bw.hasFocus():                              # offscreen may not grant focus; guard it
        f._refold_timer.stop()
        f._schedule_live_refold(bw)
        assert not f._refold_timer.isActive()      # typing does not schedule a live re-fold


# ── bridge keyed on a value the FORM DERIVES (GPS L1 C/A full_power on enbw_mhz) ──────────────
# The full-power law keys on an equivalent-noise bandwidth that has no input field and is
# non-analytic in the operator's control (--sidelobes). The script exposes it as a HIDDEN
# derived field with a nearest-integer table lookup, so the form computes it from --sidelobes
# and folds --power against it — mirroring what the transmit script produces at runtime.

# enbw_mhz(n) for n = 0..3 (Rc·frac(n)); index 0 is the main-lobe-only bandwidth.
_ENBW = [0.923588, 0.971788, 0.988638, 0.997168]
ENBW_LAW = {"id": "full_power", "name": "Full signal power", "in": "density", "out": "abs",
            "k": 60.0, "param": "enbw_mhz", "coeff": 10.0, "ref": 1.0, "rep": 0.988638}


def _specs_sidelobes():
    # --sidelobes slider (0..3, step 1) + a VISIBLE passband readout (linear) + a HIDDEN enbw
    # readout (table) the full_power law keys on.
    return [
        {"dest": "sidelobes", "flags": ["-Sidelobes", "--sidelobes"], "type": "int",
         "min": 0, "max": 3, "step": 1, "default": 2, "live": True},
        {"dest": "passband_bw_mhz", "flags": ["-Passband"], "type": "float", "kind": "derived",
         "unit": "MHz", "formula": {"linear": ["sidelobes", 2.046, 2.046]}},
        {"dest": "enbw_mhz", "flags": ["-ENBW"], "type": "float", "kind": "derived",
         "unit": "MHz", "hidden": True, "formula": {"table": ["sidelobes", *_ENBW]}},
        {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "unit": "dBm",
         "min": -140.0, "max": 60.0, "default": -20.0, "live": True, "help": "abs"},
        {"dest": "gain", "flags": ["-Gain", "--gain"], "type": "float", "unit": "dB",
         "min": 0.0, "max": 89.75, "live": True, "help": "rel"},
    ]


def _sidelobe_bounds():
    art = {
        "anchor_curve": [[40.0, -100.0], [89.75, -50.25]],   # dBm/Hz density
        "passive_hops": [], "freq_dependent_limits": [],
        "gain_ceiling_db": 89.75, "min_gain_db": 40.0, "operating_unit": "dBm",
        "readings": {"reported": {"kind": "law", "unit": "dBm", "law": ENBW_LAW},
                     "limiting": {"kind": "same"}},
    }
    return {"min_power_dbm": -40.0, "max_power_dbm": 10.0, "quantity": "full power",
            "operating_plane": "antenna", "artifact": art}


def test_hidden_derived_field_is_not_rendered_but_passband_is():
    f = ParamForm()
    f.set_params(_specs_sidelobes(), cal_bounds=_sidelobe_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    rendered = {info["spec"]["dest"] for info in f._derived.values()}
    assert "passband_bw_mhz" in rendered           # the operator-facing bandwidth readout
    assert "enbw_mhz" not in rendered              # the internal law input stays hidden
    assert "enbw_mhz" not in f._widgets            # and has no input widget


def test_bridge_keyed_on_derived_field_folds_from_its_source():
    from PyQt6.QtWidgets import QSpinBox
    f = ParamForm()
    f.set_params(_specs_sidelobes(), cal_bounds=_sidelobe_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    assert isinstance(f._widgets["sidelobes"][0], QSpinBox)   # a 0..3 slider, not a combo
    # the full_power law's keyed param resolves to the derived enbw, keyed on --sidelobes
    assert f._bridge_param_dests() == ["enbw_mhz"] and f._param_dependent()
    # anchor top density -50.25 dBm/Hz → full power = -50.25 + 60 + 10·log10(enbw)
    for n in (0, 1, 2, 3):
        f._widgets["sidelobes"][0].setValue(n)     # re-fetch: a refold rebuilds the form
        f._on_freq_changed()
        assert f._live_params() == {"enbw_mhz": pytest.approx(_ENBW[n])}
        expected = -50.25 + 60.0 + 10.0 * math.log10(_ENBW[n])
        assert _power_max(f) == pytest.approx(round(expected, 2), abs=0.011)


def test_zero_sidelobes_full_power_equals_main_lobe():
    # At 0 sidelobes the passband IS the main lobe, so the full-power reading folds identically
    # to a main-lobe-power reading (k = 10·log10(Rc·I_ML) = 59.654784). Compared at the fold
    # math (PowerFold), which always folds — the ParamForm number only re-derives from the
    # artifact when the bridge is parameter-dependent.
    from state.power_fold import PowerFold
    main = {"id": "main_lobe_power", "name": "Main-lobe power", "in": "density", "out": "abs",
            "k": 59.654784}
    art = _sidelobe_bounds()["artifact"]
    full = PowerFold.from_artifact(art)
    art_main = dict(art)
    art_main["readings"] = {"reported": {"kind": "law", "unit": "dBm", "law": main},
                            "limiting": {"kind": "same"}}
    lobe = PowerFold.from_artifact(art_main)
    assert full._reading_delta(full._reported, {"enbw_mhz": _ENBW[0]}) == pytest.approx(
        lobe._reading_delta(lobe._reported, None), abs=1e-4)
    # and the ParamForm --power max at 0 sidelobes lands on that shared value (density top
    # -50.25 dBm/Hz + 59.654784 = 9.40 dBm)
    f = ParamForm()
    f.set_params(_specs_sidelobes(), cal_bounds=_sidelobe_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    f._widgets["sidelobes"][0].setValue(0)
    f._on_freq_changed()
    assert _power_max(f) == pytest.approx(round(-50.25 + 59.654784, 2), abs=0.011)


def test_eval_formula_linear_and_table_ops():
    f = ParamForm()
    f.set_params(_specs_sidelobes(), cal_bounds=_sidelobe_bounds(), absolute_allowed=True,
                 default_power_mode="absolute")
    f._widgets["sidelobes"][0].setValue(3)
    assert f._eval_formula({"linear": ["sidelobes", 2.046, 2.046]}) == pytest.approx(2.046 * 4)
    assert f._eval_formula({"table": ["sidelobes", *_ENBW]}) == pytest.approx(_ENBW[3])
    # out-of-range index clamps to the ends of the table
    f._widgets["sidelobes"][0].setValue(3)
    assert f._eval_formula({"table": ["sidelobes", 5.0, 6.0]}) == pytest.approx(6.0)
    # numeric literals aren't treated as field sources
    assert ParamForm._formula_sources({"formula": {"linear": ["sidelobes", 2.0, 1.0]}}) \
        == ["sidelobes"]
