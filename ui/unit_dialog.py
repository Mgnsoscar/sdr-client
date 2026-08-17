"""
UnitDialog — add or edit a unit's identity, addresses, and API key.

A unit is identified by a stable label (the fleet key, used in plans/drift/UI),
separate from the one-or-more addresses the client tries to reach it at. Listing
every address a unit can have (home wifi IP, work ethernet IP, an mDNS .local
name) lets the client connect from anywhere without editing anything — it probes
them in order and uses the first that answers.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from config import UnitEntry
from .theme import Palette


class UnitDialog(QDialog):
    def __init__(self, existing: Optional[UnitEntry] = None,
                 taken_labels: Optional[set] = None,
                 discovered_provider: Optional[Callable[[], list]] = None,
                 taken_addresses: Optional[set] = None,
                 taken_machine_ids: Optional[set] = None,
                 rescan: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self._existing = existing
        self._taken = {l.lower() for l in (taken_labels or set())}
        self._taken_addrs = {a.lower() for a in (taken_addresses or set())}
        self._taken_mids = {m for m in (taken_machine_ids or set()) if m}
        # machine-id of a picked discovered unit (so we can fingerprint it on save)
        self._picked_machine_id = existing.machine_id if existing else ""
        # When editing, the unit's own label is allowed to stay the same.
        if existing is not None:
            self._taken.discard(existing.label.lower())
        # Live discovery is only offered when adding (not editing).
        self._discovered_provider = discovered_provider if existing is None else None
        self._rescan = rescan
        self.result_entry: Optional[UnitEntry] = None

        self.setWindowTitle("Edit unit" if existing else "Add unit")
        self.setMinimumWidth(480)
        self._build()
        if existing is not None:
            self._label.setText(existing.label)
            self._addrs.setPlainText("\n".join(existing.addresses))
            self._key.setText(existing.api_key)
        if self._discovered_provider is not None:
            self._refresh_discovered()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        # Discovered-units picker (add mode only).
        if self._discovered_provider is not None:
            row = QHBoxLayout()
            lbl = QLabel("Discovered on the network")
            lbl.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {Palette.TEXT};")
            row.addWidget(lbl)
            row.addStretch(1)
            refresh = QPushButton("Refresh")
            refresh.setFixedWidth(80)
            refresh.clicked.connect(self._on_refresh)
            row.addWidget(refresh)
            outer.addLayout(row)

            self._disc = QListWidget()
            self._disc.setFixedHeight(96)
            self._disc.setToolTip("Units announcing themselves via mDNS. Click one to "
                                  "fill in its name and addresses.")
            self._disc.itemClicked.connect(self._on_pick_discovered)
            outer.addWidget(self._disc)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self._label = QLineEdit()
        self._label.setPlaceholderText("e.g. Broadcaster 1")
        self._label.textChanged.connect(self._revalidate)
        form.addRow("Name *", self._label)

        self._addrs = QPlainTextEdit()
        self._addrs.setPlaceholderText(
            "One address per line, tried in order:\n"
            "broadcaster-1.local\n192.168.1.42\n169.254.61.247")
        self._addrs.setFixedHeight(96)
        self._addrs.textChanged.connect(self._revalidate)
        form.addRow("Addresses *", self._addrs)

        self._key = QLineEdit()
        self._key.setPlaceholderText("optional — only if this unit requires a key")
        form.addRow("API key", self._key)
        outer.addLayout(form)

        hint = QLabel("The name is a stable label; addresses can change freely under "
                      "it. List every address the unit might have — the app uses "
                      "whichever answers first.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(hint)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.CRASH};")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)
        self._revalidate()

    # ── Discovery ────────────────────────────────────────────────────────────

    def _on_refresh(self) -> None:
        # Kick off a fresh mDNS query, then re-read now and again shortly after so
        # units that answer within the next second or two show up.
        if self._rescan is not None:
            try:
                self._rescan()
            except Exception:  # noqa: BLE001
                pass
        self._refresh_discovered()
        # Re-read a few times: mDNS answers land within ~1-2 s, and an active subnet
        # probe (the multicast-filtered-bridge fallback) can take a little longer.
        for delay in (700, 1600, 2800):
            QTimer.singleShot(delay, self._refresh_discovered)

    def _refresh_discovered(self) -> None:
        if self._discovered_provider is None:
            return
        try:
            self._disc.count()   # touch the widget; a delayed timer may fire after
        except RuntimeError:     # the dialog was closed & its C++ object deleted
            return
        try:
            units = self._discovered_provider() or []
        except Exception:  # noqa: BLE001
            units = []
        self._disc.clear()
        new_n = 0
        for u in units:
            addrs = ", ".join(u.suggested_addresses) or "no address"
            already = ((u.machine_id and u.machine_id in self._taken_mids)
                       or u.unit_id.lower() in self._taken
                       or any(a.lower() in self._taken_addrs for a in u.suggested_addresses))
            if already:
                # Still show it (greyed, unselectable) so you can tell discovery
                # is working even when everything's already added.
                item = QListWidgetItem(f"{u.unit_id}   ·   {addrs}   (already added)")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                item = QListWidgetItem(f"{u.unit_id}   ·   {addrs}")
                item.setData(Qt.ItemDataRole.UserRole, u)
                new_n += 1
            self._disc.addItem(item)
        if not units:
            placeholder = QListWidgetItem(
                "No units seen yet — is the unit powered on, on this network, and "
                "running the agent? Click Refresh to re-scan.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._disc.addItem(placeholder)
        elif new_n == 0:
            # Everything discovered is already added — that's a good sign, not an error.
            hint = QListWidgetItem("Every discovered unit is already added.")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self._disc.insertItem(0, hint)

    def _on_pick_discovered(self, item: QListWidgetItem) -> None:
        u = item.data(Qt.ItemDataRole.UserRole)
        if u is None:
            return
        self._label.setText(u.unit_id)
        self._addrs.setPlainText("\n".join(u.suggested_addresses))
        self._picked_machine_id = u.machine_id   # fingerprint, so we recognise it later

    def _parse_addresses(self) -> List[str]:
        out = []
        for line in self._addrs.toPlainText().splitlines():
            a = line.strip()
            if a and a not in out:
                out.append(a)
        return out

    def _current_error(self) -> str:
        label = self._label.text().strip()
        if not label:
            return "a name is required"
        if ":" in label:
            return "the name can't contain a colon"
        if label.lower() in self._taken:
            return f"a unit named '{label}' already exists"
        if not self._parse_addresses():
            return "at least one address is required"
        return ""

    def _revalidate(self) -> None:
        err = self._current_error()
        self._status.setText(err)
        self._buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not err)

    def _on_save(self) -> None:
        if self._current_error():
            return
        self.result_entry = UnitEntry(
            label=self._label.text().strip(),
            addresses=self._parse_addresses(),
            api_key=self._key.text().strip(),
            machine_id=self._picked_machine_id,
        )
        self.accept()
