"""Offscreen tests for NoWheelFilter: a wheel over a combo box or spin box never
changes its value, the tick is handed to the enclosing scroll area so the page
still scrolls, and non-guarded widgets are left entirely alone."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QScrollArea, QSpinBox, QVBoxLayout,
    QWidget,
)

from ui.duration_spin import DurationSpinBox
from ui.no_wheel import NoWheelFilter

_app = QApplication.instance() or QApplication([])


def _wheel(widget, dy=-120):
    """A vertical wheel event centred on `widget` (negative dy = scroll down)."""
    pos = QPointF(widget.rect().center())
    gpos = QPointF(widget.mapToGlobal(widget.rect().center()))
    return QWheelEvent(pos, gpos, QPoint(0, dy), QPoint(0, dy),
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                       Qt.ScrollPhase.NoScrollPhase, False)


def test_filter_consumes_combo_wheel():
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(1)
    assert NoWheelFilter().eventFilter(combo, _wheel(combo)) is True
    assert combo.currentIndex() == 1        # untouched — we never delivered to it


def test_filter_consumes_spinbox_wheel():
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(5)
    assert NoWheelFilter().eventFilter(spin, _wheel(spin)) is True
    assert spin.value() == 5


def test_filter_consumes_double_spinbox_wheel():
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 10.0)
    spin.setValue(2.5)
    assert NoWheelFilter().eventFilter(spin, _wheel(spin)) is True
    assert spin.value() == 2.5


def test_filter_consumes_duration_spinbox_wheel():
    spin = DurationSpinBox()
    spin.setRange(0.0, 3600.0)
    spin.setValue(90.0)
    assert NoWheelFilter().eventFilter(spin, _wheel(spin)) is True
    assert spin.value() == 90.0


def test_non_guarded_wheel_passes_through():
    plain = QWidget()
    # Not a guarded control → the filter must decline (return False) and not touch it.
    assert NoWheelFilter().eventFilter(plain, _wheel(plain)) is False


def test_wheel_forwarded_to_enclosing_scroll_area():
    area = QScrollArea()
    content = QWidget()
    content.setFixedSize(180, 2000)         # taller than the viewport → scrollable
    lay = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(0)
    lay.addWidget(combo)
    lay.addWidget(spin)
    area.setWidget(content)
    area.resize(200, 200)
    area.show()

    bar = area.verticalScrollBar()
    filt = NoWheelFilter()

    assert bar.value() == 0
    filt.eventFilter(combo, _wheel(combo, dy=-120))    # over the combo
    assert combo.currentIndex() == 0                   # combo unchanged
    assert bar.value() > 0                              # page scrolled instead

    before = bar.value()
    filt.eventFilter(spin, _wheel(spin, dy=-120))      # over the spin box
    assert spin.value() == 0                            # spin unchanged
    assert bar.value() > before                         # page scrolled further


def test_installed_app_filter_suppresses_change_end_to_end():
    filt = NoWheelFilter()
    _app.installEventFilter(filt)
    try:
        combo = QComboBox()
        combo.addItems(["a", "b", "c"])
        combo.setCurrentIndex(2)
        QApplication.sendEvent(combo, _wheel(combo))
        assert combo.currentIndex() == 2

        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setValue(7)
        QApplication.sendEvent(spin, _wheel(spin))
        assert spin.value() == 7
    finally:
        _app.removeEventFilter(filt)
