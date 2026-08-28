"""CollapsibleDescription: single-line shows as-is with no toggle; multi-line collapses to
the first line and a toggle reveals / re-hides the rest."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui.desc_widget import CollapsibleDescription

_app = QApplication.instance() or QApplication([])


def test_single_line_shows_fully_with_no_toggle():
    d = CollapsibleDescription("Just one line.")
    assert d._label.text() == "Just one line."
    assert d._toggle.isHidden()                             # no toggle for one line


def test_multi_line_collapses_to_first_line_and_expands():
    d = CollapsibleDescription("Summary line.\nDetail line one.\nDetail line two.")
    assert d._label.text() == "Summary line."               # collapsed by default
    assert not d._toggle.isHidden() and "more" in d._toggle.text()
    d._flip()                                               # expand
    assert d._label.text() == "Summary line.\nDetail line one.\nDetail line two."
    assert "less" in d._toggle.text()
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
