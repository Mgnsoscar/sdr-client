"""
CalibrationPanel — the Calibration sub-tab of the unit detail view.

Shows this unit's power calibration (whether it's calibrated + a resolved per-signal
summary) and lets you edit `calibration.json` two ways:

  • Editor  — forms: chain gain limits, the safety/regulatory limits list, and a
              per-(signal × measured-plane) CURVE GRID for entering measured points.
  • JSON    — the raw document (source of truth for the plane topology and anything
              the forms don't cover).

Both views drive one document model (self._doc); switching tabs syncs it. Upload or
Save sends the document to the agent, which VALIDATES it (the full resolver checks,
docs/calibration.md §9.2) before storing — so a bad curve is rejected with the
agent's exact reason, never at transmit.

Network calls go through the DataHub run_async / task_done pattern, filtered to this
host + ops:
    cal_get:<host>   GET /calibration → {unit_type, document, valid, signals|error}
    cal_save:<host>  POST /files (calibration.json) → {saved, calibration:{…}} | raises
"""
from __future__ import annotations

import copy
import json
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from api.client import AgentHTTPError
from .theme import Palette

CAL_NAME = "calibration.json"

# When the unit is simply uncalibrated, the /calibration route answers 404 with this
# detail. A generic "Not Found" 404 instead means the route itself is missing — i.e.
# the agent deployed on the unit predates the calibration endpoints and must be updated.
_NO_CAL_DETAIL = "no calibration document"
_OUTDATED_AGENT_MSG = (
    "this unit's agent is out of date — it has no calibration endpoint. "
    "Open the unit's ••• menu → “Update agent…”, then Refresh here.")


def _is_outdated_agent(err) -> bool:
    """A 404 that is NOT the agent's own 'not calibrated' answer ⇒ the route is absent
    ⇒ the deployed agent predates the calibration/files endpoints."""
    return (isinstance(err, AgentHTTPError) and err.status_code == 404
            and _NO_CAL_DETAIL not in (err.detail or "").lower())


def _fmt_range(lo, hi, unit: str) -> str:
    if lo is None or hi is None:
        return "—"
    return f"{lo:g} – {hi:g} {unit}"


def _numstr(x) -> str:
    """Format a JSON number for a text field: drop the trailing .0 on integers."""
    if isinstance(x, bool) or x is None:
        return ""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _to_float(s: str, field: str) -> float:
    s = (s or "").strip()
    if s == "":
        raise ValueError(f"{field} is empty")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"{field}: '{s}' is not a number")


def _template() -> dict:
    """A minimal, valid starting document (broadcaster, one measured plane)."""
    return {
        "schema_version": 1, "unit_id": "", "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB input"}],
            "planes": {
                "sdr_output": {"type": "measured", "quantity": "total in-band power"},
            },
        },
        "defaults": {"amplitude": 0.8},
        "signals": {
            "mock": {"amplitude": 0.8, "curves": {
                "sdr_output": {"points": [
                    {"gain_db": 40, "power_dbm": -36}, {"gain_db": 74, "power_dbm": -2.5}]}}},
        },
    }


# ── A small editable (gain, power) grid ─────────────────────────────────────────

