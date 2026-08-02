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
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from api import AgentClient, Fleet
from api import models as m
from config import ClientConfig, UnitEntry
from .theme import Palette
from .unit_card import UnitCard
from .unit_detail import UnitDetail
from .unit_dialog import UnitDialog
from .qt_adapter import DataHub


class UnitsTab(QWidget):
    # Number of cards per row in the grid.
    COLUMNS = 3

    def __init__(self, fleet: Fleet, hub: DataHub, parent=None):
        super().__init__(parent)
        self.fleet = fleet
        self.hub = hub
        self._cards: Dict[str, UnitCard] = {}
        # The known-units config (units.yaml). This tab owns edits to it.
        self._cfg = ClientConfig.load()

        self._stack = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        self._grid_page = self._build_grid_page()
        self._detail = UnitDetail(fleet, hub, on_back=self._show_grid,
                                  on_edit=self.edit_unit, on_remove=self.remove_unit)
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
        header.addSpacing(12)
        self._add_btn = QPushButton("Add unit…")
        self._add_btn.setObjectName("primary")
        self._add_btn.setToolTip("Add a unit by name and one or more addresses")
        self._add_btn.clicked.connect(self._on_add)
        header.addWidget(self._add_btn)
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

        scroll.setWidget(grid_host)
        lay.addWidget(scroll, stretch=1)

        self._rebuild_grid()
        return page

    def _rebuild_grid(self) -> None:
        """(Re)create a card per unit from the current fleet."""
        while self._grid.count():
            w = self._grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._cards.clear()

        units = self.fleet.units()
        if not units:
            # A fresh label each rebuild — the loop above deletes whatever was here,
            # so a reused instance would be a use-after-delete (a hard Qt crash).
            empty = QLabel("No units yet. Click “Add unit…” to add one.")
            empty.setStyleSheet(f"font-size: 13px; color: {Palette.TEXT_FAINT};")
            self._grid.addWidget(empty, 0, 0)
        # Card per unit, keyed by stable hostname/label (matches snapshot keys).
        for i, client in enumerate(units):
            card = UnitCard(client.hostname, display_name=client.unit_id)
            card.clicked.connect(self._on_card_clicked)
            self._cards[client.hostname] = card
            self._grid.addWidget(card, i // self.COLUMNS, i % self.COLUMNS)
        self._update_summary()

    # ── Add / edit / remove units ───────────────────────────────────────────────

    def _known_addresses(self) -> set:
        return {a for u in self._cfg.units for a in u.addresses}

    def _on_add(self) -> None:
        dlg = UnitDialog(taken_labels=set(self.fleet.hostnames()),
                         taken_addresses=self._known_addresses(),
                         discovered_provider=self.hub.discovery.discovered,
                         rescan=self.hub.discovery.rescan,
                         parent=self.window())
        if not dlg.exec() or dlg.result_entry is None:
            return
        entry = dlg.result_entry
        self._cfg.units.append(entry)
        self._persist()
        client = AgentClient(entry.label, addresses=entry.addresses, api_key=entry.api_key)
        self.hub.add_unit(client)
        self._rebuild_grid()
        self.hub.run_async(f"warmup:{entry.label}", client.warmup)
        self.hub.refresh_now()

    def edit_unit(self, label: str) -> None:
        entry = next((u for u in self._cfg.units if u.label == label), None)
        if entry is None:
            # Fall back to a live-fleet view if it isn't in the config for some reason.
            try:
                c = self.fleet.get(label)
                entry = UnitEntry(label=label, addresses=c.addresses(), api_key=c.api_key)
            except KeyError:
                return
        dlg = UnitDialog(existing=entry, taken_labels=set(self.fleet.hostnames()),
                         parent=self.window())
        if not dlg.exec() or dlg.result_entry is None:
            return
        new = dlg.result_entry
        # Swap the config entry.
        self._cfg.units = [new if u.label == label else u for u in self._cfg.units]
        if not any(u.label == label for u in self._cfg.units):
            self._cfg.units.append(new)
        self._persist()
        # Replace the live client (a rename changes the fleet key).
        self.hub.remove_unit(label)
        client = AgentClient(new.label, addresses=new.addresses, api_key=new.api_key)
        self.hub.add_unit(client)
        self._show_grid()
        self._rebuild_grid()
        self.hub.run_async(f"warmup:{new.label}", client.warmup)
        self.hub.refresh_now()

    def remove_unit(self, label: str) -> None:
        if QMessageBox.question(
            self, "Remove unit",
            f"Remove '{label}' from this PC?\nThis only forgets the unit here; the "
            f"unit and its broadcasts are untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._cfg.units = [u for u in self._cfg.units if u.label != label]
        self._persist()
        self.hub.remove_unit(label)
        self._show_grid()
        self._rebuild_grid()

    def _persist(self) -> None:
        try:
            self._cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, "Could not save units",
                                f"The unit list couldn't be written to disk:\n{exc}")

    def _update_summary(self) -> None:
        total = len(self._cards)
        online = sum(1 for c in self._cards.values() if c.is_online())
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