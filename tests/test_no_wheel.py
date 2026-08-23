"""Offscreen tests for NoComboWheelFilter: a wheel over a combo box never changes
its selection, the tick is handed to the enclosing scroll area so the page still
scrolls, and non-combo widgets are left entirely alone."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QScrollArea, QVBoxLayout, QWidget,
)

from ui.no_wheel import NoComboWheelFilter

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
    assert NoComboWheelFilter().eventFilter(combo, _wheel(combo)) is True
    assert combo.currentIndex() == 1        # untouched — we never delivered to it


def test_non_combo_wheel_passes_through():
    plain = QWidget()
    # Not a combo → the filter must decline (return False) and not touch the event.
    assert NoComboWheelFilter().eventFilter(plain, _wheel(plain)) is False


def test_wheel_forwarded_to_enclosing_scroll_area():
    area = QScrollArea()
    content = QWidget()
    content.setFixedSize(180, 2000)         # taller than the viewport → scrollable
    lay = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    combo.setCurrentIndex(0)
    lay.addWidget(combo)
    area.setWidget(content)
    area.resize(200, 200)
    area.show()

    bar = area.verticalScrollBar()
    assert bar.value() == 0
    NoComboWheelFilter().eventFilter(combo, _wheel(combo, dy=-120))
    # The combo did NOT change, and the page scrolled down instead.
    assert combo.currentIndex() == 0
    assert bar.value() > 0


def test_installed_app_filter_suppresses_change_end_to_end():
    filt = NoComboWheelFilter()
    _app.installEventFilter(filt)
    try:
        combo = QComboBox()
        combo.addItems(["a", "b", "c"])
        combo.setCurrentIndex(2)
        QApplication.sendEvent(combo, _wheel(combo))
        assert combo.currentIndex() == 2
    finally:
        _app.removeEventFilter(filt)
