"""
CodeEditor — a light, IDE-style Python source editor for the Scripts tab.

Prefers QScintilla (PyQt6-QScintilla) when it's installed: native code folding
for functions / classes / docstrings, a line-number margin, current-line
highlight, brace matching, and a Python lexer — all styled to the app's light
palette so the editor blends into the window.

When QScintilla isn't available it falls back to a QPlainTextEdit with our own
line-number gutter + Python highlighter (ui/py_highlighter), so the app still
runs (without folding) if the optional dependency is missing.

Both variants expose the same small surface used by ScriptsPanel:
    setPlainText(str) / toPlainText() -> str / clear() / setPlaceholderText(str)
    and the textChanged signal.

`CodeEditor(parent)` is a factory returning the best available implementation.
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QTextFormat
from PyQt6.QtWidgets import QFrame, QPlainTextEdit, QTextEdit, QWidget

from .py_highlighter import PythonHighlighter, Syntax
from .theme import Palette

# Shared light-editor colours.
GUTTER_TEXT   = "#B4BCC8"
GUTTER_ACTIVE = "#5C6675"
CURRENT_LINE  = "#F1F6FB"
SELECTION     = "#CFE4F6"
FOLD_MARGIN   = "#EEF2F6"
FOLD_ARROW    = "#A6AEBA"   # small, light fold arrow (▶/▼)

try:
    from PyQt6.Qsci import QsciScintilla, QsciLexerPython, QsciScintillaBase
    _HAVE_QSCI = True
except Exception:  # noqa: BLE001 — optional dependency
    _HAVE_QSCI = False


def _editor_font() -> QFont:
    f = QFont("IBM Plex Mono")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    return f


def _sci_color(hex_str: str) -> int:
    """A #RRGGBB colour as Scintilla's 0xBBGGRR integer."""
    c = QColor(hex_str)
    return c.red() | (c.green() << 8) | (c.blue() << 16)


