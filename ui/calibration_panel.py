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
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QApplication, QComboBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from api.client import AgentHTTPError
from api.models import UNIT_TYPES, UNIT_TYPE_LABELS
from .theme import Palette

CAL_NAME = "calibration.json"
CAL_CAPABILITY = "calibration"
CAL_VALIDATE_CAPABILITY = "cal-validate"   # agent >= 1.1.9 dry-run endpoint

# When the unit is simply uncalibrated, the /calibration route answers 404 with this
# detail. A generic "Not Found" 404 instead means the route itself is missing — i.e.
# the agent deployed on the unit predates the calibration endpoints and must be updated.
_NO_CAL_DETAIL = "no calibration document"
_OUTDATED_AGENT_MSG = (
    "this unit's agent is out of date — it has no calibration endpoint. "
    "Open the unit's ••• menu → “Update agent…”, then Refresh here.")


def _is_outdated_agent(err) -> bool:
    """Fallback heuristic (used only before /info capabilities are known): a 404 that
    is NOT the agent's own 'not calibrated' answer ⇒ the route is absent ⇒ the deployed
    agent predates the calibration/files endpoints."""
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


def _curve_issues(sid: str, plane: str, pts) -> list:
    """Cheap monotonicity checks on one signal/plane curve, mirroring the resolver:
    points sorted by gain must have strictly increasing gain AND power (invertible)."""
    vals = []
    for pt in pts or []:
        try:
            vals.append((float(pt["gain_db"]), float(pt["power_dbm"])))
        except (KeyError, TypeError, ValueError):
            return [f"signal '{sid}' · {plane}: a point isn't numeric"]
    if len(vals) < 2:
        return []                              # 0 pts = latent (legal); 1 pt = slope-1 ok
    vals.sort(key=lambda gp: gp[0])
    out = []
    if any(vals[i][0] <= vals[i - 1][0] for i in range(1, len(vals))):
        out.append(f"signal '{sid}' · {plane}: two points share a gain")
    if any(vals[i][1] <= vals[i - 1][1] for i in range(1, len(vals))):
        out.append(f"signal '{sid}' · {plane}: power must increase with gain (not invertible)")
    return out


def local_calibration_issues(doc) -> list:
    """A fast, best-effort structural check of a working document, for instant editor
    feedback BEFORE the authoritative agent validate/save. Catches the common mistakes
    (non-monotonic curve, no safety ceiling, unset/dangling operating plane, a derived
    plane missing its parent/Δ, a curve on a non-measured plane). Not exhaustive — the
    agent's resolver remains the source of truth."""
    if not isinstance(doc, dict):
        return ["document is not an object"]
    issues: list = []
    if doc.get("schema_version") != 1:
        issues.append(f"schema_version should be 1 (is {doc.get('schema_version')!r})")
    chain = doc.get("chain") or {}
    planes = chain.get("planes") or {}
    if not isinstance(planes, dict) or not planes:
        return issues + ["no planes defined — add at least one measured plane"]

    measured = {n for n, p in planes.items()
                if isinstance(p, dict) and p.get("type") == "measured"}
    for name, p in planes.items():
        if not isinstance(p, dict):
            issues.append(f"plane '{name}' is malformed"); continue
        t = p.get("type")
        if t == "derived":
            frm = p.get("from")
            if not frm:
                issues.append(f"derived plane '{name}' has no parent plane")
            elif frm not in planes:
                issues.append(f"derived plane '{name}' points at unknown plane '{frm}'")
            if p.get("delta_db") is None:
                issues.append(f"derived plane '{name}' has no Δ dB")
        elif t != "measured":
            issues.append(f"plane '{name}' has an unknown type")

    op = chain.get("operating_plane")
    if not op:
        issues.append("no operating plane set")
    elif op not in planes:
        issues.append(f"operating plane '{op}' is not one of the planes")
    else:
        seen, cur = set(), op                  # walk derived hops to a measured anchor
        while isinstance(planes.get(cur), dict) and planes[cur].get("type") == "derived":
            if cur in seen:
                issues.append(f"derived plane cycle through '{cur}'"); break
            seen.add(cur)
            cur = planes[cur].get("from")
            if cur not in planes:
                break

    gl = chain.get("gain_limits") or {}
    if gl.get("max_gain_db") is None and not chain.get("limits"):
        issues.append("no safety ceiling — set a max gain or add at least one limit")
    for lim in (chain.get("limits") or []):
        if isinstance(lim, dict) and lim.get("plane") not in planes:
            issues.append(f"limit references unknown plane '{lim.get('plane')}'")

    signals = doc.get("signals") or {}
    if not signals:
        issues.append("no signals — add at least one")
    for sid, sig in signals.items():
        for pname, curve in ((sig or {}).get("curves") or {}).items():
            if pname not in planes:
                issues.append(f"signal '{sid}': curve for unknown plane '{pname}'")
            elif pname not in measured:
                issues.append(f"signal '{sid}': curve given for derived plane '{pname}'")
            issues.extend(_curve_issues(sid, pname, (curve or {}).get("points")))
    return issues


