"""
CodeEditor — a light, IDE-style Python source editor.

A QPlainTextEdit with a line-number gutter, a soft current-line highlight, a
4-space tab stop, and Python syntax highlighting (ui/py_highlighter). Styled to
match the app's light surface so it sits inside the window rather than a dark
pane. Editable; the owning panel tracks dirty state via textChanged.
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QTextFormat
from PyQt6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from .py_highlighter import PythonHighlighter
from .theme import Palette


class _LineNumberArea(QWidget):
    """The gutter. Painting/width live on the editor so it can measure the digits."""

    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self._editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    GUTTER_TEXT   = "#B4BCC8"   # line numbers
    GUTTER_ACTIVE = "#5C6675"   # current line's number
    CURRENT_LINE  = "#F1F6FB"   # current-line band
    EDITOR_BG     = Palette.SURFACE
    EDITOR_FG     = Palette.TEXT

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._gutter = _LineNumberArea(self)

        mono = QFont("IBM Plex Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self.setFont(mono)
        # A tab is four spaces wide (Qt's default 80px looks far wider in a mono font).
        self.setTabStopDistance(QFontMetricsF(mono).horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._highlighter = PythonHighlighter(self.document())

        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {self.EDITOR_BG}; color: {self.EDITOR_FG};"
            f" border: none; padding: 6px 8px; selection-background-color: #CFE4F6;"
            f" selection-color: {self.EDITOR_FG}; }}"
        )

        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width(0)
        self._highlight_current_line()

    # ── gutter geometry ─────────────────────────────────────────────────────────
    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width(0)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(),
                                       self.line_number_area_width(), cr.height()))

    # ── current-line band ────────────────────────────────────────────────────────
    def _highlight_current_line(self) -> None:
        selections = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(self.CURRENT_LINE))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        self.setExtraSelections(selections)
        self._gutter.update()

    # ── gutter paint ─────────────────────────────────────────────────────────────
    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(self.EDITOR_BG))

        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur_line = self.textCursor().blockNumber()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                active = num == cur_line
                painter.setPen(QColor(self.GUTTER_ACTIVE if active else self.GUTTER_TEXT))
                painter.drawText(0, top, self._gutter.width() - 8,
                                 self.fontMetrics().height(),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1
        painter.end()
