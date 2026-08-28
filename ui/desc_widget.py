"""CollapsibleDescription — a compact way to show a possibly multi-line task / sequence /
plan description. A single-line description shows as-is; a multi-line one shows its first
line with a 'Show more' toggle that reveals the rest, so lists stay tidy while still
allowing detailed documentation."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QLabel, QPlainTextEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

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

    def _collapsed_to_one_line(self) -> bool:
        """Collapsed multi-line descriptions show ONLY the first line — elided to one
        line. (A single-line description has no toggle and always shows in full.)"""
        return self._multi and not self._expanded

    def _render(self) -> None:
        if self._collapsed_to_one_line():
            self._label.setWordWrap(False)          # keep the first line on ONE line
            # Let the label shrink below its text width so the line can be elided to fit.
            self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self._label.setText(self._elided(self._first))
        else:
            self._label.setWordWrap(True)           # full text wraps at the column width
            # Preferred (not Ignored) so the label fills the available width and wraps
            # normally instead of collapsing to a sliver and stacking every word.
            self._label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            self._label.setText(self._full if self._expanded else self._first)
        self._toggle.setText("Show less ▴" if self._expanded else "Show more ▾")

    def _elided(self, text: str) -> str:
        w = self._label.width()
        if w <= 0:
            return text                              # not laid out yet; resizeEvent re-elides
        return self._label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, w)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-elide the collapsed first line whenever the available width changes.
        if self._collapsed_to_one_line():
            self._label.setText(self._elided(self._first))

    def _flip(self) -> None:
        self._expanded = not self._expanded
        self._render()