class _Sparkline(QWidget):
    """A tiny gain→power plot of a curve's points, so a fat-fingered point (a dip, a
    duplicate) is obvious at a glance next to the grid."""
    def __init__(self):
        super().__init__()
        self._pts: list = []
        self.setFixedHeight(46)
        self.setMinimumWidth(120)
        self.setToolTip("gain (x) → power (y) for the points above")

    def set_points(self, pts) -> None:
        vals = []
        for g, p in pts or []:
            try:
                vals.append((float(g), float(p)))
            except (TypeError, ValueError):
                continue
        vals.sort(key=lambda gp: gp[0])
        self._pts = vals
        self.update()

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 6
        if len(self._pts) < 1:
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no points")
            return
        gs = [g for g, _ in self._pts]; ps = [p for _, p in self._pts]
        g0, g1 = min(gs), max(gs); p0, p1 = min(ps), max(ps)
        gspan = (g1 - g0) or 1.0; pspan = (p1 - p0) or 1.0

        def xy(g, p):
            x = pad + (g - g0) / gspan * (w - 2 * pad)
            y = h - pad - (p - p0) / pspan * (h - 2 * pad)   # y grows downward
            return x, y

        # detect a non-monotonic (non-invertible) power sequence → draw the line red
        bad = any(ps[i] <= ps[i - 1] for i in range(1, len(ps)))
        line = QColor(Palette.CRASH if bad else Palette.ACCENT)
        qp.setPen(QPen(line, 1.5))
        for i in range(1, len(self._pts)):
            x0, y0 = xy(*self._pts[i - 1]); x1, y1 = xy(*self._pts[i])
            qp.drawLine(int(x0), int(y0), int(x1), int(y1))
        qp.setPen(QPen(line, 1))
        qp.setBrush(line)
        for g, p in self._pts:
            x, y = xy(g, p)
            qp.drawEllipse(int(x) - 2, int(y) - 2, 4, 4)


