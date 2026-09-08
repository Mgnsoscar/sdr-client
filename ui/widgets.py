"""
Small shared widgets used across tabs.
"""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QSizePolicy, QWidget

from .theme import status_color


def fit_dialog_to_screen(dialog: QWidget, want_w: int, want_h: int,
                         *, fw: float = 0.94, fh: float = 0.92, cap_max: bool = True) -> None:
    """Size a top-level window to ``(want_w, want_h)`` but never larger than a fraction of the
    current screen's AVAILABLE geometry (excludes the taskbar), and never with a forced minimum
    bigger than that cap — so on a short or DPI-scaled viewport (e.g. a 768-px panel at Windows
    125% ≈ 614 logical px) the buttons stay on-screen and it can still shrink into view.

    A dialog whose BODY scrolls keeps its footer (Save/Cancel) pinned and visible because the cap
    shrinks the scroll area, not the footer. Call once after the layout is built. This generalizes
    the cap ramp_editor introduced; height matters most, but width is capped too for the wide
    plan/hold dialogs on a narrow scaled screen.

    ``cap_max`` also pins ``setMaximumHeight`` so a modal dialog can't be dragged taller than the
    screen. Pass ``cap_max=False`` for a RESIZABLE main window (which the user must still be able to
    maximize) — then only the initial size is capped, not the growable maximum."""
    scr = dialog.screen() or QApplication.primaryScreen()
    if scr is not None:
        avail = scr.availableGeometry()
        if avail.height() > 240:
            cap_h = int(avail.height() * fh)
            want_h = min(want_h, cap_h)
            if cap_max:
                dialog.setMaximumHeight(cap_h)             # can't grow taller than the screen
            if dialog.minimumHeight() > cap_h:             # relax an unshrinkable floor
                dialog.setMinimumHeight(cap_h)
        if avail.width() > 240:
            cap_w = int(avail.width() * fw)
            want_w = min(want_w, cap_w)
            if dialog.minimumWidth() > cap_w:
                dialog.setMinimumWidth(cap_w)
    dialog.resize(max(dialog.minimumWidth(), want_w), max(dialog.minimumHeight(), want_h))


def natural_key(text: str):
    """A case-insensitive, digit-aware sort key so lists order the way people expect
    — 'task2' before 'task10', 'A' next to 'a'. Use as `sorted(items, key=...)`."""
    s = text or ""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", s)]


class StatusPill(QLabel):
    """A small rounded badge showing a status word in its semantic color."""

    def __init__(self, text: str = "", status: str | None = None, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Keep the badge at its natural height — otherwise, in a row made tall by a
        # neighbour (e.g. an expanded description), the label's coloured background
        # stretches to the full row height and the pill balloons.
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_status(text, status or text)

    def set_status(self, text: str, status: str | None = None) -> None:
        fg, bg = status_color(status if status is not None else text)
        self.setText(text.upper())
        self.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: 9px; "
            f"padding: 2px 9px; font-size: 11px; font-weight: 700; "
            f"letter-spacing: 0.3px;"
        )