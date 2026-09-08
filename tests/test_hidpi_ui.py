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

from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea, QWidget

from api import models as m
import ui.arm_dialog as ad
import ui.scripts_panel as scp
import ui.sequences_panel as sp
from ui.widgets import fit_dialog_to_screen

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


# ── vertical overflow on a shorter viewport: dialogs cap to the screen ──────────────────
# When the screen is less tall (a short panel, or a 125%-scaled one now that the app is
# genuinely DPI-aware), a dialog that hardcodes a tall height used to push its footer buttons
# off-screen. fit_dialog_to_screen caps every tall dialog to the available geometry.

def test_fit_dialog_to_screen_caps_height_and_relaxes_over_tall_minimum():
    avail = _app.primaryScreen().availableGeometry()
    cap_h = int(avail.height() * 0.92)
    w = QWidget()
    w.setMinimumHeight(avail.height() + 500)         # an unshrinkable floor taller than the screen
    fit_dialog_to_screen(w, 100000, 100000)          # ask for an absurd size
    assert w.maximumHeight() <= cap_h                # can't grow taller than the screen
    assert w.minimumHeight() <= cap_h                # the over-tall floor was relaxed → shrinkable
    assert w.height() <= cap_h


def test_fit_dialog_to_screen_cap_max_false_stays_maximizable():
    # A main window must remain freely resizable/maximizable: only the initial size is capped.
    w = QWidget()
    fit_dialog_to_screen(w, 100000, 100000, cap_max=False)
    assert w.maximumHeight() == _QWIDGETSIZE_MAX     # no fixed maximum applied
    assert w.height() <= int(_app.primaryScreen().availableGeometry().height() * 0.92)


def test_arm_dialog_footer_is_pinned_outside_the_scroll_area():
    # The arm/proceed dialog stacks many sections; its body now scrolls so the Arm/Cancel footer
    # stays reachable on a short viewport. Guard: the button box is NOT inside the scroll body.
    dlg = ad.ArmDialog("Arm test", 5.0, 60.0, 0.0)
    try:
        scrolls = dlg.findChildren(QScrollArea)
        assert scrolls, "arm dialog body should live in a scroll area"
        body = scrolls[0].widget()
        box = dlg.findChild(QDialogButtonBox)
        assert box is not None
        assert not body.isAncestorOf(box)            # footer pinned outside the scroll
    finally:
        dlg.deleteLater()