class _CurveTable(QTableWidget):
    def __init__(self):
        super().__init__(0, 2)
        self.setHorizontalHeaderLabels(["gain (dB)", "power (dBm)"])
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Grow with the rows (up to a cap) so added points are always visible, rather
        # than hiding them behind an inner scrollbar in a fixed-height box.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip("Each row is one measured point: the SDR gain you set and the "
                        "power you measured on this plane. Enter at least two points, "
                        "with strictly increasing gain; power is interpolated between them.")
        self._fit_height()

    def _fit_height(self) -> None:
        """Size the table to its rows (with a sensible min and max), so it expands as
        points are added instead of scrolling inside a squat box."""
        header = self.horizontalHeader().height()
        row_h = self.verticalHeader().defaultSectionSize()
        rows = max(self.rowCount(), 1)
        wanted = header + rows * row_h + 2 * self.frameWidth() + 2
        self.setMinimumHeight(min(wanted, header + 3 * row_h))   # show ~3 rows before scrolling
        self.setMaximumHeight(min(wanted, header + 12 * row_h))  # cap tall grids

    def set_points(self, points) -> None:
        self.setRowCount(0)
        for pt in points or []:
            self._append(_numstr(pt.get("gain_db")), _numstr(pt.get("power_dbm")))
        self._fit_height()

    def _append(self, g="", p="") -> None:
        r = self.rowCount()
        self.insertRow(r)
        self.setItem(r, 0, QTableWidgetItem(g))
        self.setItem(r, 1, QTableWidgetItem(p))

    def add_blank_row(self) -> None:
        self._append()
        self._fit_height()

    def remove_selected(self) -> None:
        for r in sorted({i.row() for i in self.selectedItems()}, reverse=True):
            self.removeRow(r)
        self._fit_height()

    def points(self, strict: bool):
        """Return [{gain_db, power_dbm}], skipping fully-blank rows. strict=True raises
        on a partially-filled or non-numeric row; strict=False skips it."""
        out = []
        for r in range(self.rowCount()):
            g = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            p = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            if not g and not p:
                continue
            try:
                out.append({"gain_db": _to_float(g, f"row {r+1} gain"),
                            "power_dbm": _to_float(p, f"row {r+1} power")})
            except ValueError:
                if strict:
                    raise
        return out


