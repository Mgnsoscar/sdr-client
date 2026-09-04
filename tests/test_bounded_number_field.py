"""BoundedNumberField — the parameter form's bounded numeric field (spinbox + range rail
+ limit chip) as a reusable widget, used by the ramp editor's From/To."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from state.power_fold import PowerFold
from ui.param_form import BoundedNumberField, _AchievableSpin

_app = QApplication.instance() or QApplication([])

_SPEC = {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm",
         "min": -1.8, "max": 28.2, "step": 0.5, "default": 0.0}


def test_bounds_render_and_clamp():
    f = BoundedNumberField(_SPEC)
    assert (f._spin.minimum(), f._spin.maximum()) == (-1.8, 28.2)
    assert f._chip is not None and f._rail is not None    # min/max chip + slider present
    f.setValue(100)
    assert f.value() == 28.2                               # clamped to max
    f.setValue(-100)
    assert f.value() == -1.8                               # clamped to min


def test_unit_suffix_and_value_changed_signal():
    f = BoundedNumberField(_SPEC)
    assert f._spin.suffix().strip() == "dBm"
    seen = []
    f.valueChanged.connect(lambda: seen.append(f.value()))
    f.setValue(5.0)
    assert seen and seen[-1] == 5.0


def test_rail_note_is_shown():
    f = BoundedNumberField(_SPEC, note="Range at 2000.00 MHz · moves with frequency")
    assert f._rail._note is not None
    assert "2000.00 MHz" in f._rail._note.text()


def test_power_snap_uses_the_achievable_grid():
    # snap_role 'power' + a fold ⇒ the spinbox steps/commits on the chain's real levels.
    art = {"anchor_curve": [[40.0, -30.0], [74.0, 4.0]], "min_gain_db": 40.0,
           "gain_ceiling_db": 74.0, "gain_step_db": 2.0}
    fold = PowerFold.from_artifact(art)
    spec = {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm",
            "min": -30.0, "max": 4.0, "step": fold.finest_step(), "snap_role": "power"}
    f = BoundedNumberField(spec, fold=fold, fold_freq=1.5e9)
    assert isinstance(f._spin, _AchievableSpin)
    f.setValue(-10.0)
    assert -30.0 <= f.value() <= 4.0


def test_value_at_a_sub_display_precision_max_is_not_flagged_over():
    # A calibrated --power view shifted by a log10 view_offset (a chirp's live density) gives a bound
    # FINER than the display: a -10.2503 dBm/MHz max shows at 2 decimals as -10.25. The spinbox rounds
    # its OWN maximum up to -10.25 (> the true -10.2503), so a value AT the max must NOT be flagged
    # "clamped" — a difference below half a display step is treated as ON the bound, else the operator
    # can't select the max (owner report: "range is (x to -10.2503), setting -10.25 doesn't work").
    spec = {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm/MHz",
            "min": -30.2503, "max": -10.2503, "step": 0.01}
    f = BoundedNumberField(spec)
    assert f._spin.decimals() == 2
    assert f._spin.maximum() == pytest.approx(-10.25)     # the spinbox rounds its max up
    f.setValue(f._spin.maximum())                         # drag/type to the displayed max
    assert f._warn is not None and not f._warn.isVisible()  # NOT falsely flagged over


def test_unbounded_numeric_has_no_rail_but_still_reads_writes():
    f = BoundedNumberField({"dest": "x", "type": "float", "step": 0.1})
    assert f._rail is None and f._chip is None            # no min/max ⇒ no rail/limit chip
    f.setValue(3.5)
    assert f.value() == 3.5
