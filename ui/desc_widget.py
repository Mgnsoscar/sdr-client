"""CollapsibleDescription — a compact way to show a possibly multi-line task / sequence /
plan description. A single-line description shows as-is; a multi-line one shows its first
line with a 'Show more' toggle that reveals the rest, so lists stay tidy while still
allowing detailed documentation."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget

from .theme import Palette


def description_editor(placeholder: str = "optional — multiple lines allowed",
                       rows: int = 4) -> QPlainTextEdit:
    """A multi-line description input: a few rows tall, then scrolls; Tab moves focus on
    instead of inserting a tab. Shared by the task / sequence / plan editors."""
    edit = QPlainTextEdit()
    edit.setPlaceholderText(placeholder)
    edit.setTabChangesFocus(True)
    edit.setFixedHeight(edit.fontMetrics().lineSpacing() * rows + 12)
    return edit


class CollapsibleDescription(QWidget):
    """Shows ``text``; when it spans multiple lines, collapses to the first line with a
    'Show more / Show less' toggle. Empty text renders nothing."""

    def __init__(self, text: str, color: Optional[str] = None,
                 font_px: int = 11, parent=None):
        super().__init__(parent)
        self._full = (text or "").rstrip("\n")
        lines = self._full.split("\n")
        self._first = lines[0] if lines else ""
        self._multi = len(lines) > 1
        self._expanded = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setStyleSheet(f"font-size: {font_px}px; color: {color or Palette.TEXT_FAINT};")
        v.addWidget(self._label)

        self._toggle = QToolButton()
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setStyleSheet(
            f"QToolButton {{ border: none; padding: 0; color: {Palette.ACCENT_INK}; "
            f"font-size: {max(9, font_px - 1)}px; font-weight: 600; }}")
        self._toggle.clicked.connect(self._flip)
        self._toggle.setVisible(self._multi)
        v.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        self._render()

    def _render(self) -> None:
        self._label.setText(self._full if self._expanded else self._first)
        self._toggle.setText("Show less ▴" if self._expanded else "Show more ▾")

    def _flip(self) -> None:
        self._expanded = not self._expanded
        self._render()
