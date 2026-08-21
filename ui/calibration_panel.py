"""
CalibrationPanel — the Calibration sub-tab of the unit detail view.

Shows this unit's power calibration: whether it's calibrated, what each signal
resolves to (operating plane, quantity, gain/power range), and an editable view of
`calibration.json`. Upload or Save sends the document to the agent, which VALIDATES
it (the full resolver checks, per docs/calibration.md §9.2) before storing — so a
bad curve is rejected here with the agent's exact reason, never at transmit.

All network calls go through the DataHub's run_async (off the GUI thread); results
arrive on the shared task_done signal, filtered here to this host + ops:
    cal_get:<host>        GET /calibration  → {unit_type, document, valid, signals|error}
    cal_save:<host>       POST /files (calibration.json) → {saved, calibration:{…}} | raises
"""
from __future__ import annotations

import json
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from api.client import AgentHTTPError
from .theme import Palette

CAL_NAME = "calibration.json"


def _fmt_range(lo, hi, unit: str) -> str:
    if lo is None or hi is None:
        return "—"
    return f"{lo:g} – {hi:g} {unit}"


class CalibrationPanel(QWidget):
    def __init__(self, hostname: str, hub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._document: Optional[dict] = None    # last-loaded doc (for Download)
        self._clean_text = ""                    # last loaded/saved text (dirty = differs)
        self._dirty = False
        self._loading = False
        self._pending_save_text = ""
        self._build()
        self.hub.task_done.connect(self._on_task_done)

    # ── layout ──────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)

        self._upload_btn = QPushButton("Upload…")
        self._upload_btn.setObjectName("primary")
        self._upload_btn.setToolTip("Replace this unit's calibration.json from a file (validated)")
        self._upload_btn.clicked.connect(self._on_upload)
        row.addWidget(self._upload_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setToolTip("Validate + store the edited document on the unit")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        row.addWidget(self._save_btn)

        self._download_btn = QPushButton("Download…")
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)
        row.addWidget(self._download_btn)
        row.addStretch(1)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._status)

        split = QSplitter(Qt.Orientation.Vertical)

        # Resolved per-signal summary
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Signal", "Operating plane", "Quantity", "Gain", "Power"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        split.addWidget(self._table)

        # Editable JSON document
        self._view = QPlainTextEdit()
        self._view.setFont(QFont("monospace"))
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._view.textChanged.connect(self._on_text_changed)
        split.addWidget(self._view)
        split.setSizes([160, 360])
        outer.addWidget(split, stretch=1)

    # ── refresh / load ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._dirty and not self._confirm_discard():
            return
        self._set_status("loading…")
        self.hub.run_async(
            f"cal_get:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        dirty = self._view.toPlainText() != self._clean_text
        if dirty != self._dirty:
            self._dirty = dirty
            self._save_btn.setEnabled(dirty)
            if dirty:
                self._set_status("unsaved changes", kind="warn")

    # ── actions ───────────────────────────────────────────────────────────────
    def _on_upload(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Upload calibration.json", "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                content = fh.read()
        except OSError as exc:
            self._set_status(f"could not read file: {exc}", kind="error")
            return
        self._send(content)

    def _on_save(self) -> None:
        text = self._view.toPlainText()
        try:
            json.loads(text)                      # fail fast before a round-trip
        except ValueError as exc:
            self._set_status(f"not valid JSON: {exc}", kind="error")
            return
        self._pending_save_text = text
        self._send(text.encode("utf-8"))

    def _send(self, content: bytes) -> None:
        self._set_status("validating + saving…")
        self.hub.run_async(
            f"cal_save:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).upload_file(CAL_NAME, content),
        )

    def _on_download(self) -> None:
        if self._document is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save calibration.json", CAL_NAME, "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._document, fh, indent=2)
        except OSError as exc:
            self._set_status(f"could not save: {exc}", kind="error")
            return
        self._set_status(f"downloaded to {path}")

    # ── results ─────────────────────────────────────────────────────────────────
    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("cal_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "cal_get":
            self._handle_get(result)
        elif op == "cal_save":
            self._handle_save(result)

    def _handle_get(self, result) -> None:
        if isinstance(result, AgentHTTPError) and result.status_code == 404:
            self._document = None
            self._table.setRowCount(0)
            self._download_btn.setEnabled(False)
            self._set_document_text("")
            self._set_status("not calibrated — Upload… a calibration.json to begin",
                              kind="faint")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        if not isinstance(result, dict):
            self._set_status("unexpected response", kind="error")
            return

        self._document = result.get("document")
        self._download_btn.setEnabled(self._document is not None)
        self._set_document_text(
            json.dumps(self._document, indent=2) if self._document is not None else "")

        utype = result.get("unit_type") or "—"
        if result.get("valid"):
            self._populate_table(result.get("signals") or {})
            n = len(result.get("signals") or {})
            self._set_status(f"calibrated ✓  ·  type {utype}  ·  "
                             f"{n} signal(s) resolve", kind="ok")
        else:
            self._table.setRowCount(0)
            self._set_status(f"stored document is INVALID: {result.get('error', '')}",
                             kind="error")

    def _handle_save(self, result) -> None:
        if isinstance(result, AgentHTTPError) and result.status_code == 400:
            # The agent's validate-on-upload rejected it — surface the exact reason.
            self._set_status("rejected — not saved", kind="error")
            QMessageBox.warning(self, "Calibration rejected",
                                f"The unit rejected this calibration and did not "
                                f"store it:\n\n{result.detail}")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        # Success: the edited text (if any) is now the clean baseline; reload canonical.
        if self._pending_save_text:
            self._clean_text = self._pending_save_text
            self._pending_save_text = ""
        self._dirty = False
        self._save_btn.setEnabled(False)
        summary = result.get("calibration") if isinstance(result, dict) else None
        n = len(summary) if isinstance(summary, dict) else 0
        self._set_status(f"saved ✓  ·  {n} signal(s) valid", kind="ok")
        self._refresh()

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _populate_table(self, signals: dict) -> None:
        self._table.setRowCount(len(signals))
        for r, (sig, info) in enumerate(sorted(signals.items())):
            cells = [
                sig,
                info.get("operating_plane", ""),
                info.get("quantity", ""),
                _fmt_range(info.get("min_gain_db"), info.get("max_gain_db"), "dB"),
                _fmt_range(info.get("min_power_dbm"), info.get("max_power_dbm"), "dBm"),
            ]
            for c, text in enumerate(cells):
                self._table.setItem(r, c, QTableWidgetItem(str(text)))

    def _set_document_text(self, text: str) -> None:
        self._loading = True
        self._view.setPlainText(text)
        self._loading = False
        self._clean_text = text
        self._dirty = False
        self._save_btn.setEnabled(False)

    def _confirm_discard(self) -> bool:
        resp = QMessageBox.question(
            self, "Discard changes?",
            "You have unsaved edits to the calibration document. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return resp == QMessageBox.StandardButton.Discard

    def _set_status(self, text: str, kind: str = "muted") -> None:
        color = {
            "ok": Palette.ONLINE,
            "warn": Palette.ARMED,
            "error": Palette.CRASH,
            "faint": Palette.TEXT_FAINT,
            "muted": Palette.TEXT_MUTED,
        }.get(kind, Palette.TEXT_MUTED)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 12px; color: {color};")
