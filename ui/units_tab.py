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

import logging
from typing import Dict

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from api import AgentClient, Fleet
from api import models as m
from config import ClientConfig, UnitEntry
from state import UnitLedger
from .theme import Palette
from .unit_card import UnitCard
from .unit_detail import UnitDetail
from .unit_dialog import UnitDialog
from .qt_adapter import DataHub

logger = logging.getLogger(__name__)


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
        # Permanent machine-id → uid ledger (survives unit deletion), so re-adding
        # the same physical Pi reuses its original id and keeps its plans linked.
        self._ledger = UnitLedger()

        self._stack = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

        self._grid_page = self._build_grid_page()
        self._detail = UnitDetail(fleet, hub, on_back=self._show_grid,
                                  on_edit=self.edit_unit, on_remove=self.remove_unit)
        self._stack.addWidget(self._grid_page)   # index 0
        self._stack.addWidget(self._detail)      # index 1

        # Auto-learn: when a unit is rediscovered at a new address (e.g. moved from
        # wifi to ethernet), add that address to the matching unit by machine-id.
        self.hub.discovery_changed.connect(self._on_discovery_changed)

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
            card = UnitCard(client.hostname, display_name=client.label)
            card.clicked.connect(self._on_card_clicked)
            self._cards[client.hostname] = card
            self._grid.addWidget(card, i // self.COLUMNS, i % self.COLUMNS)
        self._update_summary()

    # ── Add / edit / remove units ───────────────────────────────────────────────

    def _known_addresses(self) -> set:
        return {a for u in self._cfg.units for a in u.addresses}

    def _labels(self) -> set:
        return {u.label for u in self._cfg.units}

    def _make_client(self, entry: UnitEntry) -> AgentClient:
        client = AgentClient(entry.uid, label=entry.label,
                             addresses=entry.addresses, api_key=entry.api_key)
        client.machine_id = entry.machine_id
        return client

    def _machine_ids(self) -> set:
        return {u.machine_id for u in self._cfg.units if u.machine_id}

    def _on_add(self) -> None:
        dlg = UnitDialog(taken_labels=self._labels(),
                         taken_addresses=self._known_addresses(),
                         taken_machine_ids=self._machine_ids(),
                         discovered_provider=self.hub.discovery.discovered,
                         rescan=self.hub.discovery.rescan,
                         parent=self.window())
        if not dlg.exec() or dlg.result_entry is None:
            return
        entry = dlg.result_entry            # carries a fresh permanent uid
        # If we've hosted this physical Pi before (same machine-id, e.g. it was
        # deleted and is now re-added from discovery), give it back its original
        # permanent id so its existing plans/schedule reconnect — instead of the
        # fresh id, which nothing references. A never-seen Pi is recorded now.
        if entry.machine_id:
            known = self._ledger.uid_for(entry.machine_id)
            if known and known != entry.uid and not any(u.uid == known
                                                        for u in self._cfg.units):
                logger.info("Re-adding a previously-known Pi — reusing its "
                            "permanent id %s (plans preserved)", known)
                entry.uid = known
            else:
                self._ledger.record(entry.machine_id, entry.uid)
        self._cfg.units.append(entry)
        self._persist()
        client = self._make_client(entry)
        self.hub.add_unit(client)
        self._rebuild_grid()
        self.hub.run_async(f"warmup:{entry.uid}", client.warmup)
        self.hub.refresh_now(entry.uid)   # just the new unit — don't wait on dead ones

    def edit_unit(self, uid: str) -> None:
        entry = next((u for u in self._cfg.units if u.uid == uid), None)
        if entry is None:
            try:
                c = self.fleet.get(uid)
                entry = UnitEntry(label=c.label, addresses=c.addresses(),
                                  api_key=c.api_key, uid=uid, machine_id=c.machine_id)
            except KeyError:
                return
        dlg = UnitDialog(existing=entry,
                         taken_labels={u.label for u in self._cfg.units if u.uid != uid},
                         parent=self.window())
        if not dlg.exec() or dlg.result_entry is None:
            return
        new = dlg.result_entry
        new.uid = entry.uid                 # permanent id survives a rename
        new.machine_id = entry.machine_id
        self._cfg.units = [new if u.uid == uid else u for u in self._cfg.units]
        self._persist()
        # Replace the live client under the SAME uid key (so plans stay intact),
        # applying the new label / addresses / api key.
        self.hub.remove_unit(uid)
        client = self._make_client(new)
        self.hub.add_unit(client)
        self._show_grid()
        self._rebuild_grid()
        self.hub.run_async(f"warmup:{new.uid}", client.warmup)
        self.hub.refresh_now(new.uid)     # just the edited unit

    def remove_unit(self, uid: str) -> None:
        entry = next((u for u in self._cfg.units if u.uid == uid), None)
        name = entry.label if entry else uid
        if QMessageBox.question(
            self, "Remove unit",
            f"Remove '{name}' from this PC?\nThis only forgets the unit here; the "
            f"unit and its broadcasts are untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._cfg.units = [u for u in self._cfg.units if u.uid != uid]
        self._persist()
        self.hub.remove_unit(uid)
        self._show_grid()
        self._rebuild_grid()

    def _persist(self) -> None:
        try:
            self._cfg.save()
        except OSError as exc:
            QMessageBox.warning(self, "Could not save units",
                                f"The unit list couldn't be written to disk:\n{exc}")

    # ── Machine-id learning + address auto-learn ────────────────────────────────

    def _sync_machine_ids(self) -> None:
        """Persist a unit's machine-id once its live client has learned it from
        /info, so we can recognise the same physical Pi later (even offline), and
        reconcile it with the permanent machine-id → uid ledger."""
        # 1. Learn machine-ids the live clients discovered from /info.
        changed = False
        for u in self._cfg.units:
            try:
                c = self.fleet.get(u.uid)
            except KeyError:
                continue
            if c.machine_id and c.machine_id != u.machine_id:
                u.machine_id = c.machine_id
                changed = True
        if changed:
            self._persist()

        # 2. Reconcile every known machine-id with the ledger: record ones we've
        #    not seen before, and re-key a unit the ledger already ties to an
        #    EARLIER uid. The latter is a manual re-add — the Pi was typed in by
        #    address (so it got a fresh temp uid), and only on connect do we learn
        #    its machine-id and discover it's a Pi whose plans reference the old
        #    uid. Re-keying restores that link.
        rekeys = []
        for u in self._cfg.units:
            if not u.machine_id:
                continue
            known = self._ledger.uid_for(u.machine_id)
            if known is None:
                self._ledger.record(u.machine_id, u.uid)
            elif known != u.uid and not any(o.uid == known for o in self._cfg.units):
                rekeys.append((u.uid, known))
        for old_uid, new_uid in rekeys:
            self._rekey_unit(old_uid, new_uid)
        if rekeys:
            self._show_grid()
            self._rebuild_grid()
            self.hub.refresh_now()

    def _rekey_unit(self, old_uid: str, new_uid: str) -> None:
        """Move a unit from a freshly-minted uid onto its original one (from the
        ledger) so its existing plans and schedule reconnect. Swaps the live fleet
        client under the new key and re-warms it."""
        entry = next((u for u in self._cfg.units if u.uid == old_uid), None)
        if entry is None:
            return
        entry.uid = new_uid
        self._persist()
        self.hub.remove_unit(old_uid)
        client = self._make_client(entry)
        self.hub.add_unit(client)
        self.hub.run_async(f"warmup:{new_uid}", client.warmup)
        logger.info("Recognised '%s' as a previously-known unit — restored its "
                    "permanent id %s so its plans/schedule reconnect", entry.label,
                    new_uid)

    def _on_discovery_changed(self) -> None:
        """A discovered unit whose machine-id matches a known unit but at a NEW
        address → add that address (never remove), and re-warm so it can connect
        over the new link. This is what makes a Pi that moved from wifi to ethernet
        reachable automatically."""
        by_mid = {u.machine_id: u for u in self._cfg.units if u.machine_id}
        if not by_mid:
            return
        learned_for: list = []
        for d in self.hub.discovery.discovered():
            unit = by_mid.get(d.machine_id) if d.machine_id else None
            if unit is None:
                continue
            new_addrs = [a for a in d.suggested_addresses if a not in unit.addresses]
            if not new_addrs:
                continue
            unit.addresses.extend(new_addrs)
            try:
                c = self.fleet.get(unit.uid)
                for a in new_addrs:
                    c.add_address(a)
            except KeyError:
                pass
            learned_for.append(unit)
            logger.info("Auto-learned address(es) %s for '%s'", new_addrs, unit.label)
        if learned_for:
            self._persist()
            for unit in learned_for:      # a new address may bring an offline unit back
                try:
                    c = self.fleet.get(unit.uid)
                    self.hub.run_async(f"warmup:{unit.uid}", c.warmup)
                except KeyError:
                    pass
            self.hub.refresh_now()

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
        # Persist any machine-ids the clients have learned from /info.
        self._sync_machine_ids()

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