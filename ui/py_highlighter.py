"""
Python syntax highlighter for the Scripts editor.

A light-theme QSyntaxHighlighter tuned to the app's palette (see ui/theme.py): an
IntelliJ-Light-faithful set of colours — navy keywords, green strings, blue
numbers, gold function names, teal class/type names, purple built-ins, grey
italic comments — harmonised with the slate accent so the editor sits calmly
inside the light window.

Triple-quoted strings (and the module docstring) span lines, so they are tracked
with per-block state rather than a single-line regex.
"""
from __future__ import annotations

import keyword
from typing import List, Tuple

from PyQt6.QtCore import QRegularExpression
from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


# ── Palette (mirrors the accepted Scripts mockup) ────────────────────────────────
class Syntax:
    TEXT      = "#1E2530"
    KEYWORD   = "#0A3D8F"   # def / class / import / return / for …
    STRING    = "#127A2E"   # "..." '...' """..."""
    NUMBER    = "#1A63C9"   # 1575.42, 0x1F, 1_023_000
    COMMENT   = "#8A93A2"   # # …
    FUNCTION  = "#8A5A0B"   # name in `def name(` and at a call site
    CLASSNAME = "#0A6478"   # name in `class Name` and type annotations
    BUILTIN   = "#8A2FA0"   # print, len, True, False, None, self …
    DECORATOR = "#8A2FA0"   # @decorator


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.DemiBold)
    if italic:
        f.setFontItalic(True)
    return f


class PythonHighlighter(QSyntaxHighlighter):
    """Highlights Python source in a QTextDocument. Cheap, regex-based, good enough
    for viewing/editing scripts (not a full parser)."""

    # Block state: inside an unterminated triple-quoted string.
    _IN_TRIPLE_SINGLE = 1   # inside '''
    _IN_TRIPLE_DOUBLE = 2   # inside \"\"\"

    def __init__(self, document) -> None:
        super().__init__(document)
        self._kw = _fmt(Syntax.KEYWORD, bold=True)
        self._builtin = _fmt(Syntax.BUILTIN)
        self._string = _fmt(Syntax.STRING)
        self._comment = _fmt(Syntax.COMMENT, italic=True)
        self._number = _fmt(Syntax.NUMBER)
        self._func = _fmt(Syntax.FUNCTION)
        self._cls = _fmt(Syntax.CLASSNAME)
        self._deco = _fmt(Syntax.DECORATOR)

        self._rules: List[Tuple[QRegularExpression, QTextCharFormat]] = []

        # Keywords (True/False/None get the built-in colour, like the mockup).
        kws = [k for k in keyword.kwlist if k not in ("True", "False", "None")]
        self._rule(r"\b(" + "|".join(kws) + r")\b", self._kw)
        self._rule(r"\b(True|False|None)\b", self._builtin)

        # Common built-ins + self/cls.
        builtins = (
            "print len range int float str bool list dict set tuple bytes bytearray "
            "enumerate zip map filter sorted reversed sum min max abs round open "
            "isinstance issubclass getattr setattr hasattr super property staticmethod "
            "classmethod type object Exception ValueError TypeError KeyError OSError "
            "RuntimeError SystemExit self cls").split()
        self._rule(r"\b(" + "|".join(builtins) + r")\b", self._builtin)

        # Decorators.
        self._rule(r"^\s*@\s*[\w\.]+", self._deco)

        # def / class names (the identifier right after the keyword).
        self._rule(r"\bdef\s+(\w+)", self._func, group=1)
        self._rule(r"\bclass\s+(\w+)", self._cls, group=1)

        # Numbers (ints, floats, hex, underscores, exponent, complex).
        self._rule(r"\b0[xX][0-9a-fA-F_]+\b", self._number)
        self._rule(r"\b\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?[jJ]?\b", self._number)

        # Single-line strings (kept simple; triple-quoted handled separately below).
        self._rule(r"'[^'\\\n]*(?:\\.[^'\\\n]*)*'", self._string)
        self._rule(r'"[^"\\\n]*(?:\\.[^"\\\n]*)*"', self._string)

        # Comments last, so a '#' inside a string isn't caught here (strings already
        # applied; we skip characters already coloured as string in highlightBlock).
        self._comment_re = QRegularExpression(r"#[^\n]*")

    def _rule(self, pattern: str, fmt: QTextCharFormat, group: int = 0) -> None:
        self._rules.append((QRegularExpression(pattern), fmt, group))  # type: ignore[arg-type]

    # ── per-block highlight ──────────────────────────────────────────────────────
    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
        # 1) single-line rules (keywords, numbers, names, single-line strings).
        for rx, fmt, group in self._rules:  # type: ignore[misc]
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                start = m.capturedStart(group)
                length = m.capturedLength(group)
                if length > 0:
                    self.setFormat(start, length, fmt)
        # 2) comments — skip a '#' that is already inside a (single-line) string span.
        it = self._comment_re.globalMatch(text)
        while it.hasNext():
            m = it.next()
            start = m.capturedStart()
            if self.format(start).foreground().color() != QColor(Syntax.STRING):
                self.setFormat(start, m.capturedLength(), self._comment)
        # 3) triple-quoted strings LAST, so they override everything and carry state
        #    across blocks (module/function docstrings, multi-line literals).
        self._apply_triples(text)

    def _apply_triples(self, text: str) -> None:
        prev = self.previousBlockState()
        self.setCurrentBlockState(-1)
        start = 0
        if prev in (self._IN_TRIPLE_SINGLE, self._IN_TRIPLE_DOUBLE):
            quote = "'''" if prev == self._IN_TRIPLE_SINGLE else '"""'
            close = text.find(quote)
            if close == -1:                        # still open through this whole line
                self.setFormat(0, len(text), self._string)
                self.setCurrentBlockState(prev)
                return
            self.setFormat(0, close + 3, self._string)
            start = close + 3
        while True:
            s = text.find("'''", start)
            d = text.find('"""', start)
            if s == -1 and d == -1:
                return
            if d == -1 or (s != -1 and s < d):
                opener, quote, state = s, "'''", self._IN_TRIPLE_SINGLE
            else:
                opener, quote, state = d, '"""', self._IN_TRIPLE_DOUBLE
            close = text.find(quote, opener + 3)
            if close == -1:                        # opens here, runs past end of line
                self.setFormat(opener, len(text) - opener, self._string)
                self.setCurrentBlockState(state)
                return
            self.setFormat(opener, close + 3 - opener, self._string)
            start = close + 3
