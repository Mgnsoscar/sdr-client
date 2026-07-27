"""
UnitsTab — the Units dashboard.

A responsive grid of compact UnitCards, one per unit in the fleet. Consumes the
DataHub's fast_update (health/system/tasks), slow_update (sdr), and stream_status
(live SSE connection) signals to keep each card current.

Clicking a card drills into that unit's detail view. The tab uses a QStackedWidget
internally: page 0 is the grid, page 1 is the detail view (with a back button).
For this first pass the detail view is a placeholder; it's built next.
"""
from __future__ import annotations

from typing import Dict

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from .theme import Palette
from .unit_card import UnitCard
from .unit_detail import UnitDetail
from .qt_adapter import DataHub


class UnitsTab(QWidget):
    # Number of cards per row in the grid.
    COLUMNS = 3

    def __init__(self, fleet: Fleet, hub: DataHub, parent=None):
        super().__init__(parent)
        self.fleet = fleet
        self.hub = hub
        self._cards: Dict[str, UnitCard] = {}

        self._stack = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        self._grid_page = self._build_grid_page()
        self._detail = UnitDetail(fleet, hub, on_back=self._show_grid)
        self._stack.addWidget(self._grid_page)   # index 0
        self._stack.addWidget(self._detail)      # index 1

    # ── Grid page ──────────────────────────────────────────────────────────────

    def _build_grid_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # Header row
        header = QHBoxLayout()
        title = QLabel("Units")
        title.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {Palette.TEXT};")
        header.addWidget(title)
        header.addStretch(1)
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
        header.addWidget(self._summary)
        lay.addLayout(header)

        # Scrollable card grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        grid_host = QWidget()
        self._grid = QGridLayout(grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(12)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        # Create a card per unit, keyed by stable hostname (matches snapshot keys).
        for i, client in enumerate(self.fleet.units()):
            card = UnitCard(client.hostname, display_name=client.unit_id)
            card.clicked.connect(self._on_card_clicked)
            self._cards[client.hostname] = card
            self._grid.addWidget(card, i // self.COLUMNS, i % self.COLUMNS)

        scroll.setWidget(grid_host)
        lay.addWidget(scroll, stretch=1)

        self._update_summary()
        return page

    def _update_summary(self) -> None:
        total = len(self._cards)
        online = sum(1 for c in self._cards.values()
                     if c._conn.text() == "online")  # simple read of current state
        self._summary.setText(f"{online}/{total} online")

    # ── Navigation ───────────────────────────────────────────────────────────

    def _on_card_clicked(self, hostname: str) -> None:
        self._detail.set_unit(hostname)
        self._stack.setCurrentIndex(1)

    def _show_grid(self) -> None:
        self._stack.setCurrentIndex(0)

    def _detail_is_open(self) -> bool:
        return self._stack.currentIndex() == 1

    # ── Data updates (called by MainWindow from hub signals) ────────────────────

    def on_fast_update(self, snap) -> None:
        for hostname, card in self._cards.items():
            sysv = snap.system.get(hostname)
            if isinstance(sysv, m.SystemHealth):
                card.update_system(sysv)
            elif isinstance(sysv, Exception):
                card.set_offline()

            tasksv = snap.tasks.get(hostname)
            if isinstance(tasksv, list):
                card.update_tasks(tasksv)
        self._update_summary()

        # Forward to the open detail view so its task list / status stay live.
        if self._detail_is_open():
            self._detail.on_fast_update(snap)

    def on_slow_update(self, snap) -> None:
        for hostname, card in self._cards.items():
            sdrv = snap.sdr.get(hostname)
            if isinstance(sdrv, m.SdrStatus):
                card.update_sdr(sdrv)

    def on_stream_status(self, hostname: str, connected: bool) -> None:
        card = self._cards.get(hostname)
        if card is not None:
            card.set_connection(connected)
            self._update_summary()
        # Also reflect on the detail header if this unit is open.
        self._detail.on_stream_status(hostname, connected)