class CalibrationPanel(QWidget):
    def __init__(self, hostname: str, hub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._doc: Optional[dict] = None       # the working document model
        self._f: dict = {}                      # references to editor widgets
        self._prev_tab = 0
        self._build()
        self.hub.task_done.connect(self._on_task_done)

    # ── layout ──────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._refresh_btn = QPushButton("Refresh"); self._refresh_btn.clicked.connect(self._refresh)
        self._upload_btn = QPushButton("Upload…"); self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        self._save_btn = QPushButton("Save"); self._save_btn.clicked.connect(self._on_save)
        self._download_btn = QPushButton("Download…"); self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)
        for b in (self._refresh_btn, self._upload_btn, self._save_btn, self._download_btn):
            row.addWidget(b)
        row.addStretch(1)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._status)

        # Resolved per-signal summary (read-only, reflects the last validated load)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["Signal", "Operating plane", "Quantity", "Gain", "Power"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setMaximumHeight(150)
        outer.addWidget(self._table)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_editor_tab(), "Editor")
        self._tabs.addTab(self._build_json_tab(), "JSON (advanced)")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self._tabs, stretch=1)

    def _build_editor_tab(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        inner = QWidget(); self._editor_layout = QVBoxLayout(inner)
        self._editor_layout.setSpacing(10)

        intro = QLabel(
            "Calibration teaches this unit what an absolute power (dBm) means for its "
            "own hardware. You describe the RF chain as a series of <b>planes</b> "
            "(SDR output → amplifier → cable → antenna), give the SDR-gain limits, add "
            "safety ceilings, and enter the <b>measured gain→power points</b> per signal. "
            "The unit interpolates those points to convert a requested power into an SDR "
            "gain. Hover any field for details.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        self._editor_layout.addWidget(intro)

        # Chain gain limits + operating plane
        gl_box = QGroupBox("Chain")
        gl_box.setToolTip("The SDR's usable internal-gain range and which plane an "
                          "absolute --power figure refers to.")
        gl_form = QFormLayout(gl_box)
        self._f["min_gain"] = QLineEdit(); self._f["max_gain"] = QLineEdit()
        self._f["min_gain"].setToolTip(
            "Lowest SDR internal gain usable on this chain, in dB. Powers that would "
            "need less gain than this are out of range (too quiet).")
        self._f["max_gain"].setToolTip(
            "Highest SDR internal gain the safety ceilings allow — usually the gain at "
            "which the amplifier hits its P1dB input. This is the hard upper stop.")
        self._f["operating"] = QComboBox()
        self._f["operating"].setToolTip(
            "The plane an absolute --power value is measured at — where you care about the "
            "delivered power, e.g. EIRP at the antenna. Pick the last plane in the chain.")
        gl_form.addRow("Min gain (dB)", self._f["min_gain"])
        gl_form.addRow("Max gain (dB)", self._f["max_gain"])
        gl_form.addRow("Operating plane", self._f["operating"])
        self._editor_layout.addWidget(gl_box)

        # Safety / regulatory limits
        lim_box = QGroupBox("Safety / regulatory limits  (tightest wins)")
        lim_box.setToolTip(
            "Hard ceilings on power at a given plane (amplifier P1dB, a licence EIRP cap, …). "
            "Each is inverted through the chain to an SDR-gain cap; the tightest one wins. "
            "At least one is required — with no ceiling the unit refuses to transmit.")
        lim_v = QVBoxLayout(lim_box)
        lim_help = QLabel(
            "Each row: the plane the ceiling applies to, the max power (dBm) allowed "
            "there, and a short reason.")
        lim_help.setWordWrap(True)
        lim_help.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        lim_v.addWidget(lim_help)
        self._limits_box = QVBoxLayout(); self._limits_box.setSpacing(4)
        lim_v.addLayout(self._limits_box)
        add_lim = QPushButton("+ Add limit"); add_lim.clicked.connect(lambda: self._add_limit_row())
        lim_v.addWidget(add_lim, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addWidget(lim_box)

        # Planes (RF chain topology)
        pl_box = QGroupBox("Planes — the RF chain (SDR → amp → cable → antenna)")
        pl_box.setToolTip(
            "Each point in the chain where you reason about power. A 'measured' plane has "
            "its own gain→power curve (below); a 'derived' plane is another plane plus a "
            "fixed offset — cable loss (negative Δ) or antenna gain (positive Δ).")
        pl_v = QVBoxLayout(pl_box)
        pl_help = QLabel(
            "Per row: a name, the type (measured = you took readings here; derived = "
            "parent plane + a fixed Δ dB), and a short quantity label (e.g. “total in-band "
            "power”, “main-lobe EIRP”).")
        pl_help.setWordWrap(True)
        pl_help.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        pl_v.addWidget(pl_help)
        self._planes_box = QVBoxLayout(); self._planes_box.setSpacing(4)
        pl_v.addLayout(self._planes_box)
        add_pl = QPushButton("+ Add plane"); add_pl.clicked.connect(self._on_add_plane)
        pl_v.addWidget(add_pl, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addWidget(pl_box)

        # Signals + curve grids
        sig_box = QGroupBox("Signals — measured curves")
        sig_box.setToolTip(
            "For each signal this unit transmits: its baseband amplitude, occupied "
            "bandwidth, and the measured gain→power points on each measured plane.")
        sig_v = QVBoxLayout(sig_box)
        sig_help = QLabel(
            "Enter the points you actually measured. Two or more per measured plane, with "
            "strictly increasing gain — the unit interpolates between them and refuses "
            "powers outside their range.")
        sig_help.setWordWrap(True)
        sig_help.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        sig_v.addWidget(sig_help)
        self._signals_box = QVBoxLayout(); self._signals_box.setSpacing(8)
        sig_v.addLayout(self._signals_box)
        add_sig = QPushButton("+ Add signal…"); add_sig.clicked.connect(self._on_add_signal)
        sig_v.addWidget(add_sig, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addWidget(sig_box)

        # Empty-state hint / template button (shown when there's no document)
        self._empty_hint = QPushButton("New from broadcaster template")
        self._empty_hint.clicked.connect(self._on_new_template)
        self._editor_layout.addWidget(self._empty_hint, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addStretch(1)

        scroll.setWidget(inner)
        return scroll

    def _build_json_tab(self) -> QWidget:
        self._view = QPlainTextEdit()
        self._view.setFont(QFont("monospace"))
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        return self._view

    # ── model → views ────────────────────────────────────────────────────────────
    def _set_doc(self, doc: Optional[dict]) -> None:
        self._doc = doc
        self._download_btn.setEnabled(doc is not None)
        self._doc_to_json()
        self._doc_to_form()

    def _doc_to_json(self) -> None:
        self._view.setPlainText(json.dumps(self._doc, indent=2) if self._doc is not None else "")

    def _plane_names(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return list(planes.keys())

    def _measured_planes(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return [n for n, p in planes.items() if isinstance(p, dict) and p.get("type") == "measured"]

    def _doc_to_form(self) -> None:
        doc = self._doc
        have = doc is not None
        self._empty_hint.setVisible(not have)
        chain = (doc or {}).get("chain") or {}
        gl = chain.get("gain_limits") or {}
        self._f["min_gain"].setText(_numstr(gl.get("min_gain_db")))
        self._f["max_gain"].setText(_numstr(gl.get("max_gain_db")))

        # planes (topology)
        self._clear_layout(self._planes_box)
        self._f["planes"] = []
        for pname, spec in (chain.get("planes") or {}).items():
            self._add_plane_row(pname, spec or {})

        names = self._plane_names()
        self._f["operating"].clear()
        self._f["operating"].addItems(names)
        op = chain.get("operating_plane")
        if op in names:
            self._f["operating"].setCurrentText(op)

        # limits
        self._clear_layout(self._limits_box)
        self._f["limits"] = []
        for lim in (chain.get("limits") or []):
            self._add_limit_row(lim)

        # signals
        self._clear_layout(self._signals_box)
        self._f["signals"] = {}
        measured = self._measured_planes()
        for sid, sig in ((doc or {}).get("signals") or {}).items():
            self._add_signal_widget(sid, sig or {}, measured)

    def _add_limit_row(self, lim: Optional[dict] = None) -> None:
        lim = lim or {}
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        plane = QComboBox(); plane.addItems(self._plane_names())
        plane.setToolTip("The plane this ceiling is measured at.")
        if lim.get("plane") in self._plane_names():
            plane.setCurrentText(lim["plane"])
        max_dbm = QLineEdit(_numstr(lim.get("max_dbm"))); max_dbm.setPlaceholderText("max dBm")
        max_dbm.setToolTip("Maximum power (dBm) permitted at that plane.")
        reason = QLineEdit(lim.get("reason", "")); reason.setPlaceholderText("reason (optional)")
        reason.setToolTip("Why this ceiling exists — e.g. “amp P1dB input”, "
                          "“licence EIRP cap”. Shown for context only.")
        rm = QPushButton("✕"); rm.setFixedWidth(28)
        for wdg, s in ((plane, 2), (max_dbm, 1), (reason, 3)):
            h.addWidget(wdg, s)
        h.addWidget(rm)
        row = {"w": w, "plane": plane, "max": max_dbm, "reason": reason}
        rm.clicked.connect(lambda: self._remove_row(self._limits_box, self._f["limits"], row))
        self._limits_box.addWidget(w)
        self._f["limits"].append(row)

    def _on_add_plane(self) -> None:
        try:
            self._sync_from(self._tabs.currentIndex(), strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = _template()
        planes = self._doc.setdefault("chain", {}).setdefault("planes", {})
        nm, i = "plane", 1
        while nm in planes:
            i += 1; nm = f"plane{i}"
        planes[nm] = {"type": "measured", "quantity": ""}
        self._download_btn.setEnabled(True)
        self._doc_to_form()
        self._tabs.setCurrentIndex(0)

    def _add_plane_row(self, name: str = "", spec: Optional[dict] = None) -> None:
        spec = spec or {}
        w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(4)
        name_e = QLineEdit(name); name_e.setPlaceholderText("name")
        name_e.setToolTip("A short id for this plane, e.g. sdr_output, amp_output, antenna.")
        type_c = QComboBox(); type_c.addItems(["measured", "derived"])
        type_c.setToolTip("measured = you took gain→power readings at this plane. "
                          "derived = this plane is another plane plus a fixed Δ dB "
                          "(cable loss / antenna gain), no readings of its own.")
        type_c.setCurrentText(spec.get("type", "measured"))
        from_lbl = QLabel("from")
        from_c = QComboBox(); from_c.addItems([n for n in self._plane_names() if n != name])
        from_c.setToolTip("The parent plane this derived plane is offset from.")
        if spec.get("from") in self._plane_names():
            from_c.setCurrentText(spec["from"])
        delta_e = QLineEdit(_numstr(spec.get("delta_db"))); delta_e.setPlaceholderText("Δ dB")
        delta_e.setToolTip("Offset from the parent plane, in dB. Negative for a loss "
                           "(cable), positive for a gain (antenna).")
        delta_e.setFixedWidth(72)
        quantity_e = QLineEdit(spec.get("quantity", "")); quantity_e.setPlaceholderText("quantity label")
        quantity_e.setToolTip("A short label for what power means here — e.g. “total "
                              "in-band power”, “main-lobe EIRP”. Keep it brief; it's a "
                              "label, not a description.")
        rm = QPushButton("✕"); rm.setFixedWidth(28)
        h.addWidget(name_e, 2); h.addWidget(type_c, 1)
        h.addWidget(from_lbl); h.addWidget(from_c, 2); h.addWidget(delta_e)
        h.addWidget(quantity_e, 2); h.addWidget(rm)
        row = {"w": w, "name": name_e, "type": type_c, "from": from_c,
               "delta": delta_e, "quantity": quantity_e, "from_lbl": from_lbl}

        derived = type_c.currentText() == "derived"
        for wdg in (from_lbl, from_c, delta_e):
            wdg.setVisible(derived)
        # A type/name change reshapes dependents (which planes are 'measured', the
        # from/operating/limit dropdowns), so rebuild the form from the widgets.
        type_c.currentTextChanged.connect(lambda _=None: self._refresh_form_from_widgets())
        name_e.editingFinished.connect(self._refresh_form_from_widgets)
        rm.clicked.connect(lambda: self._remove_plane(row))
        self._planes_box.addWidget(w)
        self._f["planes"].append(row)

    def _read_planes(self, strict: bool) -> dict:
        prev = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        planes: dict = {}
        for row in self._f.get("planes", []):
            name = row["name"].text().strip()
            if not name:
                continue
            if row["type"].currentText() == "measured":
                p = {"type": "measured"}
            else:
                p = {"type": "derived", "from": row["from"].currentText()}
                d = row["delta"].text().strip()
                if d:
                    p["delta_db"] = _to_float(d, f"plane '{name}' Δ dB")
                elif strict:
                    raise ValueError(f"derived plane '{name}' has no Δ dB")
                else:
                    p["delta_db"] = 0.0
            if row["quantity"].text().strip():
                p["quantity"] = row["quantity"].text().strip()
            if isinstance(prev.get(name), dict) and prev[name].get("description"):
                p["description"] = prev[name]["description"]
            planes[name] = p
        return planes

    def _remove_plane(self, row) -> None:
        try:
            self._sync_from(0, strict=False)
        except ValueError:
            pass
        name = row["name"].text().strip()
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        planes.pop(name, None)
        self._doc_to_form()

    def _refresh_form_from_widgets(self) -> None:
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            return
        self._doc_to_form()

    def _add_signal_widget(self, sid: str, sig: dict, measured) -> None:
        box = QGroupBox(f"signal: {sid}")
        v = QVBoxLayout(box)
        top = QFormLayout()
        amp = QLineEdit(_numstr(sig.get("amplitude")))
        amp.setToolTip("The baseband amplitude (0–1) the script drives for this signal. "
                       "It must match the amplitude used while measuring the curve below, "
                       "since power scales with it.")
        bw = QLineEdit(_numstr(sig.get("occupied_bw_hz")))
        bw.setToolTip("Occupied bandwidth of the signal in Hz (optional) — used to relate "
                      "total in-band power to spectral density.")
        top.addRow("Amplitude (0–1)", amp)
        top.addRow("Occupied BW (Hz)", bw)
        v.addLayout(top)

        curves = {}
        for plane in measured:
            v.addWidget(QLabel(f"curve · {plane}"))
            tbl = _CurveTable()
            tbl.set_points(((sig.get("curves") or {}).get(plane) or {}).get("points"))
            v.addWidget(tbl)
            btns = QHBoxLayout()
            addp = QPushButton("+ point"); addp.clicked.connect(tbl.add_blank_row)
            rmp = QPushButton("− point"); rmp.clicked.connect(tbl.remove_selected)
            btns.addWidget(addp); btns.addWidget(rmp); btns.addStretch(1)
            v.addLayout(btns)
            curves[plane] = tbl

        rm = QPushButton("Remove signal")
        entry = {"w": box, "amp": amp, "bw": bw, "curves": curves}
        rm.clicked.connect(lambda: self._remove_signal(sid))
        v.addWidget(rm, alignment=Qt.AlignmentFlag.AlignRight)
        self._signals_box.addWidget(box)
        self._f["signals"][sid] = entry

    # ── views → model ─────────────────────────────────────────────────────────────
    def _read_form(self, strict: bool) -> dict:
        """Rebuild the document from the editor widgets, preserving fields the form
        doesn't model (schema_version, unit_id, meta, chain.planes, interp/offset_db).
        strict=True raises ValueError on bad numeric input."""
        doc = copy.deepcopy(self._doc) if self._doc else _template()
        chain = doc.setdefault("chain", {})
        gl = chain.setdefault("gain_limits", {})
        self._set_num(gl, "min_gain_db", self._f["min_gain"].text(), "min gain", strict)
        self._set_num(gl, "max_gain_db", self._f["max_gain"].text(), "max gain", strict)
        if self._f["operating"].currentText():
            chain["operating_plane"] = self._f["operating"].currentText()
        chain["planes"] = self._read_planes(strict)

        limits = []
        for row in self._f.get("limits", []):
            mx = row["max"].text().strip()
            if not mx:
                if strict:
                    raise ValueError(f"limit on '{row['plane'].currentText()}' has no max dBm")
                continue
            lim = {"plane": row["plane"].currentText(),
                   "max_dbm": _to_float(mx, "limit max dBm")}
            if row["reason"].text().strip():
                lim["reason"] = row["reason"].text().strip()
            limits.append(lim)
        chain["limits"] = limits

        signals = {}
        prev_sigs = (self._doc or {}).get("signals") or {}
        for sid, w in self._f.get("signals", {}).items():
            sig = {}
            if w["amp"].text().strip():
                sig["amplitude"] = _to_float(w["amp"].text(), f"{sid} amplitude")
            if w["bw"].text().strip():
                sig["occupied_bw_hz"] = _to_float(w["bw"].text(), f"{sid} occupied BW")
            curves = {}
            for plane, tbl in w["curves"].items():
                pts = tbl.points(strict)
                if not pts:
                    continue
                entry = {"points": pts}
                prev = ((prev_sigs.get(sid) or {}).get("curves") or {}).get(plane) or {}
                for k in ("interp", "offset_db"):     # preserve fields the form doesn't edit
                    if k in prev:
                        entry[k] = prev[k]
                curves[plane] = entry
            sig["curves"] = curves
            signals[sid] = sig
        doc["signals"] = signals
        return doc

    @staticmethod
    def _set_num(d: dict, key: str, text: str, field: str, strict: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        try:
            d[key] = _to_float(text, field)
        except ValueError:
            if strict:
                raise

    def _json_to_doc(self, strict: bool) -> None:
        text = self._view.toPlainText().strip()
        if not text:
            if strict:
                raise ValueError("document is empty")
            return
        try:
            self._doc = json.loads(text)
        except ValueError:
            if strict:
                raise

    def _sync_from(self, tab_index: int, strict: bool) -> None:
        """Pull the given tab's contents into self._doc."""
        if tab_index == 0:                       # Editor
            self._doc = self._read_form(strict)
        else:                                     # JSON
            self._json_to_doc(strict)

    def _on_tab_changed(self, idx: int) -> None:
        # Sync the tab we're leaving into the model (best-effort), then repaint the
        # tab we're entering from the model.
        try:
            self._sync_from(self._prev_tab, strict=False)
        except ValueError:
            pass
        if idx == 0:
            self._doc_to_form()
        else:
            self._doc_to_json()
        self._prev_tab = idx

    # ── refresh / load ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"cal_get:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )

    def _on_new_template(self) -> None:
        self._set_doc(_template())
        self._set_status("template loaded — edit, then Save", kind="warn")

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
        try:
            self._sync_from(self._tabs.currentIndex(), strict=True)
        except ValueError as exc:
            self._set_status(f"cannot save: {exc}", kind="error")
            return
        if self._doc is None:
            self._set_status("nothing to save", kind="error")
            return
        self._send(json.dumps(self._doc).encode("utf-8"))

    def _send(self, content: bytes) -> None:
        self._set_status("validating + saving…")
        self.hub.run_async(
            f"cal_save:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).upload_file(CAL_NAME, content),
        )

    def _on_download(self) -> None:
        try:
            self._sync_from(self._tabs.currentIndex(), strict=False)
        except ValueError:
            pass
        if self._doc is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save calibration.json", CAL_NAME, "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._doc, fh, indent=2)
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
        if parts[0] == "cal_get":
            self._handle_get(result)
        elif parts[0] == "cal_save":
            self._handle_save(result)

    def _handle_get(self, result) -> None:
        if _is_outdated_agent(result):
            self._set_doc(None)
            self._table.setRowCount(0)
            self._set_status(_OUTDATED_AGENT_MSG, kind="error")
            return
        if isinstance(result, AgentHTTPError) and result.status_code == 404:
            self._set_doc(None)
            self._table.setRowCount(0)
            self._set_status("not calibrated — start from a template or Upload…", kind="faint")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        if not isinstance(result, dict):
            self._set_status("unexpected response", kind="error")
            return
        self._set_doc(result.get("document"))
        utype = result.get("unit_type") or "—"
        if result.get("valid"):
            from state.calibration_cache import get_calibration_cache
            get_calibration_cache().put(self.hostname, result)   # remember for offline
            self._populate_table(result.get("signals") or {})
            n = len(result.get("signals") or {})
            self._set_status(f"calibrated ✓  ·  type {utype}  ·  {n} signal(s) resolve", kind="ok")
        else:
            self._table.setRowCount(0)
            self._set_status(f"stored document is INVALID: {result.get('error', '')}", kind="error")

    def _handle_save(self, result) -> None:
        if _is_outdated_agent(result):
            self._set_status(_OUTDATED_AGENT_MSG, kind="error")
            QMessageBox.warning(self, "Agent out of date",
                                "This unit's agent has no file-upload endpoint, so the "
                                "calibration could not be saved.\n\nUpdate the agent "
                                "(unit ••• menu → “Update agent…”), then try again.")
            return
        if isinstance(result, AgentHTTPError) and result.status_code == 400:
            self._set_status("rejected — not saved", kind="error")
            QMessageBox.warning(self, "Calibration rejected",
                                f"The unit rejected this calibration and did not store it:"
                                f"\n\n{result.detail}")
            return
        if isinstance(result, Exception):
            self._set_status(f"error: {result}", kind="error")
            return
        summary = result.get("calibration") if isinstance(result, dict) else None
        n = len(summary) if isinstance(summary, dict) else 0
        self._set_status(f"saved ✓  ·  {n} signal(s) valid", kind="ok")
        self._refresh()

    # ── helpers ─────────────────────────────────────────────────────────────────
    def _on_add_signal(self) -> None:
        sid, ok = QInputDialog.getText(self, "Add signal", "Signal id (e.g. gps_l1_mcode):")
        sid = (sid or "").strip()
        if not ok or not sid:
            return
        # sync current edits, then add an empty signal for each measured plane
        try:
            self._sync_from(self._tabs.currentIndex(), strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = _template()
        self._doc.setdefault("signals", {})[sid] = {"curves": {}}
        self._download_btn.setEnabled(True)
        self._doc_to_form()
        self._tabs.setCurrentIndex(0)

    def _remove_signal(self, sid: str) -> None:
        try:
            self._sync_from(0, strict=False)
        except ValueError:
            pass
        if self._doc and sid in (self._doc.get("signals") or {}):
            del self._doc["signals"][sid]
        self._doc_to_form()

    def _remove_row(self, layout, registry: list, row: dict) -> None:
        registry.remove(row)
        row["w"].setParent(None)
        row["w"].deleteLater()

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _populate_table(self, signals: dict) -> None:
        self._table.setRowCount(len(signals))
        for r, (sig, info) in enumerate(sorted(signals.items())):
            cells = [sig, info.get("operating_plane", ""), info.get("quantity", ""),
                     _fmt_range(info.get("min_gain_db"), info.get("max_gain_db"), "dB"),
                     _fmt_range(info.get("min_power_dbm"), info.get("max_power_dbm"), "dBm")]
            for c, text in enumerate(cells):
                self._table.setItem(r, c, QTableWidgetItem(str(text)))

    def _set_status(self, text: str, kind: str = "muted") -> None:
        color = {"ok": Palette.ONLINE, "warn": Palette.ARMED, "error": Palette.CRASH,
                 "faint": Palette.TEXT_FAINT, "muted": Palette.TEXT_MUTED}.get(kind, Palette.TEXT_MUTED)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 12px; color: {color};")
