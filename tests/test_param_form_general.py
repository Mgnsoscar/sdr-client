"""General parameter-form behaviours: '.' decimal separator regardless of OS locale, an
empty numeric field (no default) reads as empty rather than a misleading 0, the mouse wheel
doesn't nudge an unfocused field, and a rail drag snaps to the script's step."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QLocale, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QDoubleSpinBox, QLineEdit

from ui.param_form import ParamForm

_app = QApplication.instance() or QApplication([])


def _spec(**kw):
    base = {"dest": "x", "flags": ["--x"], "type": "float"}
    base.update(kw)
    return base


def test_spinbox_uses_dot_decimal_regardless_of_locale():
    QLocale.setDefault(QLocale(QLocale.Language.German))      # a ',' locale
    try:
        f = ParamForm()
        f.set_params([_spec(min=0.0, max=10.0, step=0.5, default=2.5)])
        w, _s = f._widgets["x"]
        assert isinstance(w, QDoubleSpinBox)
        assert w.locale().decimalPoint() == "."
        assert w.textFromValue(2.5) == "2.5"
    finally:
        QLocale.setDefault(QLocale(QLocale.Language.C))


def test_numeric_field_without_default_is_an_empty_text_box():
    # No default → an empty QLineEdit with a placeholder, not a spinbox stuck at 0.
    f = ParamForm()
    f.set_params([_spec(min=0.0, max=89.75, step=0.25)])       # e.g. an unset --gain
    w, _s = f._widgets["x"]
    assert isinstance(w, QLineEdit)
    assert w.text() == "" and "allowed" in w.placeholderText()


def test_numeric_field_with_default_is_a_stepper():
    f = ParamForm()
    f.set_params([_spec(min=0.0, max=89.75, step=0.25, default=40.0)])
    assert isinstance(f._widgets["x"][0], QDoubleSpinBox)


def test_wheel_over_unfocused_field_is_ignored():
    f = ParamForm()
    f.set_params([_spec(min=0.0, max=10.0, step=1.0, default=5.0)])
    w, _s = f._widgets["x"]
    assert not w.hasFocus()
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPointF(0, 0).toPoint(),
                     QPointF(0, -120).toPoint(), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    before = w.value()
    handled = _app.sendEvent(w, ev)
    assert w.value() == before                                # value untouched by the wheel


def test_rail_drag_snaps_to_step():
    f = ParamForm()
    f.set_params([_spec(min=0.0, max=10.0, step=0.5, default=0.0)])
    frame = f._widgets["x"][0].parent()
    from ui.param_widgets import RangeRail
    rail = f.findChild(RangeRail)
    assert rail is not None
    rail.valueChanged.emit(3.17)                              # a drag lands off-grid
    assert f._widgets["x"][0].value() == pytest.approx(3.0)   # snapped to the 0.5 grid
