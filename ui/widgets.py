"""
Small shared widgets used across tabs.
"""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel

from .theme import status_color


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
        self.set_status(text, status or text)

    def set_status(self, text: str, status: str | None = None) -> None:
        fg, bg = status_color(status if status is not None else text)
        self.setText(text.upper())
        self.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: 9px; "
            f"padding: 2px 9px; font-size: 11px; font-weight: 700; "
            f"letter-spacing: 0.3px;"
        )