# ── QScintilla implementation (preferred) ────────────────────────────────────────
if _HAVE_QSCI:

    class _SciCodeEditor(QsciScintilla):
        """QScintilla editor styled to the app's light theme, with folding."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            font = _editor_font()
            self.setFont(font)

            lex = QsciLexerPython(self)
            lex.setDefaultFont(font)
            lex.setFont(font)
            lex.setDefaultPaper(QColor(Palette.SURFACE))
            lex.setDefaultColor(QColor(Syntax.TEXT))
            L = QsciLexerPython
            palette = {
                L.Default: Syntax.TEXT,
                L.Keyword: Syntax.KEYWORD,
                L.Comment: Syntax.COMMENT,
                L.CommentBlock: Syntax.COMMENT,
                L.SingleQuotedString: Syntax.STRING,
                L.DoubleQuotedString: Syntax.STRING,
                L.TripleSingleQuotedString: Syntax.STRING,
                L.TripleDoubleQuotedString: Syntax.STRING,
                L.UnclosedString: Syntax.STRING,
                L.Number: Syntax.NUMBER,
                L.FunctionMethodName: Syntax.FUNCTION,
                L.ClassName: Syntax.CLASSNAME,
                L.Decorator: Syntax.DECORATOR,
                L.Operator: "#5C6675",
                L.Identifier: Syntax.TEXT,
                L.HighlightedIdentifier: Syntax.TEXT,
            }
            for style, color in palette.items():
                lex.setColor(QColor(color), style)
                lex.setPaper(QColor(Palette.SURFACE), style)
                lex.setFont(font, style)
            # Fold docstrings/comments too, not just compound statements.
            lex.setFoldComments(True)
            lex.setFoldQuotes(True)
            self.setLexer(lex)
            self._lexer = lex

            # Line-number margin (0); hide the symbol margin (1); folding margin (2).
            self.setMarginType(0, QsciScintilla.MarginType.NumberMargin)
            self.setMarginLineNumbers(0, True)
            self.setMarginWidth(0, "0000")
            self.setMarginWidth(1, 0)
            self.setMarginsForegroundColor(QColor(GUTTER_TEXT))
            self.setMarginsBackgroundColor(QColor(Palette.SURFACE))
            self.setMarginsFont(font)
            self.setFolding(QsciScintilla.FoldStyle.PlainFoldStyle, 2)
            self.setMarginWidth(2, 9)     # a narrow fold margin → a small, unobtrusive arrow
            self.setFoldMarginColors(QColor(Palette.SURFACE), QColor(Palette.SURFACE))
            # PyCharm-style fold markers: a small, light arrow (▶ collapsed / ▼ expanded),
            # no boxed tree lines.
            B = QsciScintillaBase
            self.SendScintilla(B.SCI_MARKERDEFINE, B.SC_MARKNUM_FOLDER, B.SC_MARK_ARROW)
            self.SendScintilla(B.SCI_MARKERDEFINE, B.SC_MARKNUM_FOLDEROPEN, B.SC_MARK_ARROWDOWN)
            for _n in (B.SC_MARKNUM_FOLDERSUB, B.SC_MARKNUM_FOLDERTAIL, B.SC_MARKNUM_FOLDEREND,
                       B.SC_MARKNUM_FOLDEROPENMID, B.SC_MARKNUM_FOLDERMIDTAIL):
                self.SendScintilla(B.SCI_MARKERDEFINE, _n, B.SC_MARK_EMPTY)
            _grey = _sci_color(FOLD_ARROW)
            for _n in (B.SC_MARKNUM_FOLDER, B.SC_MARKNUM_FOLDEROPEN):
                self.SendScintilla(B.SCI_MARKERSETFORE, _n, _grey)
                self.SendScintilla(B.SCI_MARKERSETBACK, _n, _grey)

            self.setCaretLineVisible(True)
            self.setCaretLineBackgroundColor(QColor(CURRENT_LINE))
            self.setCaretForegroundColor(QColor(Syntax.TEXT))
            self.setSelectionBackgroundColor(QColor(SELECTION))
            self.setSelectionForegroundColor(QColor(Syntax.TEXT))

            self.setIndentationsUseTabs(False)
            self.setTabWidth(4)
            self.setIndentationGuides(True)
            self.setAutoIndent(True)
            self.setBraceMatching(QsciScintilla.BraceMatch.SloppyBraceMatch)
            self.setMatchedBraceForegroundColor(QColor(Palette.ACCENT))
            self.setWrapMode(QsciScintilla.WrapMode.WrapNone)
            self.setFrameStyle(QFrame.Shape.NoFrame)
            self.setEolMode(QsciScintilla.EolMode.EolUnix)
            self._placeholder = ""

        # ── QPlainTextEdit-compatible surface used by ScriptsPanel ──
        def setPlainText(self, text: str) -> None:   # noqa: N802
            self.setText(text)

        def toPlainText(self) -> str:                # noqa: N802
            return self.text()

        def setPlaceholderText(self, text: str) -> None:  # noqa: N802
            self._placeholder = text                 # QScintilla has no native placeholder

    CodeEditorImpl = _SciCodeEditor
    HAS_FOLDING = True


# ── QPlainTextEdit fallback (no QScintilla) ──────────────────────────────────────
class _LineNumberArea(QWidget):
    def __init__(self, editor: "_PlainCodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self._editor.paint_line_numbers(event)


class _PlainCodeEditor(QPlainTextEdit):
    """Fallback editor: light QPlainTextEdit with a gutter + our Python highlighter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._gutter = _LineNumberArea(self)
        mono = _editor_font()
        self.setFont(mono)
        self.setTabStopDistance(QFontMetricsF(mono).horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._highlighter = PythonHighlighter(self.document())
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {Palette.SURFACE}; color: {Syntax.TEXT};"
            f" border: none; padding: 6px 8px; selection-background-color: {SELECTION};"
            f" selection-color: {Syntax.TEXT}; }}"
        )
        self.blockCountChanged.connect(lambda _c: self._update_gutter_width())
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(),
                                       self.line_number_area_width(), cr.height()))

    def _highlight_current_line(self) -> None:
        selections = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(CURRENT_LINE))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        self.setExtraSelections(selections)
        self._gutter.update()

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(Palette.SURFACE))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur_line = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                active = num == cur_line
                painter.setPen(QColor(GUTTER_ACTIVE if active else GUTTER_TEXT))
                painter.drawText(0, top, self._gutter.width() - 8,
                                 self.fontMetrics().height(),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1
        painter.end()


if not _HAVE_QSCI:
    CodeEditorImpl = _PlainCodeEditor
    HAS_FOLDING = False


def CodeEditor(parent=None):  # noqa: N802 — factory named like a class for call sites
    """Return the best available editor: QScintilla (with folding) if installed,
    else the QPlainTextEdit fallback."""
    return CodeEditorImpl(parent)
