"""
UnitDialog — add or edit a unit's identity, addresses, and API key.

A unit is identified by a stable label (the fleet key, used in plans/drift/UI),
separate from the one-or-more addresses the client tries to reach it at. Listing
every address a unit can have (home wifi IP, work ethernet IP, an mDNS .local
name) lets the client connect from anywhere without editing anything — it probes
them in order and uses the first that answers.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
    QVBoxLayout,
)

from config import UnitEntry
from .theme import Palette


class UnitDialog(QDialog):
    def __init__(self, existing: Optional[UnitEntry] = None,
                 taken_labels: Optional[set] = None, parent=None):
        super().__init__(parent)
        self._existing = existing
        self._taken = {l.lower() for l in (taken_labels or set())}
        # When editing, the unit's own label is allowed to stay the same.
        if existing is not None:
            self._taken.discard(existing.label.lower())
        self.result_entry: Optional[UnitEntry] = None

        self.setWindowTitle("Edit unit" if existing else "Add unit")
        self.setMinimumWidth(460)
        self._build()
        if existing is not None:
            self._label.setText(existing.label)
            self._addrs.setPlainText("\n".join(existing.addresses))
            self._key.setText(existing.api_key)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

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
        )
        self.accept()
