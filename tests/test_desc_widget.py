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
