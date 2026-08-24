"""
CalibrationPanel — the Calibration sub-tab of the unit detail view.

Shows this unit's power calibration (whether it's calibrated + a resolved per-signal
summary) and lets you edit `calibration.json` two ways:

  • Editor  — the "chain builder" (calibration v2, docs/calibration-v2.md §8): the RF
              chain reads left-to-right as a flow of STAGES. Source/measured stages show
              their gain→power minicurve; PASSIVE stages (cable/antenna/pad) are pickers
              onto the fleet-wide component library, their loss evaluated at each
              signal's frequency. Selecting a stage opens a detail pane — a frequency-
              response plot + component picker for a passive stage, or the per-signal
              measured curve grids for a measured stage. Alongside: the resolved Signals
              table, the Limits/ceiling, the Component library grid, and the chain
              settings (gains, operating plane, defaults).
  • JSON    — the raw document (source of truth for the plane topology and anything
              the editor doesn't cover).

Both views drive one document model (self._doc); switching tabs syncs it. Upload or
Save sends the document to the agent, which VALIDATES it (the full resolver checks)
before storing — so a bad curve is rejected with the agent's exact reason, never at
transmit. Passive planes reference components by id; the catalog (components.yaml) is
uploaded to the unit first so those refs resolve.

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
    QAbstractItemDelegate, QAbstractItemView, QAbstractScrollArea, QApplication,
    QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from api.client import AgentHTTPError
from api.models import UNIT_TYPES, UNIT_TYPE_LABELS
from .theme import Palette

CAL_NAME = "calibration.json"
CAL_CAPABILITY = "calibration"
CAL_VALIDATE_CAPABILITY = "cal-validate"   # agent >= 1.1.9 dry-run endpoint
CAL_COMPONENTS_CAPABILITY = "calibration-components"   # agent >= 1.2.0 (v2 component refs)
_COMPONENTS_NEEDS_NEWER = (
    "this unit's agent is too old for component-based calibration (needs 1.2.0+). "
    "Update the agent, or use a constant Δ dB on the passive planes.")

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
            # A passive hop's Δ dB comes from either an inline constant (delta_db) OR a
            # library component (component, possibly frequency-dependent) — v2. Only flag
            # when it has NEITHER.
            if p.get("delta_db") is None and not p.get("component"):
                issues.append(f"derived plane '{name}' has no Δ dB or component")
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


# ── calibration v2 "chain builder" visual pieces (the mockup) ────────────────────

def _interp_db(table, f: float) -> float:
    """Linear interpolation of a [[freq_hz, delta_db], …] table at frequency f, with
    endpoint clamping (mirrors the agent/calkit interp). Empty table → 0.0."""
    pts = sorted(((float(a), float(b)) for a, b in (table or [])), key=lambda p: p[0])
    if not pts:
        return 0.0
    if len(pts) == 1 or f <= pts[0][0]:
        return pts[0][1]
    if f >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        if f <= pts[i][0]:
            (x0, y0), (x1, y1) = pts[i - 1], pts[i]
            return y0 + (y1 - y0) * (f - x0) / (x1 - x0)
    return pts[-1][1]


def _freq_span(table):
    """(min_freq_hz, max_freq_hz) of a delta table, or None if empty."""
    fs = [float(a) for a, _ in (table or [])]
    return (min(fs), max(fs)) if fs else None


def _fmt_ghz_span(table) -> str:
    span = _freq_span(table)
    if not span:
        return "—"
    lo, hi = span
    if lo == hi:
        return "flat · constant"
    return f"{lo/1e9:.2f}–{hi/1e9:.2f} GHz"


def _badge(text: str, fg: str, bg: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(
        f"color: {fg}; background: {bg}; font-size: 10px; font-weight: 700; "
        f"letter-spacing: .06em; padding: 2px 7px; border-radius: 5px;")
    return lab


# kind → (foreground, background) for badges, matching the mockup
_KIND_COLORS = {
    "source":   (Palette.TEXT_MUTED, Palette.IDLE_SOFT),
    "measured": (Palette.ACCENT, Palette.ACCENT_SOFT),
    "passive":  (Palette.ARMED, Palette.ARMED_SOFT),
    "cable":    (Palette.ACCENT, Palette.ACCENT_SOFT),
    "antenna":  (Palette.ONLINE, Palette.ONLINE_SOFT),
    "pad":      (Palette.TEXT_MUTED, Palette.IDLE_SOFT),
}


class _FreqSparkline(QWidget):
    """A tiny ΔdB-vs-frequency curve for a component (loss/gain sweep). A single point
    draws as a flat line (a constant hop)."""
    def __init__(self, height: int = 40):
        super().__init__()
        self._pts: list = []
        self._color = Palette.ACCENT
        self.setFixedHeight(height)
        self.setMinimumWidth(80)

    def set_table(self, table, color: str = Palette.ACCENT) -> None:
        self._pts = sorted(((float(a), float(b)) for a, b in (table or [])),
                           key=lambda p: p[0])
        self._color = color
        self.update()

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 5
        if not self._pts:
            return
        col = QColor(self._color)
        if len(self._pts) == 1:
            qp.setPen(QPen(QColor(Palette.IDLE), 2))
            y = h // 2
            qp.drawLine(pad, y, w - pad, y)
            return
        fs = [f for f, _ in self._pts]; ds = [d for _, d in self._pts]
        f0, f1 = min(fs), max(fs); d0, d1 = min(ds), max(ds)
        fspan = (f1 - f0) or 1.0; dspan = (d1 - d0) or 1.0

        def xy(f, d):
            x = pad + (f - f0) / fspan * (w - 2 * pad)
            y = h - pad - (d - d0) / dspan * (h - 2 * pad)
            return int(x), int(y)

        qp.setPen(QPen(col, 2))
        for i in range(1, len(self._pts)):
            x0, y0 = xy(*self._pts[i - 1]); x1, y1 = xy(*self._pts[i])
            qp.drawLine(x0, y0, x1, y1)


class _FreqResponsePlot(QWidget):
    """The big per-component frequency-response plot: the ΔdB(f) sweep with its measured
    points, vertical band markers at the signals' frequencies, and an evaluated dot on
    the curve at each. Δ negative = loss, positive = gain."""
    def __init__(self):
        super().__init__()
        self._table: list = []
        self._markers: list = []      # [(label, freq_hz, color), …]
        self.setMinimumHeight(190)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().verticalPolicy())

    def set_data(self, table, markers) -> None:
        self._table = sorted(((float(a), float(b)) for a, b in (table or [])),
                             key=lambda p: p[0])
        self._markers = list(markers or [])
        self.update()

    def paintEvent(self, _evt) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        L, R, T, B = 44, 12, 16, 26
        x0, x1, y0, y1 = L, W - R, T, H - B
        qp.fillRect(self.rect(), QColor(Palette.SURFACE))
        # axes
        qp.setPen(QPen(QColor(Palette.BORDER), 1))
        qp.drawLine(x0, y0, x0, y1)
        qp.drawLine(x0, y1, x1, y1)
        if not self._table:
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                        "select a passive stage to see its frequency response")
            return
        fs = [f for f, _ in self._table]; ds = [d for _, d in self._table]
        # include marker freqs in the x-range so their lines land on the plot
        mfs = [m[1] for m in self._markers]
        fmin = min(fs + mfs); fmax = max(fs + mfs)
        if fmax == fmin:
            fmax = fmin + 1.0
        dmin = min(ds); dmax = max(ds)
        if dmax == dmin:
            dmin -= 0.5; dmax += 0.5
        dpad = (dmax - dmin) * 0.15
        dmin -= dpad; dmax += dpad

        def X(f):
            return x0 + (f - fmin) / (fmax - fmin) * (x1 - x0)

        def Y(d):
            return y1 - (d - dmin) / (dmax - dmin) * (y1 - y0)

        mono = QFont("monospace"); mono.setPointSize(8)
        qp.setFont(mono)
        # y grid + labels (dB)
        qp.setPen(QColor(Palette.TEXT_FAINT))
        for frac in (0.0, 0.5, 1.0):
            d = dmax - frac * (dmax - dmin)
            yy = int(Y(d))
            qp.setPen(QPen(QColor(Palette.SURFACE_ALT), 1))
            qp.drawLine(x0, yy, x1, yy)
            qp.setPen(QColor(Palette.TEXT_FAINT))
            qp.drawText(0, yy - 6, L - 6, 12,
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                        f"{d:+.2f}")
        # x labels (GHz) at the ends + middle
        for frac in (0.0, 0.5, 1.0):
            f = fmin + frac * (fmax - fmin)
            xx = int(X(f))
            al = (Qt.AlignmentFlag.AlignHCenter if frac == 0.5 else
                  (Qt.AlignmentFlag.AlignLeft if frac == 0.0 else Qt.AlignmentFlag.AlignRight))
            qp.drawText(xx - 24, y1 + 4, 48, 14,
                        int(al | Qt.AlignmentFlag.AlignTop), f"{f/1e9:.2f}")
        # band markers (vertical dashed) + evaluated dots
        for label, freq, color in self._markers:
            xx = int(X(freq))
            pen = QPen(QColor(color), 1.5); pen.setDashPattern([3, 3])
            qp.setPen(pen)
            qp.drawLine(xx, y0, xx, y1)
            qp.setPen(QColor(color))
            fm = qp.fontMetrics()
            tw = min(fm.horizontalAdvance(label) + 6, x1 - x0)
            tx = max(x0, min(xx - tw // 2, x1 - tw))     # keep the label inside the axes
            qp.drawText(tx, y0 - 2, tw, 12,
                        int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom),
                        label)
        # the measured Δ(f) curve
        qp.setPen(QPen(QColor(Palette.ACCENT), 2.5))
        prev = None
        for f, d in self._table:
            pt = (int(X(f)), int(Y(d)))
            if prev is not None:
                qp.drawLine(prev[0], prev[1], pt[0], pt[1])
            prev = pt
        qp.setBrush(QColor(Palette.ACCENT)); qp.setPen(QColor(Palette.ACCENT))
        for f, d in self._table:
            qp.drawEllipse(int(X(f)) - 3, int(Y(d)) - 3, 6, 6)
        # evaluated dots on the curve at each marker freq
        for label, freq, color in self._markers:
            d = _interp_db(self._table, freq)
            qp.setBrush(QColor(color))
            qp.setPen(QPen(QColor(Palette.SURFACE), 1.5))
            qp.drawEllipse(int(X(freq)) - 4, int(Y(d)) - 4, 8, 8)


_STAGE_MIME = "application/x-sdr-stage"


class _ClickCard(QFrame):
    """A QFrame that runs a callback when clicked — used for the chain stages and the
    component-library cards. Optionally a drop target: when `on_drop` is given it accepts
    a dragged stage (see _DragHandle) and calls on_drop(src_name, self._drop_name)."""
    def __init__(self, on_click=None, on_drop=None, drop_name=None):
        super().__init__()
        self._on_click = on_click
        self._on_drop = on_drop
        self._drop_name = drop_name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if on_drop is not None:
            self.setAcceptDrops(True)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._on_click is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._on_drop is not None and event.mimeData().hasFormat(_STAGE_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._on_drop is not None and event.mimeData().hasFormat(_STAGE_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._on_drop is not None and event.mimeData().hasFormat(_STAGE_MIME):
            src = bytes(event.mimeData().data(_STAGE_MIME)).decode("utf-8")
            event.acceptProposedAction()
            if src and src != self._drop_name:
                self._on_drop(src, self._drop_name)


class _DragHandle(QLabel):
    """The grip on a chain stage: dragging it reorders the stage (dropping onto another
    stage card). Kept separate from the card's click-to-select so the two don't fight."""
    def __init__(self, name: str):
        super().__init__("⠿")
        self._name = name
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder this stage")
        self.setStyleSheet(f"color:{Palette.TEXT_FAINT};font-size:13px;")

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        from PyQt6.QtCore import QMimeData
        from PyQt6.QtGui import QDrag
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_STAGE_MIME, self._name.encode("utf-8"))
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)


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
    def __init__(self, on_changed=None, headers=("gain (dB)", "power (dBm)")):
        super().__init__(0, 2)
        self._on_changed = on_changed          # called after any edit (live feedback)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # No persistent selection fill: clicking a cell (or arrow-keying to it) makes
        # it the CURRENT cell, and that outline is the only in-focus visual — it shows
        # only while the grid has focus, so nothing stays highlighted after you click
        # away. (NoSelection still supports a current cell + arrow-key navigation.)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Edit on a double-click or F2, or just by typing on the current cell.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.AnyKeyPressed
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        # The current (focused) cell gets a clear accent outline — the only in-focus
        # visual, and it disappears on its own when the grid loses focus.
        self.setStyleSheet(
            f"QTableWidget::item:focus {{ background: {Palette.SURFACE}; "
            f"border: 1px solid {Palette.ACCENT}; }}")
        # Grow with the rows (up to a cap) so added points are always visible, rather
        # than hiding them behind an inner scrollbar in a fixed-height box.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip("Each row is one measured point: the SDR gain you set and the "
                        "power you measured on this plane. Enter at least two points, "
                        "with gain AND power both strictly increasing.\n\n"
                        "Double-click a cell (or just type) to edit · Del clears the "
                        "current cell · Ctrl+Z / Ctrl+Y undo/redo · Esc or click away "
                        "to deselect · paste rows of \"gain, power\" (Ctrl+V) from a "
                        "spreadsheet.")
        # Undo/redo history of row snapshots (see _record_history / undo / redo).
        self._history: list = [[]]
        self._hist_idx = 0
        self._restoring = False
        self.cellChanged.connect(lambda *_: self._changed())
        # Clicking anywhere outside the grid (including out of its own cell editor)
        # drops the current cell, so no highlight lingers after clicking away.
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        self._fit_height()

    def _changed(self) -> None:
        self._record_history()
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
        if event.matches(QKeySequence.StandardKey.Paste) and self._paste_csv():
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo()
            return
        # Redo: the platform's standard chord, plus an explicit Ctrl+Y so it works the
        # same everywhere (StandardKey.Redo is Ctrl+Y on Windows but Ctrl+Shift+Z on
        # some Linux setups).
        if event.matches(QKeySequence.StandardKey.Redo) or (
                event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_Y):
            self.redo()
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._deselect()               # Esc drops the current cell
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) \
                and self._clear_current_contents():
            return                         # Del/Backspace empties the current cell
        super().keyPressEvent(event)

    # ── Focus / current-cell ergonomics ──────────────────────────────────────

    def _on_focus_changed(self, _old, new) -> None:
        # When focus leaves the grid entirely (including its own cell editor), drop
        # the current cell so no highlight lingers after clicking away.
        if new is self or (new is not None and self.isAncestorOf(new)):
            return
        self.setCurrentCell(-1, -1)

    def _deselect(self) -> None:
        self.clearSelection()
        self.setCurrentCell(-1, -1)

    def _finish_edit(self) -> None:
        """Commit and close any open cell editor, so its value is saved before rows
        are added/removed underneath it (the +/− buttons are focus-less, so a click
        on them doesn't itself end the edit)."""
        if self.state() != QAbstractItemView.State.EditingState:
            return
        editor = self.focusWidget()
        if editor is not None and editor is not self:
            self.commitData(editor)
            self.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _clear_current_contents(self) -> bool:
        it = self.currentItem()
        if it is None or it.text() == "":
            return False
        it.setText("")                     # fires cellChanged → history + sparkline
        return True

    # ── Undo / redo ──────────────────────────────────────────────────────────

    def _snapshot(self) -> list:
        return [((self.item(r, 0).text() if self.item(r, 0) else ""),
                 (self.item(r, 1).text() if self.item(r, 1) else ""))
                for r in range(self.rowCount())]

    def _reset_history(self) -> None:
        """Make the current grid the baseline (called after a programmatic load), so
        undo never reaches back past it."""
        self._history = [self._snapshot()]
        self._hist_idx = 0

    def _record_history(self) -> None:
        if self._restoring:
            return
        snap = self._snapshot()
        if snap == self._history[self._hist_idx]:
            return                         # nothing actually changed
        del self._history[self._hist_idx + 1:]     # a fresh edit drops the redo branch
        self._history.append(snap)
        self._hist_idx += 1
        cap = 200
        if len(self._history) > cap:
            drop = len(self._history) - cap
            del self._history[:drop]
            self._hist_idx -= drop

    def _restore(self, snap) -> None:
        self._restoring = True
        self.blockSignals(True)
        self.setRowCount(0)
        for g, p in snap:
            self._append(g, p)
        self.blockSignals(False)
        self._restoring = False
        self._fit_height()
        if self._on_changed:
            self._on_changed()             # refresh the sparkline, but don't re-record

    def undo(self) -> None:
        if self._hist_idx > 0:
            self._hist_idx -= 1
            self._restore(self._history[self._hist_idx])

    def redo(self) -> None:
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._restore(self._history[self._hist_idx])

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
        self._reset_history()   # the loaded points are the undo baseline

    def _append(self, g="", p="") -> None:
        r = self.rowCount()
        self.insertRow(r)
        self.setItem(r, 0, QTableWidgetItem(g))
        self.setItem(r, 1, QTableWidgetItem(p))

    def add_blank_row(self) -> None:
        self._finish_edit()
        self._append()
        self._fit_height()
        self._changed()
        # Land on the new row's first cell, ready to type straight away.
        r = self.rowCount() - 1
        if r >= 0:
            self.setCurrentCell(r, 0)

    def remove_selected(self) -> None:
        # Remove the selected rows; if nothing is selected, fall back to the current
        # row, then the last row — so "− point" always removes something rather than
        # silently doing nothing when the user hasn't clicked to select a whole row.
        self._finish_edit()
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

    # Generic two-column accessors (used when this grid holds a freq→Δ dB table for a
    # component, not a gain→power curve).
    def set_rows(self, pairs) -> None:
        self.blockSignals(True)
        self.setRowCount(0)
        for x, y in (pairs or []):
            self._append(_numstr(x), _numstr(y))
        self.blockSignals(False)
        self._fit_height()
        self._reset_history()

    def rows(self, strict: bool) -> list:
        out = []
        for r in range(self.rowCount()):
            a = self.item(r, 0).text().strip() if self.item(r, 0) else ""
            b = self.item(r, 1).text().strip() if self.item(r, 1) else ""
            if not a and not b:
                continue
            try:
                out.append([_to_float(a, f"row {r+1} col 1"), _to_float(b, f"row {r+1} col 2")])
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
        self._expanded_signals: set = set()    # measured-detail signals shown expanded
        from state import ComponentCatalog
        self._catalog = ComponentCatalog()      # the client's canonical component library
        self._components_synced = False          # merged this unit's catalog on first load
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
        self._json_btn = QPushButton("JSON…")
        self._json_btn.setToolTip("View or edit the raw calibration document — for anything "
                                  "the form doesn't cover. Applying it reloads the editor.")
        self._json_btn.clicked.connect(self._open_json)
        self._components_btn = QPushButton("Components…")
        self._components_btn.setToolTip("Open the component library — characterize cables "
                                        "and antennas once, then pick them in the chain.")
        self._components_btn.clicked.connect(self._open_components)
        for b in (self._refresh_btn, self._validate_btn, self._upload_btn,
                  self._save_btn, self._download_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self._json_btn)
        row.addWidget(self._components_btn)
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

        # The resolved per-signal summary table now lives inside the editor's Signals
        # card (built in _build_editor_tab); _populate_table fills it after a get/validate.

        outer.addWidget(self._build_editor_tab(), stretch=1)

    # ── card scaffolding (matches the mockup's .card + header) ───────────────────
    def _make_card(self, *, number=None, title=None, desc=None, lbl=None, sub=None,
                   trailing: Optional[QWidget] = None):
        """A surface card with a header, returning (frame, body_layout). The header is
        either a numbered eyebrow (number ● title — desc) or an uppercase lbl · sub."""
        frame = QFrame(); frame.setObjectName("calcard")
        frame.setStyleSheet(
            f"QFrame#calcard {{ background: {Palette.SURFACE}; border: 1px solid "
            f"{Palette.BORDER}; border-radius: 10px; }}")
        outer = QVBoxLayout(frame); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)

        header = QWidget(); header.setObjectName("calhdr")
        header.setStyleSheet(f"#calhdr {{ border-bottom: 1px solid {Palette.BORDER}; }}")
        hb = QHBoxLayout(header); hb.setContentsMargins(14, 11, 14, 11); hb.setSpacing(10)
        if number is not None:
            num = QLabel(str(number)); num.setFixedSize(20, 20)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(
                f"background: {Palette.ACCENT}; color: #fff; border-radius: 10px; "
                f"font-size: 11px; font-weight: 700;")
            hb.addWidget(num)
            txt = QLabel(f"<span style='color:{Palette.TEXT};font-weight:600;'>{title}</span>"
                         f" <span style='color:{Palette.TEXT_FAINT};'>{desc or ''}</span>")
            txt.setObjectName("cardtitle")
            txt.setTextFormat(Qt.TextFormat.RichText)
            hb.addWidget(txt)
        else:
            l = QLabel((lbl or "").upper())
            l.setStyleSheet(f"font-size: 11px; font-weight: 700; letter-spacing: .09em; "
                            f"color: {Palette.TEXT_FAINT};")
            hb.addWidget(l)
            if sub:
                s = QLabel(sub); s.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                hb.addWidget(s)
        hb.addStretch(1)
        if trailing is not None:
            hb.addWidget(trailing)
        outer.addWidget(header)

        content = QWidget(); body = QVBoxLayout(content)
        body.setContentsMargins(14, 12, 14, 12); body.setSpacing(10)
        outer.addWidget(content)
        return frame, body

    def _build_editor_tab(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget(); self._editor_layout = QVBoxLayout(inner)
        self._editor_layout.setContentsMargins(2, 2, 2, 2)
        self._editor_layout.setSpacing(14)
        self._selected_plane: Optional[str] = None

        intro = QLabel(
            "Build this unit's RF chain from parts you characterized once. Passive stages "
            "become pickers — choose the cable and antenna you actually wired — and their "
            "loss is evaluated at each signal's frequency.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
        self._editor_layout.addWidget(intro)

        # ── Section 2: hardware chain (a left-to-right flow of stages) ───────────
        chain_card, chain_body = self._make_card(
            number="2", title="Hardware chain",
            desc="— drop in the parts you wired this unit with")
        chain_scroll = QScrollArea(); chain_scroll.setWidgetResizable(True)
        chain_scroll.setFrameShape(QFrame.Shape.NoFrame)
        chain_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        chain_scroll.setMinimumHeight(190)
        holder = QWidget(); self._chain_row = QHBoxLayout(holder)
        self._chain_row.setContentsMargins(0, 2, 0, 2); self._chain_row.setSpacing(0)
        chain_scroll.setWidget(holder)
        chain_body.addWidget(chain_scroll)
        self._editor_layout.addWidget(chain_card)

        # ── Section 3 + signals/limits: two columns ─────────────────────────────
        cols = QHBoxLayout(); cols.setSpacing(14)
        self._detail_card, self._detail_body = self._make_card(
            number="3", title="Stage detail", desc="— pick a stage above")
        self._detail_hdr = self._detail_card.findChild(QLabel, "cardtitle")  # title, not the "3"
        cols.addWidget(self._detail_card, 3)

        rightw = QWidget(); rcol = QVBoxLayout(rightw)
        rcol.setContentsMargins(0, 0, 0, 0); rcol.setSpacing(14)
        add_sig = QPushButton("+ Add signal…"); add_sig.clicked.connect(self._on_add_signal)
        add_sig.setStyleSheet("font-size: 11px;")
        sig_card, sig_body = self._make_card(
            lbl="Signals", sub="resolved --power at each frequency", trailing=add_sig)
        sig_body.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Signal", "Freq MHz", "Ampl.", "--power dBm"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setMaximumHeight(180)
        self._table.setToolTip("Click a signal to open its measured curve for editing.")
        self._table.cellClicked.connect(self._on_signal_row_clicked)
        sig_body.addWidget(self._table)
        rcol.addWidget(sig_card)

        add_lim = QPushButton("+ Add"); add_lim.clicked.connect(lambda: (self._add_limit_row(), None))
        add_lim.setStyleSheet("font-size: 11px;")
        lim_card, lim_body = self._make_card(lbl="Limits · ceiling", trailing=add_lim)
        lim_body.setContentsMargins(0, 0, 0, 4)
        self._limits_box = QVBoxLayout(); self._limits_box.setContentsMargins(0, 0, 0, 0)
        self._limits_box.setSpacing(0)
        lim_body.addLayout(self._limits_box)
        rcol.addWidget(lim_card)
        rcol.addStretch(1)
        cols.addWidget(rightw, 2)
        self._editor_layout.addLayout(cols)

        # ── Section 1: component library ────────────────────────────────────────
        fleet = QLabel("fleet-wide · deployed to units")
        fleet.setStyleSheet(f"font-size: 10px; font-weight: 700; letter-spacing: .06em; "
                            f"color: {Palette.ACCENT}; background: {Palette.ACCENT_SOFT}; "
                            f"padding: 3px 9px; border-radius: 5px;")
        lib_card, lib_body = self._make_card(
            number="1", title="Component library",
            desc="— characterize a part once; every unit reuses it", trailing=fleet)
        self._lib_grid = QGridLayout(); self._lib_grid.setSpacing(12)
        lib_body.addLayout(self._lib_grid)
        self._editor_layout.addWidget(lib_card)

        # ── Chain settings (gains / operating / defaults — needed by the resolver) ─
        self._f["unit_type"] = QComboBox()
        for t in UNIT_TYPES:
            self._f["unit_type"].addItem(UNIT_TYPE_LABELS.get(t, t), t)
        self._f["unit_type"].setToolTip(
            "This unit's hardware type. It selects the shared type-defaults chain that's "
            "merged in, so it must match the real unit — a wrong type silently mis-resolves.")
        self._f["min_gain"] = QLineEdit(); self._f["max_gain"] = QLineEdit()
        self._f["min_gain"].setToolTip("Lowest usable SDR internal gain (dB).")
        self._f["max_gain"].setToolTip(
            "Highest SDR gain the safety ceilings allow (usually the amp's P1dB gain).")
        self._f["def_amp"] = QLineEdit()
        self._f["def_amp"].setToolTip(
            "Default baseband amplitude (0–1) a signal uses when it sets none of its own.")
        set_card, set_body = self._make_card(
            lbl="Chain settings", sub="gain range · defaults")
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Unit type", self._f["unit_type"])
        form.addRow("Min gain (dB)", self._f["min_gain"])
        form.addRow("Max gain (dB)", self._f["max_gain"])
        form.addRow("Default amplitude", self._f["def_amp"])
        set_body.addLayout(form)
        self._editor_layout.addWidget(set_card)

        # Empty-state hint / template button (shown when there's no document).
        self._empty_hint = QPushButton("New from template")
        self._empty_hint.clicked.connect(self._on_new_template)
        self._editor_layout.addWidget(self._empty_hint, alignment=Qt.AlignmentFlag.AlignLeft)
        self._editor_layout.addStretch(1)

        scroll.setWidget(inner)
        return scroll

    def _open_json(self) -> None:
        """View / edit the raw calibration document in a dialog. The editor form is the
        primary surface; this is the escape hatch for anything the form doesn't cover.
        Applying valid JSON replaces the working document and rebuilds the editor."""
        try:                                     # fold current form edits in first
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self.window())
        dlg.setWindowTitle("Calibration document · JSON")
        dlg.setMinimumSize(700, 560)
        v = QVBoxLayout(dlg); v.setSpacing(8)
        intro = QLabel("The raw calibration document. Edit here for anything the form "
                       "doesn't surface — “Apply” parses it back into the editor.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addWidget(intro)
        view = QPlainTextEdit(); view.setFont(QFont("monospace"))
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(json.dumps(self._doc, indent=2) if self._doc is not None else "")
        v.addWidget(view, 1)
        err = QLabel(""); err.setWordWrap(True)
        err.setStyleSheet(f"font-size:11px;color:{Palette.CRASH};")
        v.addWidget(err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        v.addWidget(bb)

        def _apply():
            msg = self._apply_json_text(view.toPlainText())
            if msg:
                err.setText(msg)
            else:
                dlg.accept()
        bb.accepted.connect(_apply)
        bb.rejected.connect(dlg.reject)
        dlg.exec()

    def _apply_json_text(self, text: str) -> Optional[str]:
        """Parse raw JSON into the working document and rebuild the editor. Returns an
        error message on failure (document left unchanged), or None on success."""
        text = (text or "").strip()
        if not text:
            return "the document is empty"
        try:
            doc = json.loads(text)
        except ValueError as exc:
            return f"not valid JSON: {exc}"
        self._doc = doc
        self._download_btn.setEnabled(doc is not None)
        self._doc_to_form()
        return None

    # ── model → views ────────────────────────────────────────────────────────────
    def _set_doc(self, doc: Optional[dict]) -> None:
        self._doc = doc
        self._download_btn.setEnabled(doc is not None)
        self._doc_to_form()

    def _plane_names(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return list(planes.keys())

    def _measured_planes(self):
        planes = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        return [n for n, p in planes.items() if isinstance(p, dict) and p.get("type") == "measured"]

    def _doc_to_form(self) -> None:
        """Rebuild every editor widget from the model, then render the chain flow, the
        selected-stage detail, the resolved signals table, the limits, and the component
        library. A full rebuild each time keeps widget lifetimes simple (see
        _select_plane)."""
        self._syncing = True
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

        # plane rows + signal entries: create the editable widgets (the chain/detail
        # render decides where the selected ones are shown).
        self._f["planes"] = [self._make_plane_row(n, s or {})
                             for n, s in (chain.get("planes") or {}).items()]
        self._f["signals"] = {}
        self._spark_src = {}                # sparkline → its source curve table
        measured = self._measured_planes()
        for sid, sig in ((doc or {}).get("signals") or {}).items():
            self._build_signal_entry(sid, sig or {}, measured)

        names = self._plane_names()
        op = names[-1] if names else None       # operating plane = the last stage, always

        # limits
        self._clear_layout(self._limits_box)
        self._f["limits"] = []
        for lim in (chain.get("limits") or []):
            self._add_limit_row(lim)
        if not (chain.get("limits") or []):
            hint = QLabel("no ceiling yet — add one (the unit refuses to transmit "
                          "without a safety ceiling)")
            hint.setWordWrap(True)
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};padding:10px 14px;")
            self._limits_box.addWidget(hint)

        self._syncing = False

        # keep the current selection if the plane still exists, else operating / first
        if self._selected_plane not in names:
            self._selected_plane = op if op in names else (names[0] if names else None)

        # Show the document's signals so they're always clickable (the resolved --power
        # column fills in after a Validate/Save; until then it reads "validate to resolve").
        self._populate_table({sid: {} for sid in ((doc or {}).get("signals") or {})},
                             resolved=False)

        self._render_chain()
        self._render_detail()
        self._render_library()
        self._update_issues()
        self._sync_validate_button()

    # ── representative frequency (for stage values / plots) ──────────────────────
    def _rep_freq(self) -> float:
        """A representative transmit frequency for previewing passive-hop dB: the first
        signal that declares a centre frequency, else 1.5 GHz (mid GNSS band)."""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            f = (sig or {}).get("center_freq_hz")
            if f:
                try:
                    return float(f)
                except (TypeError, ValueError):
                    pass
        return 1.5e9

    def _signal_markers(self):
        """Band markers for the frequency plot: (label, freq_hz, colour), one per distinct
        centre frequency. Signals that share a frequency are merged into a single dashed
        line with their labels combined, so overlapping signals don't stack invisibly.
        Each signal's label is its chosen plot_label, else a short form of its id."""
        cols = [Palette.ACCENT, Palette.ONLINE, Palette.ARMED, Palette.TEXT_MUTED]
        groups: dict = {}                         # rounded freq → {"freq", "labels"}
        order: list = []
        for sid, sig in sorted(((self._doc or {}).get("signals") or {}).items()):
            f = (sig or {}).get("center_freq_hz")
            if not f:
                continue
            try:
                fval = float(f)
            except (TypeError, ValueError):
                continue
            label = ((sig or {}).get("plot_label") or "").strip() \
                or (sid.split("_")[-1][:6] or sid[:6])
            key = round(fval, 3)                   # merge near-identical frequencies
            if key not in groups:
                groups[key] = {"freq": fval, "labels": []}
                order.append(key)
            groups[key]["labels"].append(label)
        out = []
        for i, key in enumerate(order):
            g = groups[key]
            labels = g["labels"]
            lab = " · ".join(labels) if len(labels) <= 2 else f"{labels[0]} +{len(labels) - 1}"
            out.append((lab, g["freq"], cols[i % len(cols)]))
        return out

    def _comp_table(self, comp_id: str):
        spec = self._catalog.get(comp_id) if comp_id else None
        return (spec or {}).get("delta_db_by_freq") or []

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
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        planes = self._doc.setdefault("chain", {}).setdefault("planes", {})
        nm, i = "plane", 1
        while nm in planes:
            i += 1; nm = f"plane{i}"
        planes[nm] = {"type": "measured"}
        self._download_btn.setEnabled(True)
        self._doc_to_form()

    def _make_plane_row(self, name: str = "", spec: Optional[dict] = None) -> dict:
        """Create (but do not place) the editable widgets for one chain stage, returning
        the row dict _read_planes reads. The chain is an ordered LINEAR sequence: a
        stage's parent is the stage before it and the operating plane is always the last
        stage, so there is no parent picker and no operating control. A stage is one of:
          • measured  — a gain→power curve measured on this box (SDR, amp),
          • component  — a library component (its Δ dB(f) fixed at add-time), or
          • constant   — an inline constant Δ dB (editable).
        The role is fixed when the stage is added; to change a stage's part, add a new
        stage and remove the old one, then drag/move it into place."""
        spec = spec or {}
        if spec.get("type") == "derived":
            role = "component" if spec.get("component") else "constant"
        else:
            role = "measured"
        name_e = QLineEdit(name); name_e.setPlaceholderText("plane id (e.g. antenna_eirp)")
        name_e.setToolTip("A short id for this stage, e.g. sdr_output, amplifier_output, "
                          "antenna_eirp. Renaming it re-points everything that references it.")
        delta_e = QLineEdit(_numstr(spec.get("delta_db"))); delta_e.setPlaceholderText("Δ dB")
        delta_e.setToolTip("Constant offset from the previous stage, in dB. Negative = "
                           "loss (cable/pad), positive = gain (antenna).")
        delta_e.editingFinished.connect(self._refresh_form_from_widgets)
        # "orig" is the plane's last-committed name, so a rename propagates to everything
        # that references it instead of silently dangling.
        row = {"name": name_e, "role": role, "comp_id": spec.get("component") or "",
               "delta": delta_e, "orig": name}
        name_e.editingFinished.connect(lambda r=row: self._on_plane_name_changed(r))
        return row

    # ── chain flow (mockup section 2) ────────────────────────────────────────────
    def _render_chain(self) -> None:
        self._clear_layout(self._chain_row)
        rows = self._f.get("planes", [])
        rep = self._rep_freq()
        n = len(rows)
        for i, row in enumerate(rows):
            self._chain_row.addWidget(self._stage_card(row, i, n, rep))
            arrow = QLabel("→")
            arrow.setStyleSheet(f"color:{Palette.BORDER_STRONG};font-size:18px;")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow.setFixedWidth(26)
            self._chain_row.addWidget(arrow)
        self._chain_row.addWidget(self._add_stage_card())
        self._chain_row.addStretch(1)

    def _add_stage_card(self) -> QWidget:
        """The dashed “+ Add stage” tile at the end of the chain."""
        card = _ClickCard(on_click=self._add_stage)
        card.setObjectName("addstage")
        card.setStyleSheet(f"#addstage {{ border:1px dashed {Palette.BORDER_STRONG}; "
                           f"border-radius:10px; }}")
        card.setMinimumWidth(150); card.setMaximumWidth(180)
        v = QVBoxLayout(card); v.setContentsMargins(12, 12, 12, 12)
        plus = QLabel("+"); plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(f"font-size:22px;color:{Palette.ACCENT};font-weight:600;")
        t = QLabel("Add stage"); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.ACCENT};")
        h = QLabel("component, measured\nor constant"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        v.addStretch(1); v.addWidget(plus); v.addWidget(t); v.addWidget(h); v.addStretch(1)
        return card

    def _stage_card(self, row, index: int, total: int, rep_freq: float) -> QWidget:
        name = row["name"].text().strip()
        role = row.get("role", "measured")
        kind = "source" if index == 0 else ("passive" if role != "measured" else "measured")
        selected = (name == self._selected_plane)
        operating = (index == total - 1)          # operating plane = last stage, always
        border = (Palette.ONLINE if operating else
                  Palette.ACCENT if selected else Palette.BORDER)
        bg = Palette.SURFACE if (operating or selected) else Palette.SURFACE_ALT
        # Non-source stages are drop targets (drag another stage onto them to reorder).
        card = _ClickCard(on_click=lambda n=name: self._select_plane(n),
                          on_drop=(self._reorder_stage if index > 0 else None),
                          drop_name=name)
        card.setObjectName("stage")
        card.setStyleSheet(f"#stage {{ background:{bg}; border:1px solid {border}; "
                           f"border-radius:10px; }}")
        card.setMinimumWidth(178); card.setMaximumWidth(230)
        v = QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 12); v.setSpacing(7)

        # top row: drag grip + operating badge + move ◀▶ handles (none on the source)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        if index > 0:
            top.addWidget(_DragHandle(name))
        if operating:
            opb = QLabel("--power reads here")
            opb.setStyleSheet(f"color:#fff;background:{Palette.ONLINE};font-size:10px;"
                              f"font-weight:700;padding:2px 8px;border-radius:999px;")
            top.addWidget(opb)
        top.addStretch(1)
        if index > 0:                             # the source stage stays first
            for glyph, delta, en in (("◀", -1, index > 1), ("▶", +1, index < total - 1)):
                mv = QPushButton(glyph); mv.setFixedSize(20, 20)
                mv.setFocusPolicy(Qt.FocusPolicy.NoFocus); mv.setEnabled(en)
                mv.setStyleSheet("font-size:10px;padding:0;")
                mv.setToolTip("Move this stage earlier" if delta < 0 else "Move this stage later")
                mv.clicked.connect(lambda _=False, r=row, d=delta: self._move_stage(r, d))
                top.addWidget(mv)
        v.addLayout(top)

        fg, kbg = _KIND_COLORS[kind]
        badge_text = {"source": "SOURCE", "measured": "MEASURED",
                      "passive": "Passive · from library"}[kind]
        v.addWidget(_badge(badge_text, fg, kbg), alignment=Qt.AlignmentFlag.AlignLeft)

        title = QLabel(name or "(unnamed)")
        title.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.TEXT};")
        v.addWidget(title)

        if role == "component":
            comp = row.get("comp_id", "")
            cn = QLabel(comp or "(missing component)")
            cn.setStyleSheet(f"font-size:12px;font-weight:500;color:{Palette.TEXT};")
            cn.setWordWrap(True)
            v.addWidget(cn)
            db = _interp_db(self._comp_table(comp), rep_freq)
            val = QLabel(f"{db:+.2f} dB  @ {rep_freq/1e6:.0f} MHz")
            val.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            v.addWidget(val)
        elif role == "constant":
            d = row["delta"].text().strip()
            val = QLabel(f"{d or '0'} dB · constant")
            val.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            v.addWidget(val)
        else:
            spark = _Sparkline(); spark.setFixedHeight(30)
            spark.set_points(self._first_curve_points(name))
            v.addWidget(spark)
            hint = QLabel("gain → power · this unit")
            hint.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            v.addWidget(hint)
        v.addStretch(1)
        return card

    # ── add / reorder stages ─────────────────────────────────────────────────────
    def _add_stage(self) -> None:
        """Add a stage to the end of the chain — a library component, a fresh measured
        plane, or a constant Δ dB. (Reorder with the ◀▶ handles.)"""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("Component from library…", lambda: self._add_component_stage())
        menu.addAction("Measured plane…", lambda: self._add_measured_stage())
        menu.addAction("Constant Δ dB…", lambda: self._add_constant_stage())
        menu.exec(self.cursor().pos())

    def _prepare_add(self):
        """Fold current edits into the model and return the planes dict to append to."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        self._download_btn.setEnabled(True)
        return self._doc.setdefault("chain", {}).setdefault("planes", {})

    def _unique_plane_id(self, base: str, planes) -> str:
        base = base or "stage"
        nm, i = base, 1
        while nm in planes:
            i += 1; nm = f"{base}_{i}"
        return nm

    def _add_component_stage(self) -> None:
        ids = self._catalog.ids()
        if not ids:
            QMessageBox.information(self, "Add component stage",
                                   "No components yet — characterize a cable/antenna in the "
                                   "Component library first.")
            self._open_components()
            return
        cid, ok = QInputDialog.getItem(self, "Add component stage",
                                       "Component (from the library):", ids, 0, False)
        if not ok or not cid:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(cid, planes)
        planes[name] = {"type": "derived", "from": "", "component": cid}
        self._selected_plane = name
        self._doc_to_form()

    def _add_measured_stage(self) -> None:
        name, ok = QInputDialog.getText(self, "Add measured plane",
                                        "Plane id (e.g. amplifier_output):")
        name = (name or "").strip()
        if not ok or not name:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(name, planes)
        planes[name] = {"type": "measured"}
        self._selected_plane = name
        self._doc_to_form()

    def _add_constant_stage(self) -> None:
        name, ok = QInputDialog.getText(self, "Add constant Δ dB stage",
                                        "Plane id (e.g. cable_loss):")
        name = (name or "").strip()
        if not ok or not name:
            return
        planes = self._prepare_add()
        name = self._unique_plane_id(name, planes)
        planes[name] = {"type": "derived", "from": "", "delta_db": 0.0}
        self._selected_plane = name
        self._doc_to_form()

    def _move_stage(self, row, delta: int) -> None:
        """Swap a stage one place earlier/later. The source (index 0) is fixed and nothing
        can move into its slot. `from`/operating are recomputed from the new order."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        names = list(planes.keys())
        name = row["name"].text().strip()
        if name not in names:
            return
        i = names.index(name); j = i + delta
        if i == 0 or j < 1 or j >= len(names):    # keep the source first; stay in range
            return
        names[i], names[j] = names[j], names[i]
        self._doc["chain"]["planes"] = {nm: planes[nm] for nm in names}
        self._selected_plane = name
        self._doc_to_form()

    def _reorder_stage(self, src: str, dst: str) -> None:
        """Drag-and-drop reorder: move stage `src` to `dst`'s slot. The source stage
        (index 0) is pinned and nothing may take slot 0, so the linear chain always keeps
        its measured source first; the operating plane stays the last stage."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        planes = (self._doc or {}).get("chain", {}).get("planes") or {}
        names = list(planes.keys())
        if src not in names or dst not in names or src == dst:
            return
        i, j = names.index(src), names.index(dst)
        if i == 0 or j == 0:                       # never move the source, never displace it
            return
        names.pop(i)
        j = names.index(dst)                       # dst's index after removing src
        names.insert(j if i > j else j + 1, src)   # before dst moving left, after moving right
        self._doc["chain"]["planes"] = {nm: planes[nm] for nm in names}
        self._selected_plane = src
        self._doc_to_form()

    def _first_curve_points(self, plane: str):
        """The first signal's measured points on `plane`, for a stage minicurve."""
        for sig in ((self._doc or {}).get("signals") or {}).values():
            pts = ((sig or {}).get("curves") or {}).get(plane, {}).get("points")
            if pts:
                return [(p.get("gain_db"), p.get("power_dbm")) for p in pts]
        return []

    def _select_plane(self, name: str) -> None:
        """Select a chain stage: fold the current edits into the model, then rebuild so
        the detail pane and stage borders follow the selection."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        self._selected_plane = name or None
        self._doc_to_form()

    def _on_signal_row_clicked(self, r: int, _c: int) -> None:
        item = self._table.item(r, 0)
        if item is not None:
            self._select_signal(item.text().strip())

    def _select_signal(self, sid: str) -> None:
        """Clicking a signal opens its measured curve: select the measured stage that
        carries it (the source, or the first measured plane) and expand just that signal
        in the stage detail so it's ready to edit."""
        try:
            self._doc = self._read_form(strict=False)
        except ValueError:
            pass
        plane = None
        measured = []
        for row in self._f.get("planes", []):
            if row.get("role") == "measured":
                nm = row["name"].text().strip()
                measured.append(nm)
                entry = self._f.get("signals", {}).get(sid)
                if plane is None and entry and nm in (entry.get("curves") or {}):
                    plane = nm
        self._selected_plane = plane or (measured[0] if measured else self._selected_plane)
        self._expanded_signals = {sid}
        self._doc_to_form()

    # ── stage detail (mockup section 3) ──────────────────────────────────────────
    def _render_detail(self) -> None:
        self._clear_layout(self._detail_body)
        name = self._selected_plane
        rows = self._f.get("planes", [])
        row = next((r for r in rows if r["name"].text().strip() == name), None)
        if self._detail_hdr is not None:
            if row is None:
                self._detail_hdr.setText(
                    f"<span style='color:{Palette.TEXT};font-weight:600;'>Stage detail</span>"
                    f" <span style='color:{Palette.TEXT_FAINT};'>— pick a stage above</span>")
            else:
                idx = rows.index(row)
                role = row.get("role", "measured")
                knd = ("Source" if idx == 0 else
                       "Measured" if role == "measured" else "Passive")
                desc = ("measured gain→power on this box" if role == "measured"
                        else "loss evaluated at each signal's frequency")
                self._detail_hdr.setText(
                    f"<span style='color:{Palette.TEXT};font-weight:600;'>{knd} · {name}</span>"
                    f" <span style='color:{Palette.TEXT_FAINT};'>— {desc}</span>")
        if row is None:
            ph = QLabel("Select a stage in the chain above to edit it.")
            ph.setStyleSheet(f"color:{Palette.TEXT_FAINT};font-size:12px;padding:16px 0;")
            self._detail_body.addWidget(ph)
            return
        if row.get("role", "measured") == "measured":
            self._detail_measured(row)
        else:
            self._detail_passive(row)
        self._detail_body.addWidget(self._stage_advanced(row))

    def _detail_passive(self, row) -> None:
        """Detail for a passive stage. A component stage shows its library part read-only —
        its Δ dB(f) sweep plotted, with an “edit in library” shortcut — because to change a
        stage's part you add a new stage and drop the old one. A constant stage shows an
        editable Δ dB field."""
        if row.get("role") == "component":
            comp = row.get("comp_id", "")
            table = self._comp_table(comp)
            markers = self._signal_markers()
            if comp and table:
                plot = _FreqResponsePlot()
                plot.set_data(table, markers)
                self._detail_body.addWidget(plot)
                leg = QHBoxLayout(); leg.setSpacing(16)
                sw = QLabel("● VNA sweep (measured points)")
                sw.setStyleSheet(f"font-size:11px;color:{Palette.ACCENT};")
                leg.addWidget(sw)
                for label, freq, color in markers:
                    db = _interp_db(table, freq)
                    l = QLabel(f"{label} → {db:+.2f} dB")
                    l.setStyleSheet(f"font-size:11px;color:{color};")
                    leg.addWidget(l)
                leg.addStretch(1)
                self._detail_body.addLayout(leg)
            pr = QHBoxLayout()
            lab = QLabel("Component")
            lab.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            val = QLabel(comp or "(missing component)")
            val.setStyleSheet(f"font-size:12px;font-weight:600;color:{Palette.TEXT};")
            edit = QPushButton("Edit in library…")
            edit.setStyleSheet("font-size:11px;")
            edit.clicked.connect(lambda _=False, c=comp: self._open_components(c or None))
            pr.addWidget(lab); pr.addWidget(val); pr.addStretch(1); pr.addWidget(edit)
            self._detail_body.addLayout(pr)
            note = QLabel("Characterized once in the Component library · shared across the "
                          "fleet. To swap the part, add a new stage and remove this one — "
                          "then drag it into place.")
        else:                                          # constant Δ dB stage
            dr = QHBoxLayout()
            dl = QLabel("Constant Δ dB")
            dl.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
            dr.addWidget(dl); row["delta"].setFixedWidth(90); dr.addWidget(row["delta"])
            dr.addStretch(1)
            self._detail_body.addLayout(dr)
            note = QLabel("A fixed, frequency-independent offset from the previous stage "
                          "(negative = loss, positive = gain).")
        note.setWordWrap(True)
        note.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        self._detail_body.addWidget(note)

    def _detail_measured(self, row) -> None:
        plane = row["name"].text().strip()
        sigs = {sid: e for sid, e in self._f.get("signals", {}).items()
                if e["curves"].get(plane) is not None}
        if not sigs:
            l = QLabel("No signals yet — “+ Add signal…” (right), then enter its measured "
                       "gain→power points here.")
            l.setWordWrap(True); l.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_FAINT};")
            self._detail_body.addWidget(l)
            return
        head = QHBoxLayout()
        intro = QLabel("Enter the gain→power points you measured on this plane, per signal. "
                       "Click a signal to expand it.")
        intro.setWordWrap(True); intro.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        head.addWidget(intro, 1)
        # expand/collapse-all when there are enough signals to be worth it
        if len(sigs) > 1:
            all_open = self._expanded_signals.issuperset(sigs)
            toggle_all = QPushButton("Collapse all" if all_open else "Expand all")
            toggle_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            toggle_all.setStyleSheet("font-size:11px;")
            toggle_all.clicked.connect(
                lambda _=False, s=set(sigs), o=all_open: self._toggle_all_signals(s, o))
            head.addWidget(toggle_all)
        self._detail_body.addLayout(head)
        for sid, entry in sigs.items():
            self._detail_body.addWidget(self._signal_section(sid, entry, plane))

    def _signal_section(self, sid: str, entry: dict, plane: str) -> QWidget:
        """One collapsible signal in the measured-stage detail: a header that toggles,
        and (when expanded) its amplitude/frequency + the gain→power grid."""
        tbl = entry["curves"][plane]
        expanded = sid in self._expanded_signals
        box = QFrame(); box.setObjectName("sigbox")
        box.setStyleSheet(f"#sigbox {{ border:1px solid "
                          f"{Palette.ACCENT if expanded else Palette.BORDER}; border-radius:8px; }}")
        bv = QVBoxLayout(box); bv.setContentsMargins(10, 8, 10, 8); bv.setSpacing(6)
        header = _ClickCard(on_click=lambda s=sid: self._toggle_signal(s))
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        hh = QHBoxLayout(header); hh.setContentsMargins(0, 0, 0, 0)
        chev = QLabel("▾" if expanded else "▸")
        chev.setStyleSheet(f"font-size:12px;color:{Palette.TEXT_MUTED};")
        nm = QLabel(sid); nm.setStyleSheet(f"font-weight:600;color:{Palette.TEXT};")
        hh.addWidget(chev); hh.addWidget(nm); hh.addStretch(1)
        if not expanded:                                 # a compact summary while collapsed
            npts = len(tbl.numeric_points())
            summ = QLabel(f"{npts} point(s)" if npts else "no points yet")
            summ.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
            hh.addWidget(summ)
        bv.addWidget(header)
        if not expanded:
            return box
        sub = QHBoxLayout()
        sub.addWidget(QLabel("plot label")); entry["plabel"].setFixedWidth(90)
        sub.addWidget(entry["plabel"])
        sub.addStretch(1)
        sub.addWidget(QLabel("ampl.")); entry["amp"].setFixedWidth(64); sub.addWidget(entry["amp"])
        sub.addWidget(QLabel("freq Hz")); entry["cfreq"].setFixedWidth(104); sub.addWidget(entry["cfreq"])
        bv.addLayout(sub)
        grid = QHBoxLayout()
        grid.addWidget(tbl, 3); grid.addWidget(entry["sparks"][plane], 2)
        bv.addLayout(grid)
        btns = QHBoxLayout()
        addp = QPushButton("+ point"); addp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        addp.clicked.connect(tbl.add_blank_row)
        rmp = QPushButton("− point"); rmp.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rmp.clicked.connect(tbl.remove_selected)
        btns.addWidget(addp); btns.addWidget(rmp); btns.addStretch(1)
        bv.addLayout(btns)
        return box

    def _toggle_signal(self, sid: str) -> None:
        self._expanded_signals ^= {sid}          # flip membership
        self._refresh_form_from_widgets()        # rebuild, preserving committed edits

    def _toggle_all_signals(self, sids: set, currently_all_open: bool) -> None:
        if currently_all_open:
            self._expanded_signals -= sids
        else:
            self._expanded_signals |= sids
        self._refresh_form_from_widgets()

    def _stage_advanced(self, row) -> QWidget:
        """The bits the chain flow doesn't show inline: the stage's id (renaming it
        re-points everything that references it) and a remove action. The stage's role,
        parent and operating status are all implied by its position in the linear chain,
        so there's nothing else to set here."""
        frame = QFrame(); frame.setObjectName("adv")
        frame.setStyleSheet(f"#adv {{ border-top:1px solid {Palette.BORDER}; }}")
        v = QVBoxLayout(frame); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(6)
        cap = QLabel("STAGE SETTINGS")
        cap.setStyleSheet(f"font-size:10px;font-weight:700;letter-spacing:.08em;"
                          f"color:{Palette.TEXT_FAINT};")
        v.addWidget(cap)
        form = QFormLayout(); form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Plane id", row["name"])
        v.addLayout(form)
        actions = QHBoxLayout()
        rm = QPushButton("Remove stage")
        # NB: clicked emits a `checked` bool — absorb it with a leading throwaway
        # parameter, or it clobbers the r=row default (r would become False).
        rm.clicked.connect(lambda _=False, r=row: self._remove_plane(r))
        actions.addStretch(1); actions.addWidget(rm)
        v.addLayout(actions)
        return frame

    # ── component library (mockup section 1) ─────────────────────────────────────
    def _render_library(self) -> None:
        while self._lib_grid.count():
            it = self._lib_grid.takeAt(0); w = it.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        used = {p.get("component") for p in
                (((self._doc or {}).get("chain") or {}).get("planes") or {}).values()
                if isinstance(p, dict)}
        comps = self._catalog.components()
        cols = 4
        idx = 0
        for cid in self._catalog.ids():
            self._lib_grid.addWidget(
                self._component_card(cid, comps.get(cid) or {}, cid in used), idx // cols, idx % cols)
            idx += 1
        add = _ClickCard(on_click=self._open_components)
        add.setObjectName("addcomp")
        add.setStyleSheet(f"#addcomp {{ border:1px dashed {Palette.BORDER_STRONG}; "
                          f"border-radius:9px; }}")
        av = QVBoxLayout(add); av.setContentsMargins(11, 16, 11, 16); av.setSpacing(4)
        plus = QLabel("+"); plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setStyleSheet(f"font-size:22px;color:{Palette.ACCENT};font-weight:600;")
        t = QLabel("Characterize component"); t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.ACCENT};")
        h = QLabel("paste a VNA sweep"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size:11px;color:{Palette.TEXT_FAINT};")
        av.addStretch(1); av.addWidget(plus); av.addWidget(t); av.addWidget(h); av.addStretch(1)
        self._lib_grid.addWidget(add, idx // cols, idx % cols)

    def _component_card(self, cid: str, spec: dict, in_chain: bool) -> QWidget:
        kind = (spec.get("kind") or "cable").lower()
        table = spec.get("delta_db_by_freq") or []
        desc = spec.get("description") or ""
        card = _ClickCard(on_click=lambda c=cid: self._open_components(c))
        card.setObjectName("comp")
        card.setStyleSheet(f"#comp {{ background:{Palette.SURFACE_ALT}; "
                           f"border:1px solid {Palette.BORDER}; border-radius:9px; }}")
        v = QVBoxLayout(card); v.setContentsMargins(11, 11, 11, 11); v.setSpacing(8)
        top = QHBoxLayout()
        nm = QLabel(desc or cid); nm.setStyleSheet(f"font-size:13px;font-weight:600;color:{Palette.TEXT};")
        nm.setWordWrap(True)
        top.addWidget(nm, 1)
        fg, bg = _KIND_COLORS.get(kind, _KIND_COLORS["pad"])
        top.addWidget(_badge(kind.capitalize(), fg, bg))
        v.addLayout(top)
        if desc:
            sub = QLabel(cid); sub.setStyleSheet(
                f"font-family:monospace;font-size:10px;color:{Palette.TEXT_FAINT};")
            v.addWidget(sub)
        spark = _FreqSparkline(40); spark.set_table(table, _KIND_COLORS.get(kind, (Palette.ACCENT,))[0])
        v.addWidget(spark)
        foot = QHBoxLayout()
        span = QLabel(_fmt_ghz_span(table))
        span.setStyleSheet(f"font-family:monospace;font-size:10px;color:{Palette.TEXT_MUTED};")
        foot.addWidget(span); foot.addStretch(1)
        tag = QLabel("in this chain" if in_chain else "in library")
        tag.setStyleSheet(f"font-size:10px;color:{Palette.TEXT_FAINT};")
        foot.addWidget(tag)
        v.addLayout(foot)
        return card

    def _read_planes(self, strict: bool) -> dict:
        """Build the planes dict from the ordered stage rows. The chain is LINEAR: each
        derived stage's parent (`from`) is the stage immediately before it, so two stages
        can never share a parent and there's no dangling reference. Preserves any stored
        description/quantity (the editor no longer surfaces quantity, but doesn't drop
        it either)."""
        prev = ((self._doc or {}).get("chain") or {}).get("planes") or {}
        planes: dict = {}
        prev_name: Optional[str] = None
        for row in self._f.get("planes", []):
            name = row["name"].text().strip()
            if not name:
                continue
            role = row.get("role", "measured")
            if role == "measured":
                p = {"type": "measured"}
            elif role == "component":
                p = {"type": "derived", "from": prev_name or "", "component": row.get("comp_id", "")}
            else:                                     # constant Δ dB
                p = {"type": "derived", "from": prev_name or ""}
                d = row["delta"].text().strip()
                if d:
                    p["delta_db"] = _to_float(d, f"stage '{name}' Δ dB")
                elif strict:
                    raise ValueError(f"stage '{name}' has no Δ dB")
                else:
                    p["delta_db"] = 0.0
            old = prev.get(name)
            if isinstance(old, dict):
                for k in ("description", "quantity"):
                    if old.get(k):
                        p[k] = old[k]
            planes[name] = p
            prev_name = name
        return planes

    def _remove_plane(self, row) -> None:
        try:
            self._sync_from(strict=False)
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

    def _build_signal_entry(self, sid: str, sig: dict, measured) -> None:
        """Create (but do not place) a signal's editable widgets: amplitude, occupied
        BW, centre frequency, and a curve grid + sparkline per measured plane. The
        measured-stage detail places the ones for the selected plane; _read_form reads
        them all regardless of placement."""
        amp = QLineEdit(_numstr(sig.get("amplitude")))
        def_amp = ((self._doc or {}).get("defaults") or {}).get("amplitude")
        if def_amp is not None:
            amp.setPlaceholderText(f"inherits ({_numstr(def_amp)})")
        amp.setToolTip("Baseband amplitude (0–1) for this signal — must match the "
                       "amplitude used while measuring the curve. Blank = inherit the "
                       "chain default.")
        amp.editingFinished.connect(self._update_issues)
        bw = QLineEdit(_numstr(sig.get("occupied_bw_hz")))
        bw.setToolTip("Occupied bandwidth (Hz), optional.")
        cfreq = QLineEdit(_numstr(sig.get("center_freq_hz")))
        cfreq.setPlaceholderText("Hz — required for a frequency-dependent chain")
        cfreq.setToolTip("Centre frequency (Hz) at which this signal's chain is evaluated "
                         "for the --power bounds. Required when a cable/antenna is "
                         "frequency-dependent; blank for a chirp (many frequencies) or a "
                         "flat chain.")
        cfreq.editingFinished.connect(self._refresh_form_from_widgets)
        plabel = QLineEdit(sig.get("plot_label", ""))
        plabel.setPlaceholderText(sid)
        plabel.setToolTip("The label drawn on the frequency-response plot's dashed line "
                          "for this signal. Blank = a short form of the signal id.")
        plabel.editingFinished.connect(self._refresh_form_from_widgets)

        curves = {}; sparks = {}
        for plane in measured:
            spark = _Sparkline()
            tbl = _CurveTable(on_changed=lambda t=None, s=spark: self._on_curve_changed(s))
            tbl.set_points(((sig.get("curves") or {}).get(plane) or {}).get("points"))
            spark.set_points(tbl.numeric_points())
            self._spark_src[spark] = tbl
            curves[plane] = tbl; sparks[plane] = spark
        self._f["signals"][sid] = {"amp": amp, "bw": bw, "cfreq": cfreq,
                                   "plabel": plabel, "curves": curves, "sparks": sparks}

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
        chain["planes"] = self._read_planes(strict)
        # The operating plane is ALWAYS the last stage in the chain (that's where --power
        # is delivered), so it's derived from the order, never set by hand.
        names = list(chain["planes"].keys())
        if names:
            chain["operating_plane"] = names[-1]

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
            if w["cfreq"].text().strip():
                sig["center_freq_hz"] = _to_float(w["cfreq"].text(), f"{sid} centre freq")
            else:
                sig.pop("center_freq_hz", None)
            if w["plabel"].text().strip():
                sig["plot_label"] = w["plabel"].text().strip()
            else:
                sig.pop("plot_label", None)
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

    def _sync_from(self, strict: bool) -> None:
        """Pull the editor form's contents into self._doc. (The JSON view is a separate
        apply-on-close dialog — see _open_json — so the form is always the live model.)"""
        self._doc = self._read_form(strict)

    # ── refresh / load ──────────────────────────────────────────────────────────
    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"cal_get:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )
        # Learn this unit's component catalog once, so a fresh client sees the parts
        # already deployed and the chain pickers can resolve existing references.
        if not self._components_synced:
            self._components_synced = True
            self.hub.run_async(
                f"cal_components:{self.hostname}",
                lambda: self.hub.fleet.get(self.hostname).get_components())

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
            self._sync_from(strict=True)
        except ValueError as exc:
            self._set_status(f"cannot save: {exc}", kind="error")
            return
        if self._doc is None:
            self._set_status("nothing to save", kind="error")
            return
        if self._blocks_on_components():
            return
        self._send(json.dumps(self._doc).encode("utf-8"))

    @staticmethod
    def _doc_uses_components(doc) -> bool:
        return any(isinstance(p, dict) and p.get("component")
                   for p in (((doc or {}).get("chain") or {}).get("planes") or {}).values())

    def _blocks_on_components(self) -> bool:
        """Guard: don't push a component-referencing document to an agent that can't
        resolve it (it would reject the derived plane confusingly). Returns True (and
        shows why) when blocked."""
        if self._doc_uses_components(self._doc) and not self._supports(CAL_COMPONENTS_CAPABILITY):
            self._set_status(_COMPONENTS_NEEDS_NEWER, kind="error")
            return True
        return False

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
        # Parse the form the SAME way Save does (strict). A non-strict read silently
        # drops malformed input — e.g. text typed into a numeric cell — so the doc
        # would validate "clean" and then fail on Save. Surface those errors here
        # instead, before any dry-run against the unit.
        try:
            self._sync_from(strict=True)
        except ValueError as exc:
            self._update_issues()          # reflect what local checks can see too
            self._set_status(f"invalid — would fail to save: {exc}", kind="error")
            return
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
        if self._blocks_on_components():
            return
        self._set_status("validating (dry run — not saving)…")
        doc = self._doc
        wire = self._catalog.to_wire()
        host = self.hostname

        def _do():
            client = self.hub.fleet.get(host)
            client.upload_components(wire)       # so component refs resolve in the dry-run
            return client.validate_calibration(doc)
        self.hub.run_async(f"cal_validate:{host}", _do)

    def _send(self, content: bytes) -> None:
        self._set_status("validating + saving…")
        wire = self._catalog.to_wire()
        host = self.hostname

        def _do():
            client = self.hub.fleet.get(host)
            client.upload_components(wire)       # push the catalog first so refs resolve
            return client.upload_file(CAL_NAME, content)
        self.hub.run_async(f"cal_save:{host}", _do)

    # ── Component library ────────────────────────────────────────────────────────
    def _open_components(self, select: Optional[str] = None) -> None:
        """Open the shared component library, optionally on a specific component. A
        rename inside the dialog is applied to this unit's chain references too, so a
        renamed part doesn't dangle. Refresh the chain afterward."""
        try:
            self._sync_from(strict=False)
        except ValueError:
            pass
        from .component_library_dialog import ComponentLibraryDialog
        dlg = ComponentLibraryDialog(self._catalog, parent=self.window(),
                                     select=select if isinstance(select, str) else None)
        dlg.exec()
        for old, new in dlg.renames.items():          # re-point chain references
            for p in (((self._doc or {}).get("chain") or {}).get("planes") or {}).values():
                if isinstance(p, dict) and p.get("component") == old:
                    p["component"] = new
        self._doc_to_form()                  # rebuild the chain with the updated catalog

    def _on_download(self) -> None:
        try:
            self._sync_from(strict=False)
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
        elif parts[0] == "cal_components":
            self._handle_components(result)

    def _handle_components(self, result) -> None:
        """Merge the unit's stored catalog into the local one (additive — never clobbers
        a locally-authored component), then refresh the chain pickers."""
        if isinstance(result, Exception) or not isinstance(result, str) or not result.strip():
            return
        try:
            from state import ComponentCatalog
            added = self._catalog.merge(ComponentCatalog.parse_wire(result))
        except Exception:  # noqa: BLE001 — a broken unit catalog shouldn't break the panel
            return
        if added:
            self._doc_to_form()                  # so the new components appear in pickers

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
            self._sync_from(strict=False)
        except ValueError:
            pass
        if self._doc is None:
            self._doc = self._blank_doc()
        self._doc.setdefault("signals", {})[sid] = {"curves": {}}
        self._download_btn.setEnabled(True)
        self._doc_to_form()

    def _remove_signal(self, sid: str) -> None:
        try:
            self._sync_from(strict=False)
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
                w.hide()               # so it can't briefly paint as an orphan before…
                w.setParent(None)
                w.deleteLater()        # …deleteLater runs on the event loop
            else:
                child = item.layout()
                if child is not None:
                    CalibrationPanel._clear_layout(child)

    def _populate_table(self, signals: dict, resolved: bool = True) -> None:
        """Fill the resolved Signals table (Signal | Freq | Ampl. | --power range). Freq
        and amplitude come from the document; the --power range from the resolver.
        `resolved=False` (the plain editor view, before a Validate/Save) shows a
        "validate to resolve" placeholder for the --power column instead."""
        doc_sigs = (self._doc or {}).get("signals") or {}
        def_amp = ((self._doc or {}).get("defaults") or {}).get("amplitude")
        self._table.setRowCount(len(signals))
        for r, (sid, info) in enumerate(sorted(signals.items())):
            dsig = doc_sigs.get(sid) or {}
            f = dsig.get("center_freq_hz")
            try:
                freq = f"{float(f)/1e6:.2f}" if f else "at run"
            except (TypeError, ValueError):
                freq = "at run"
            amp = dsig.get("amplitude", def_amp)
            ampl = _numstr(amp) if amp is not None else "—"
            rng = _fmt_range(info.get("min_power_dbm"), info.get("max_power_dbm"), "").strip()
            if not resolved:                             # plain editor view, pre-validate
                rng = "validate to resolve"
            elif rng in ("—", "") and not f:
                rng = "per frequency"
            for c, text in enumerate([sid, freq, ampl, rng or "—"]):
                item = QTableWidgetItem(str(text))
                if c == 0:
                    item.setToolTip("Click to edit this signal's measured curve.")
                self._table.setItem(r, c, item)

    def _set_status(self, text: str, kind: str = "muted") -> None:
        color = {"ok": Palette.ONLINE, "warn": Palette.ARMED, "error": Palette.CRASH,
                 "faint": Palette.TEXT_FAINT, "muted": Palette.TEXT_MUTED}.get(kind, Palette.TEXT_MUTED)
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 12px; color: {color};")
