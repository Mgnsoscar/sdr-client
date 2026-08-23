"""
no_wheel — stop the mouse wheel from silently changing a drop-down's selection.

Combo boxes live inside long, scrollable forms and dialogs. Qt's default is that a
wheel tick over a combo box changes its selected option — so scrolling the page with
the pointer happening to sit over a combo silently edits it, an easily-missed change.
This app-wide event filter takes the wheel away from every QComboBox: the selection
never changes on scroll, and the tick is handed to the nearest scrolling ancestor
instead, so the page keeps scrolling under the pointer rather than stalling on a
"dead zone" over the control.

It matches only the combo box itself, so an OPEN dropdown's list — a separate popup
view, not a QComboBox — still scrolls normally; you can wheel through a long list of
options once it's open.

Install once, right after the QApplication is created:

    from ui.no_wheel import NoComboWheelFilter
    app.installEventFilter(NoComboWheelFilter(app))   # parent to app so it lives on
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication, QComboBox, QWidget


class NoComboWheelFilter(QObject):
    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.Wheel or not isinstance(obj, QComboBox):
            return False   # not our concern — leave it entirely alone
        # Hand the tick to the nearest scrolling ancestor so the page still moves
        # under the pointer; if there is none, the wheel is simply swallowed.
        area = self._scroll_ancestor(obj)
        if area is not None:
            event.setAccepted(False)
            QApplication.sendEvent(area.viewport(), event)
        return True        # never let the combo act on the wheel

    @staticmethod
    def _scroll_ancestor(w: QWidget) -> Optional[QAbstractScrollArea]:
        p = w.parentWidget()
        while p is not None:
            if isinstance(p, QAbstractScrollArea):
                return p
            p = p.parentWidget()
        return None
