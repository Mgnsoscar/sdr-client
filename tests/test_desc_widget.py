"""CollapsibleDescription: single-line shows as-is with no toggle; multi-line collapses to
the first line and a toggle reveals / re-hides the rest."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.desc_widget import CollapsibleDescription

_app = QApplication.instance() or QApplication([])


def test_single_line_shows_fully():
    d = CollapsibleDescription("Just one line.")
    assert d._label.text() == "Just one line."


def test_multi_line_collapses_to_first_line_and_expands():
    d = CollapsibleDescription("Summary line.\nDetail line one.\nDetail line two.")
    assert d._label.text() == "Summary line."               # collapsed by default
    d._flip()                                               # expand
    assert d._label.text() == "Summary line.\nDetail line one.\nDetail line two."
    d._flip()                                               # collapse again
    assert d._label.text() == "Summary line."


def test_collapsed_first_line_stays_on_one_line():
    # Collapsed, the first line must not wrap (it's elided to a single line); expanded,
    # word-wrap is back on so the full text can flow. This is what stopped a long first
    # line splitting into several stacked lines in a narrow unit-detail column.
    d = CollapsibleDescription("A long summary line.\nDetail.")
    assert d._label.wordWrap() is False                     # collapsed → one line
    d._flip()
    assert d._label.wordWrap() is True                      # expanded → wraps


def test_single_line_keeps_word_wrap():
    # A single-line description has no toggle, so it must stay wrapped (never elided) —
    # otherwise long single-line text would be truncated with no way to reveal it.
    d = CollapsibleDescription("One long line with no explicit breaks in it at all.")
    assert d._label.wordWrap() is True


def test_clicking_the_text_toggles_expansion():
    # Clicking the description text itself expands/collapses it (like the toggle link).
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    d = CollapsibleDescription("Summary line.\nDetail line.")
    assert d._expanded is False

    def click():
        pos = QPointF(2, 2)
        for etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            ev = QMouseEvent(etype, pos, Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            _app.sendEvent(d._label, ev)

    click()
    assert d._expanded is True                              # a plain click expands
    click()
    assert d._expanded is False                             # and collapses again


def test_single_line_text_is_not_click_toggle():
    # A single-line description has nothing to reveal, so its text isn't a toggle target.
    d = CollapsibleDescription("Only one line.")
    assert d._press_pos is None
