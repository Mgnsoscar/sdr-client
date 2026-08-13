"""
click_focus — commit a text/number field when the user clicks outside it.

Qt commits an edited field on focus-out (fires editingFinished, reformats a
spinbox, validates), but a click on empty/background space doesn't move focus, so
the entry stays uncommitted until the user tabs or clicks another control. This
app-wide event filter closes that gap: on any mouse press on a widget that won't
take focus itself (a label, a panel, a dialog background), it clears focus from
the currently focused input — which commits it.

Install once, right after the QApplication is created:

    from ui.click_focus import ClickFocusFilter
    app.installEventFilter(ClickFocusFilter(app))   # parent to app so it lives on
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QLineEdit, QPlainTextEdit, QTextEdit, QWidget,
)

# The editable widgets whose entry we want committed on an outside click.
_INPUTS = (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)


class ClickFocusFilter(QObject):
    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            self._maybe_commit(obj)
        return False   # never consume the event — this only nudges focus

    def _maybe_commit(self, clicked) -> None:
        # Only widget-level mouse events carry the precise target (the app filter
        # also sees QWindow-level events, which don't identify the clicked widget).
        if not isinstance(clicked, QWidget):
            return
        fw = QApplication.focusWidget()
        if not isinstance(fw, _INPUTS):
            return
        # Clicking the focused field itself (or its internal parts, e.g. a spinbox's
        # line edit or arrows) must keep focus.
        if clicked is fw or fw.isAncestorOf(clicked) or clicked.isAncestorOf(fw):
            return
        # If the clicked widget takes focus on its own (another input, a button),
        # Qt already moves focus and commits the old field — nothing to do here.
        if int(clicked.focusPolicy().value) & int(Qt.FocusPolicy.ClickFocus.value):
            return
        fw.clearFocus()