def _rename_plane_in_doc(doc: dict, old: str, new: str) -> dict:
    """Rename a plane throughout a calibration document: the chain.planes key (order
    preserved), operating_plane, every limit's plane, every derived plane's 'from', and
    each signal's curve keyed by this plane. Keeps the document internally consistent so
    a rename never leaves a dangling reference. Mutates and returns `doc`."""
    if old == new or not new:
        return doc
    chain = doc.get("chain") or {}
    planes = chain.get("planes")
    if isinstance(planes, dict) and old in planes:
        # rebuild preserving insertion order with the one key swapped
        chain["planes"] = {(new if k == old else k): v for k, v in planes.items()}
    if chain.get("operating_plane") == old:
        chain["operating_plane"] = new
    for lim in (chain.get("limits") or []):
        if isinstance(lim, dict) and lim.get("plane") == old:
            lim["plane"] = new
    for spec in (chain.get("planes") or {}).values():
        if isinstance(spec, dict) and spec.get("from") == old:
            spec["from"] = new
    for sig in (doc.get("signals") or {}).values():
        curves = (sig or {}).get("curves")
        if isinstance(curves, dict) and old in curves:
            sig["curves"] = {(new if k == old else k): v for k, v in curves.items()}
    return doc


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
    def __init__(self, on_changed=None):
        super().__init__(0, 2)
        self._on_changed = on_changed          # called after any edit (live feedback)
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
                        "with gain AND power both strictly increasing. Tip: paste rows of "
                        "\"gain, power\" (Ctrl+V) straight from a spreadsheet.")
        self.cellChanged.connect(lambda *_: self._changed())
        self._fit_height()

    def _changed(self) -> None:
        if self._on_changed:
            self._on_changed()

    def numeric_points(self) -> list:
        """(gain, power) tuples for the sparkline — skips blank/non-numeric rows."""
        out = []
        for r in range(self.rowCount()):
            g = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            p = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            try:
                out.append((float(g), float(p)))
            except ValueError:
                continue
        return out

    def keyPressEvent(self, event) -> None:
        # Ctrl+V pastes spreadsheet rows: lines of "gain, power" (comma, tab, or
        # whitespace separated) become new points, so an operator can copy a measured
        # table straight in instead of retyping it cell by cell.
        if event.matches(QKeySequence.StandardKey.Paste):
            if self._paste_csv():
                return
        super().keyPressEvent(event)

    def _paste_csv(self) -> bool:
        text = QApplication.clipboard().text()
        if not text or not text.strip():
            return False
        rows = []
        for line in text.splitlines():
            line = line.strip().replace(",", " ").replace("\t", " ")
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
        if not rows:
            return False
        self.blockSignals(True)
        for g, p in rows:
            self._append(g, p)
        self.blockSignals(False)
        self._fit_height()
        self._changed()
        return True

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
        # Display sorted by gain (the resolver sorts internally anyway) so the grid
        # reads in the order the curve is actually interpolated.
        def _key(pt):
            try:
                return (0, float(pt.get("gain_db")))
            except (TypeError, ValueError):
                return (1, 0.0)                # unparseable rows sink to the bottom
        self.blockSignals(True)
        self.setRowCount(0)
        for pt in sorted(points or [], key=_key):
            self._append(_numstr(pt.get("gain_db")), _numstr(pt.get("power_dbm")))
        self.blockSignals(False)
        self._fit_height()

    def _append(self, g="", p="") -> None:
        r = self.rowCount()
        self.insertRow(r)
        self.setItem(r, 0, QTableWidgetItem(g))
        self.setItem(r, 1, QTableWidgetItem(p))

    def add_blank_row(self) -> None:
        self._append()
        self._fit_height()
        self._changed()

    def remove_selected(self) -> None:
        # Remove the selected rows; if nothing is selected, fall back to the current
        # row, then the last row — so "− point" always removes something rather than
        # silently doing nothing when the user hasn't clicked to select a whole row.
        rows = {i.row() for i in self.selectedItems()}
        if not rows and self.currentRow() >= 0:
            rows = {self.currentRow()}
        if not rows and self.rowCount() > 0:
            rows = {self.rowCount() - 1}
        for r in sorted(rows, reverse=True):
            self.removeRow(r)
        self._fit_height()
        self._changed()

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
        self._validate_btn = QPushButton("Validate"); self._validate_btn.clicked.connect(self._on_validate)
        self._validate_btn.setToolTip("Check this document against the unit WITHOUT saving "
                                      "it — preview what each signal resolves to, or why "
                                      "it's rejected.")
        self._upload_btn = QPushButton("Upload…"); self._upload_btn.setObjectName("primary")
        self._upload_btn.clicked.connect(self._on_upload)
        self._save_btn = QPushButton("Save"); self._save_btn.clicked.connect(self._on_save)
        self._download_btn = QPushButton("Download…"); self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)
        for b in (self._refresh_btn, self._validate_btn, self._upload_btn,
                  self._save_btn, self._download_btn):
            row.addWidget(b)
        row.addStretch(1)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._status)

        # Live local check — instant, structural, complementing the authoritative
        # agent Validate/Save. Hidden when the working document has no issues.
        self._issues = QLabel("")
        self._issues.setWordWrap(True)
        self._issues.setVisible(False)
        self._issues.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
        outer.addWidget(self._issues)

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
        self._f["unit_type"] = QComboBox()
        for t in UNIT_TYPES:
            self._f["unit_type"].addItem(UNIT_TYPE_LABELS.get(t, t), t)
        self._f["unit_type"].setToolTip(
            "This unit's hardware type. It selects the shared type-defaults chain that's "
            "merged in, so it must match the real unit — a wrong type silently mis-resolves.")
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
        self._f["def_amp"] = QLineEdit()
        self._f["def_amp"].setToolTip(
            "The default baseband amplitude (0–1) a signal uses when it doesn't set its "
            "own. A signal's own amplitude, when set, overrides this.")
        gl_form.addRow("Unit type", self._f["unit_type"])
        gl_form.addRow("Min gain (dB)", self._f["min_gain"])
        gl_form.addRow("Max gain (dB)", self._f["max_gain"])
        gl_form.addRow("Operating plane", self._f["operating"])
        gl_form.addRow("Default amplitude", self._f["def_amp"])
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
        pl_box = QGroupBox("Planes — the RF chain (SDR → amp → cable → antenna) · shared by every signal")
        pl_box.setToolTip(
            "The chain topology is unit HARDWARE: define it once here and every signal "
            "reuses it — you don't repeat planes per signal. A 'measured' plane has its "
            "own gain→power curve (entered per signal below); a 'derived' plane is another "
            "plane plus a fixed offset — cable loss (negative Δ) or antenna gain (positive Δ).")
        pl_v = QVBoxLayout(pl_box)
        pl_help = QLabel(
            "Defined once for the whole unit — every signal shares these planes. Per row: "
            "a name, the type (measured = you took readings here; derived = parent plane + "
            "a fixed Δ dB), and a short quantity label (e.g. “total in-band power”, "
            "“main-lobe EIRP”).")
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
            "The planes are shared (defined once above); here you enter only the points "
            "YOU MEASURED for this signal on each measured plane. Two or more per plane, "
            "with gain AND power both strictly increasing — the unit interpolates between "
            "them and refuses powers outside their range. (Different signals need their "
            "own points because power-vs-gain depends on the waveform.)")
        sig_help.setWordWrap(True)
        sig_help.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        sig_v.addWidget(sig_help)
        self._signals_box = QVBoxLayout(); self._signals_box.setSpacing(8)
        sig_v.addLayout(self._signals_box)
        add_sig = QPushButton("+ Add signal…"); add_sig.clicked.connect(self._on_add_signal)
        sig_v.addWidget(add_sig, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addWidget(sig_box)

        # Empty-state hint / template button (shown when there's no document). Its label
        # is filled with the unit's real type in _doc_to_form so it doesn't imply
        # broadcaster on, say, an X410.
        self._empty_hint = QPushButton("New from template")
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
        if not have:
            utype, _ = self._unit_meta()
            self._empty_hint.setText(f"New from {utype} template")
        chain = (doc or {}).get("chain") or {}
        gl = chain.get("gain_limits") or {}
        self._f["min_gain"].setText(_numstr(gl.get("min_gain_db")))
        self._f["max_gain"].setText(_numstr(gl.get("max_gain_db")))

        ut = (doc or {}).get("unit_type", "")
        i = self._f["unit_type"].findData(ut)
        if i >= 0:
            self._f["unit_type"].setCurrentIndex(i)
        elif ut:                                    # a type we don't have a label for
            self._f["unit_type"].addItem(ut, ut)
            self._f["unit_type"].setCurrentIndex(self._f["unit_type"].count() - 1)
        self._f["def_amp"].setText(_numstr(((doc or {}).get("defaults") or {}).get("amplitude")))

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
        self._spark_src = {}                # sparkline → its source curve table
        measured = self._measured_planes()
        for sid, sig in ((doc or {}).get("signals") or {}).items():
            self._add_signal_widget(sid, sig or {}, measured)

        self._update_issues()
        self._sync_validate_button()

    def _update_issues(self) -> None:
        """Recompute the instant local structural check from the current widgets and
        show the top few problems (or hide the panel when the document is clean)."""
        try:
            doc = self._read_form(strict=False)
        except ValueError:
            return
        issues = local_calibration_issues(doc)
        if not issues:
            self._issues.setVisible(False)
            self._issues.clear()
            return
        shown = issues[:6]
        more = f"  (+{len(issues) - len(shown)} more)" if len(issues) > len(shown) else ""
        self._issues.setText("⚠ " + " · ".join(shown) + more)
        self._issues.setVisible(True)

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
            self._doc = self._blank_doc()
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
        # "orig" is the plane's last-committed name, so a rename can be propagated to
        # everything that references it (operating plane, limits, derived 'from', and
        # each signal's curve keyed by this plane) instead of silently dangling.
        row = {"w": w, "name": name_e, "type": type_c, "from": from_c,
               "delta": delta_e, "quantity": quantity_e, "from_lbl": from_lbl,
               "orig": name}

        derived = type_c.currentText() == "derived"
        for wdg in (from_lbl, from_c, delta_e):
            wdg.setVisible(derived)
        # A type/name change reshapes dependents (which planes are 'measured', the
        # from/operating/limit dropdowns), so rebuild the form from the widgets.
        type_c.currentTextChanged.connect(lambda _=None: self._refresh_form_from_widgets())
        name_e.editingFinished.connect(lambda r=row: self._on_plane_name_changed(r))
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
        # Removing a plane cascades — it drops that plane's measured points from every
        # signal that has them, plus its limits, and clears the operating pointer if it
        # pointed here. Confirm first, since that's easy to do by a mis-click and not
        # obviously reversible.
        affected = sorted(
            sid for sid, sig in ((self._doc or {}).get("signals") or {}).items()
            if isinstance((sig or {}).get("curves"), dict) and name in sig["curves"])
        if name:
            detail = (f"\n\nThis also removes its measured points from "
                      f"{len(affected)} signal(s): {', '.join(affected)}." if affected else "")
            resp = QMessageBox.question(
                self, "Remove plane",
                f"Remove plane '{name}' from the chain?{detail}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if resp != QMessageBox.StandardButton.Yes:
                self._doc_to_form()               # repaint (undo the widget-level edit)
                return
        chain = (self._doc or {}).get("chain", {})
        planes = chain.get("planes") or {}
        planes.pop(name, None)
        # Drop references to the removed plane so the document stays consistent: its
        # safety limits, its per-signal curves, and the operating-plane pointer if it
        # pointed here. (A derived plane whose parent this was is left for the agent to
        # flag clearly — silently rewiring the chain would be worse than a plain error.)
        chain["limits"] = [l for l in (chain.get("limits") or [])
                           if not (isinstance(l, dict) and l.get("plane") == name)]
        if chain.get("operating_plane") == name:
            chain["operating_plane"] = ""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            curves = (sig or {}).get("curves")
            if isinstance(curves, dict):
                curves.pop(name, None)
        self._doc_to_form()

    def _refresh_form_from_widgets(self) -> None:
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            return
        self._doc_to_form()

    def _on_plane_name_changed(self, row: dict) -> None:
        """A plane was renamed in the form. Read the form back with the OLD name (so
        curves/operating/limits stay consistent), then rename the plane everywhere it's
        referenced, so nothing dangles. Falls back to a plain rebuild when there's no
        real rename or the new name would collide with another plane."""
        new = row["name"].text().strip()
        old = row.get("orig", "")
        planes_now = {r["name"].text().strip() for r in self._f.get("planes", []) if r is not row}
        if not new or new == old or new in planes_now:
            # nothing to propagate (or a name clash — let the generic rebuild/agent
            # surface it); just resync so the rest of the form stays current.
            self._refresh_form_from_widgets()
            return
        row["name"].setText(old)                      # read the form under the old name…
        try:
            doc = self._read_form(strict=False)
        except ValueError:
            row["name"].setText(new)
            self._refresh_form_from_widgets()
            return
        row["name"].setText(new)
        self._doc = _rename_plane_in_doc(doc, old, new)
        row["orig"] = new
        self._doc_to_form()

    def _add_signal_widget(self, sid: str, sig: dict, measured) -> None:
        box = QGroupBox(f"signal: {sid}")
        v = QVBoxLayout(box)
        top = QFormLayout()
        amp = QLineEdit(_numstr(sig.get("amplitude")))
        # Show the inherited default as a placeholder so a blank field visibly means
        # "inherit defaults.amplitude", not "unset".
        def_amp = ((self._doc or {}).get("defaults") or {}).get("amplitude")
        if def_amp is not None:
            amp.setPlaceholderText(f"inherits default ({_numstr(def_amp)})")
        amp.setToolTip("The baseband amplitude (0–1) the script drives for this signal. "
                       "It must match the amplitude used while measuring the curve below, "
                       "since power scales with it. Leave blank to inherit the chain's "
                       "default amplitude.")
        bw = QLineEdit(_numstr(sig.get("occupied_bw_hz")))
        bw.setToolTip("Occupied bandwidth of the signal in Hz (optional) — used to relate "
                      "total in-band power to spectral density.")
        top.addRow("Amplitude (0–1)", amp)
        top.addRow("Occupied BW (Hz)", bw)
        v.addLayout(top)

        curves = {}
        for plane in measured:
            v.addWidget(QLabel(f"curve · {plane}"))
            spark = _Sparkline()
            tbl = _CurveTable(on_changed=lambda t=None, s=spark: self._on_curve_changed(s))
            tbl.set_points(((sig.get("curves") or {}).get(plane) or {}).get("points"))
            spark.set_points(tbl.numeric_points())
            row = QHBoxLayout()
            row.addWidget(tbl, 3); row.addWidget(spark, 2)
            v.addLayout(row)
            self._spark_src[spark] = tbl   # so the change handler can refresh the plot
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

    def _on_curve_changed(self, spark: "_Sparkline") -> None:
        """A curve grid was edited: repaint its sparkline from its source table and
        refresh the live local-issues panel."""
        src = getattr(self, "_spark_src", {}).get(spark)
        if src is not None:
            spark.set_points(src.numeric_points())
        self._update_issues()

    # ── views → model ─────────────────────────────────────────────────────────────
    def _read_form(self, strict: bool) -> dict:
        """Rebuild the document from the editor widgets, preserving fields the form
        doesn't model (schema_version, unit_id, meta, chain.planes, interp/offset_db).
        strict=True raises ValueError on bad numeric input."""
        doc = copy.deepcopy(self._doc) if self._doc else _template()
        ut = self._f["unit_type"].currentData()
        if ut:
            doc["unit_type"] = ut
        def_amp_text = self._f["def_amp"].text().strip()
        defaults = doc.setdefault("defaults", {})
        if def_amp_text:
            try:
                defaults["amplitude"] = _to_float(def_amp_text, "default amplitude")
            except ValueError:
                if strict:
                    raise
        else:
            defaults.pop("amplitude", None)       # blank ⇒ no chain-wide default
        if not defaults:
            doc.pop("defaults", None)
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
            # Start from the stored signal so fields the form doesn't model (the JSON
            # tab is the source of truth for those) survive a form round-trip; then
            # overwrite only what the form edits.
            sig = dict(prev_sigs.get(sid) or {})
            if w["amp"].text().strip():
                sig["amplitude"] = _to_float(w["amp"].text(), f"{sid} amplitude")
            else:
                sig.pop("amplitude", None)
            if w["bw"].text().strip():
                sig["occupied_bw_hz"] = _to_float(w["bw"].text(), f"{sid} occupied BW")
            else:
                sig.pop("occupied_bw_hz", None)
            curves = {}
            for plane, tbl in w["curves"].items():
                pts = tbl.points(strict)
                if not pts:
                    continue
                prev = ((prev_sigs.get(sid) or {}).get("curves") or {}).get(plane) or {}
                entry = dict(prev)           # preserve unmodeled curve fields too
                entry["points"] = pts
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
        # Leaving the JSON tab with unparseable text would silently discard those edits
        # (best-effort sync swallows the error and the Editor repaints from the stale
        # model). Keep the user on JSON with a clear error instead of losing their work.
        if self._prev_tab == 1 and idx != 1:
            text = self._view.toPlainText().strip()
            if text:
                try:
                    json.loads(text)
                except ValueError as exc:
                    self._set_status(
                        f"JSON has an error — fix it or clear it before leaving this tab: {exc}",
                        kind="error")
                    self._tabs.blockSignals(True)
                    self._tabs.setCurrentIndex(1)
                    self._tabs.blockSignals(False)
                    return
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

    def _unit_meta(self) -> tuple[str, str]:
        """This unit's type + id, read from its client, to seed a new document with
        the RIGHT unit_type (it selects the shared type-defaults layer, so a wrong one
        silently mis-resolves) instead of the template's hardcoded 'broadcaster'."""
        try:
            c = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return "broadcaster", self.hostname
        return (getattr(c, "unit_type", "") or "broadcaster",
                getattr(c, "unit_id", "") or self.hostname)

    def _blank_doc(self) -> dict:
        """A fresh template stamped with this unit's real type + id."""
        doc = _template()
        utype, uid = self._unit_meta()
        doc["unit_type"] = utype
        doc["unit_id"] = uid
        return doc

    def _on_new_template(self) -> None:
        self._set_doc(self._blank_doc())
        utype, _ = self._unit_meta()
        self._set_status(f"template loaded (unit type: {utype}) — edit, then Save", kind="warn")

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

    def _supports(self, capability: str) -> bool:
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(client, "supports", lambda _c: False)(capability))

    def _sync_validate_button(self) -> None:
        ok = self._supports(CAL_VALIDATE_CAPABILITY)
        self._validate_btn.setEnabled(ok)
        self._validate_btn.setToolTip(
            "Check this document against the unit WITHOUT saving it — preview what each "
            "signal resolves to, or why it's rejected." if ok else
            "This unit's agent is too old for dry-run validate (needs 1.1.9+). "
            "Local checks still run above; use Save to validate on the unit.")

    def _on_validate(self) -> None:
        try:
            self._sync_from(self._tabs.currentIndex(), strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._set_status("nothing to validate", kind="faint")
            return
        self._update_issues()                       # instant local pass
        issues = local_calibration_issues(self._doc)
        if not self._supports(CAL_VALIDATE_CAPABILITY):
            self._set_status(
                f"{len(issues)} local issue(s) found — see above" if issues else
                "no local issues found (agent too old to dry-run on the unit)",
                kind="error" if issues else "warn")
            return
        self._set_status("validating (dry run — not saving)…")
        doc = self._doc
        self.hub.run_async(
            f"cal_validate:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).validate_calibration(doc))

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
        elif parts[0] == "cal_validate":
            self._handle_validate(result)

    def _handle_validate(self, result) -> None:
        if isinstance(result, Exception):
            self._set_status(f"validate failed: {result}", kind="error")
            return
        if not isinstance(result, dict):
            self._set_status("unexpected response", kind="error")
            return
        if result.get("valid"):
            self._populate_table(result.get("signals") or {})
            n = len(result.get("signals") or {})
            self._set_status(
                f"valid ✓ (dry run) · {n} signal(s) resolve — NOT saved yet", kind="ok")
        else:
            self._set_status(f"would be REJECTED: {result.get('error', '')}", kind="error")

    def _agent_lacks_calibration(self) -> bool:
        """Definitive when the unit's /info has been read (agent_version is set): the
        agent is reachable but advertises no calibration capability ⇒ it's too old.
        When /info hasn't been read yet, returns False and the caller falls back to the
        404 heuristic on the actual response."""
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(client, "agent_version", "")) and not (
            getattr(client, "supports", lambda _c: False)(CAL_CAPABILITY))

    def _is_outdated(self, result) -> bool:
        """Prefer the explicit capability flag; fall back to the 404 heuristic."""
        return self._agent_lacks_calibration() or _is_outdated_agent(result)

    def _handle_get(self, result) -> None:
        if self._is_outdated(result):
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
        if self._is_outdated(result):
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
            self._doc = self._blank_doc()
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
