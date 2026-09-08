"""Windows display-scaling (high-DPI) robustness guards — the testable subset of the
`/code-review` findings. Offscreen Qt can't simulate a 125% display, so these are cheap
regression locks, not DPI rendering tests:

  • a sequence row's action buttons stay SIZE-TO-CONTENT (min width, not fixed) so a longer
    label ("Proceed" > "Arm", "Hold now") or a wider fallback font can't clip to an ellipsis;
  • the file-tree SVG icons render per the screen device-pixel-ratio (cached by ratio) instead
    of a single DPR-1.0 32px raster that would blur when enlarged.

The manifest / high-DPI-policy / monospace-font fixes are build- or render-config that can't be
unit-tested; they're covered by the full suite still passing.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from api import models as m
import ui.scripts_panel as scp
import ui.sequences_panel as sp

_app = QApplication.instance() or QApplication([])

_QWIDGETSIZE_MAX = 16777215        # Qt's "no maximum" sentinel


def _idle_row():
    seq = m.Sequence(id="s1", name="Loss-of-lock test", steps=[])
    return sp._SequenceRow(seq, None, on_start=lambda s: None, on_stop=lambda s: None,
                           on_edit=lambda s: None, on_delete=lambda s: None,
                           on_log=lambda s: None, can_run=True, can_edit=True)


def test_row_action_buttons_size_to_content_not_fixed_width():
    # setFixedWidth clamps maximumWidth == minimumWidth; the fix uses setMinimumWidth so each
    # button keeps its floor for row alignment but can grow to fit its text.
    row = _idle_row()
    for b in (row._start, row._stop, row._log, row._edit, row._delete):
        assert b.minimumWidth() == 66
        assert b.maximumWidth() == _QWIDGETSIZE_MAX      # free to grow — not pinned to 66
    assert row._hold_now.minimumWidth() == 72
    assert row._hold_now.maximumWidth() == _QWIDGETSIZE_MAX
    assert row._edit_wb.minimumWidth() == 60
    assert row._edit_wb.maximumWidth() == _QWIDGETSIZE_MAX


def test_file_tree_svg_icon_renders_and_caches_per_dpr():
    if not scp._HAVE_SVG:
        pytest.skip("QtSvg unavailable in this build")
    scp._ICON_CACHE.clear()
    ic = scp._py_icon()
    assert not ic.isNull()                               # rendered a real raster
    assert scp._py_icon() is ic                          # repeat call is served from the cache
    # cache key is now (name, device_pixel_ratio), so a mixed-DPI move re-renders crisply.
    key = next(iter(scp._ICON_CACHE))
    assert isinstance(key, tuple) and key[0] == "py" and len(key) == 2
