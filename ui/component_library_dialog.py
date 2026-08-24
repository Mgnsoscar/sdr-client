"""
ComponentLibraryDialog — author the shared RF component catalog (calibration v2).

Characterize a cable / antenna / pad once: an id, a kind, and a signed
Δ dB-vs-frequency table (a VNA sweep — negative = loss, positive = gain). The
catalog is the client's canonical library (state.ComponentCatalog); a unit's chain
references a component by id, and the catalog is uploaded to each unit so the agent
resolves it. See the agent's docs/calibration-v2.md.

Reuses the calibration curve grid (paste, undo/redo, single in-focus cell) and the
sparkline, so editing a component feels like editing a measured curve.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)

from state import ComponentCatalog, CatalogError, parse_sweep
from state.component_catalog import KINDS
from .calibration_panel import _CurveTable, _Sparkline
from .theme import Palette

_KIND_LABEL = {"cable": "Cable", "antenna": "Antenna", "pad": "Pad"}


class ComponentLibraryDialog(QDialog):
    def __init__(self, catalog: ComponentCatalog, parent=None, select: Optional[str] = None):
        super().__init__(parent)
        self._cat = catalog
        self._current: Optional[str] = None       # id being edited (None = a new one)
        self.renames: dict = {}                    # old id → new id, applied by the caller
        self.setWindowTitle("Component library")
        self.setMinimumSize(720, 460)
        self._build()
        self._reload_list()
        if select and self._select_id(select):
            pass
        elif self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._new()

    # ── layout ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        intro = QLabel("Characterize a cable, antenna or pad once — as a signed "
                       "Δ dB vs frequency table (negative = loss). Every unit reuses it; "
                       "pick it in the unit's hardware chain.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(intro)

        body = QHBoxLayout(); body.setSpacing(14); outer.addLayout(body, 1)

        # left: the list + new/delete
        left = QVBoxLayout(); left.setSpacing(6)
        self._list = QListWidget(); self._list.setFixedWidth(220)
        self._list.currentItemChanged.connect(lambda *_: self._on_select())
        left.addWidget(self._list, 1)
        row = QHBoxLayout()
        newb = QPushButton("New"); newb.clicked.connect(self._new)
        delb = QPushButton("Delete"); delb.setStyleSheet(f"color: {Palette.CRASH};")
        delb.clicked.connect(self._delete)
        # These must never become the dialog's Enter-default (that's Save, below), or
        # pressing Enter in a field would fire them instead of saving.
        newb.setAutoDefault(False); delb.setAutoDefault(False)
        row.addWidget(newb); row.addWidget(delb); row.addStretch(1)
        left.addLayout(row)
        body.addLayout(left)

        # right: the editor
        right = QVBoxLayout(); right.setSpacing(8)
        form = QFormLayout(); form.setSpacing(8)
        self._id = QLineEdit(); self._id.setPlaceholderText("e.g. cable_lmr240_3m_a")
        self._id.setToolTip("The component's stable id, referenced by units' chains. "
                            "Renaming it here re-points this unit's chain automatically.")
        self._kind = QComboBox(); self._kind.setEditable(True)
        self._kind.addItems([_KIND_LABEL[k] for k in KINDS])
        self._kind.setToolTip("A free-text grouping label (cable / antenna / pad / "
                              "anything). It only groups the library — the maths ignores it.")
        self._desc = QLineEdit(); self._desc.setPlaceholderText("optional — e.g. 3 m LMR-240, VNA 2026-08")
        # Enter in any of the header fields saves the component (rather than triggering
        # the dialog's default button — which used to be "New", silently discarding the
        # edit into a fresh blank component).
        self._id.returnPressed.connect(self._save)
        self._desc.returnPressed.connect(self._save)
        if self._kind.lineEdit() is not None:
            self._kind.lineEdit().returnPressed.connect(self._save)
        form.addRow("Id", self._id)
        form.addRow("Kind", self._kind)
        form.addRow("Description", self._desc)
        right.addLayout(form)

        gridrow = QHBoxLayout(); gridrow.setSpacing(10)
        self._table = _CurveTable(on_changed=self._refresh_spark,
                                  headers=("freq (Hz)", "Δ dB"))
        self._table.setToolTip("Each row: a frequency (Hz) and the component's Δ dB there. "
                               "Negative = loss (cable), positive = gain (antenna). One row "
                               "= a constant, frequency-independent value. Paste a VNA sweep "
                               "of \"freq, dB\" rows (Ctrl+V).")
        self._spark = _Sparkline()
        gridrow.addWidget(self._table, 3); gridrow.addWidget(self._spark, 2)
        right.addLayout(gridrow, 1)

        btns = QHBoxLayout()
        addp = QPushButton("+ point"); addp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        addp.clicked.connect(self._table.add_blank_row)
        rmp = QPushButton("− point"); rmp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmp.clicked.connect(self._table.remove_selected)
        paste = QPushButton("Paste VNA sweep…"); paste.clicked.connect(self._paste_sweep)
        paste.setAutoDefault(False)
        addp.setAutoDefault(False); rmp.setAutoDefault(False)
        btns.addWidget(addp); btns.addWidget(rmp); btns.addWidget(paste); btns.addStretch(1)
        self._save_btn = QPushButton("Save component"); self._save_btn.setObjectName("primary")
        self._save_btn.clicked.connect(self._save)
        self._save_btn.setAutoDefault(True); self._save_btn.setDefault(True)
        btns.addWidget(self._save_btn)
        right.addLayout(btns)

        self._status = QLabel(""); self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        right.addWidget(self._status)
        body.addLayout(right, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject); bb.accepted.connect(self.accept)
        outer.addWidget(bb)

    # ── list ────────────────────────────────────────────────────────────────
    def _reload_list(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for cid in self._cat.ids():
            spec = self._cat.get(cid) or {}
            it = QListWidgetItem(f"{cid}")
            it.setData(Qt.ItemDataRole.UserRole, cid)
            it.setToolTip(f"{_KIND_LABEL.get(spec.get('kind'), spec.get('kind'))} · "
                          f"{len(spec.get('delta_db_by_freq') or [])} point(s)")
            self._list.addItem(it)
        self._list.blockSignals(False)

    def _on_select(self) -> None:
        it = self._list.currentItem()
        if it is None:
            return
        self._load(it.data(Qt.ItemDataRole.UserRole))

    def _load(self, cid: str) -> None:
        spec = self._cat.get(cid)
        if spec is None:
            return
        self._current = cid
        self._id.setText(cid); self._id.setEnabled(True)    # editable → rename in place
        self._kind.setCurrentText(_KIND_LABEL.get(spec.get("kind"), spec.get("kind", "")))
        self._desc.setText(spec.get("description", ""))
        self._table.set_rows(spec.get("delta_db_by_freq") or [])
        self._refresh_spark()
        self._set_status("")

    def _new(self) -> None:
        self._current = None
        self._list.blockSignals(True); self._list.setCurrentRow(-1); self._list.blockSignals(False)
        self._id.clear(); self._id.setEnabled(True)
        self._kind.setCurrentIndex(0)
        self._desc.clear()
        self._table.set_rows([])
        self._table.add_blank_row()
        self._refresh_spark()
        self._set_status("new component — enter an id and its Δ dB points")
        self._id.setFocus()

    # ── actions ──────────────────────────────────────────────────────────────
    def _kind_key(self) -> str:
        """The kind as a free-text label — the typed/selected text, lowercased. A known
        label ('Cable') maps back to its key; anything else is used verbatim."""
        text = self._kind.currentText().strip()
        for k, lbl in _KIND_LABEL.items():
            if text.lower() == lbl.lower():
                return k
        return text.lower() or "cable"

    def _paste_sweep(self) -> None:
        from PyQt6.QtWidgets import QApplication
        try:
            table = parse_sweep(QApplication.clipboard().text())
        except CatalogError as exc:
            QMessageBox.information(self, "Paste VNA sweep",
                                    f"Couldn't read a sweep from the clipboard:\n{exc}\n\n"
                                    "Copy rows of \"frequency dB\" (Hz, signed dB) first.")
            return
        self._table.set_rows(table)
        self._refresh_spark()
        self._set_status(f"pasted {len(table)} point(s)")

    def _save(self) -> None:
        cid = self._id.text().strip()
        if not cid:
            self._set_status("enter an id", error=True); return
        old = self._current
        renaming = bool(old) and cid != old
        try:
            table = self._table.rows(strict=True)
            if renaming:
                self._cat.rename(old, cid)          # move the entry (keeps chain order)
            self._cat.put(cid, self._kind_key(), table, description=self._desc.text().strip())
        except (CatalogError, ValueError) as exc:
            self._set_status(f"can't save: {exc}", error=True); return
        if renaming:
            # Collapse rename chains (a→b then b→c ⇒ a→c) so the caller re-points once.
            src = next((k for k, v in self.renames.items() if v == old), old)
            self.renames[src] = cid
        self._current = cid
        self._reload_list()
        self._select_id(cid)
        self._set_status(f"renamed to “{cid}”" if renaming else f"saved “{cid}”")

    def _delete(self) -> None:
        it = self._list.currentItem()
        if it is None:
            return
        cid = it.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(
                self, "Delete component",
                f"Delete “{cid}” from the library?\nUnits still wired to it will fail to "
                "resolve until you pick another.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._cat.remove(cid)
        self._reload_list()
        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._new()

    def _select_id(self, cid: str) -> bool:
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.ItemDataRole.UserRole) == cid:
                self._list.setCurrentRow(i); return True
        return False

    # ── helpers ──────────────────────────────────────────────────────────────
    def _refresh_spark(self) -> None:
        self._spark.set_points(self._table.numeric_points())

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status.setStyleSheet(
            f"font-size: 11px; color: {Palette.CRASH if error else Palette.TEXT_FAINT};")
        self._status.setText(text)
