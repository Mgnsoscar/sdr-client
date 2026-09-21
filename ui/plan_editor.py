"""
Plan editor — the sequence editor with one more level of hierarchy.

A plan lays every sequence of every unit out on ONE timeline (the mockup the owner approved,
docs/plan-editor-mockup.html):

  UNITS are bands; each of a unit's sequences is a NEUTRAL FRAME (a slate pill when collapsed, a
  slim window bar over its expanded steps) whose left edge is its own on-air (green) and right
  edge its own off-air (red). Expanded, a sequence shows its steps EXACTLY as the sequence editor
  does — the real `_TimelineCanvas` + `_RowHeader` are embedded per sequence (`_EmbeddedCanvas`),
  so colour-by-task, the readout chips, ramps, drag-to-anchor, the context menus, the step / ramp
  editors and undo all come from one code path. Warm-up / cool-down outside a sequence's window
  are hatched stubs. Steps are edited in place (double-click / right-click, or the toolbar's step
  tools on the selected sequence).

  ANYTHING ANCHORS TO ANYTHING: a sequence's on-air / off-air can hang off the plan's own anchors,
  another sequence's edge (any unit) or a step's edge (any unit); a step can hang off a step or a
  sequence edge in another item. Drag an edge dot onto another handle to anchor; the connector is
  drawn with the sequence editor's own router (`_connector_points` / `_ortho_path` / the arrowhead
  + inline offset chip), so cross-sequence anchor lines look exactly like in-sequence ones.

  Timing resolves through ui/plan_graph.py onto the plan's two clocks (on-air forward, off-air
  backward; a hatched "relative" middle whose length is set at arm). Two sequences of one unit
  that overlap are a CHANNEL CONFLICT (red outline + a warning pill; a unit has one TX channel).
  Folded units pack their sequences into sub-lanes. Unit banners stay visible while scrolling.

Holds are deferred in the plan editor (owner: "wait with holds"): a Hold in a plan-local sequence
draws as a plain divider and the arm paths still compile it out / honour it exactly as before.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import yaml

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
                         QCursor)
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QToolTip, QVBoxLayout, QWidget,
)

from api import models as m
from api.fleet import LIBRARY_HOST
from . import plan_graph as pg
from . import timeline_model as tlm
from .qt_adapter import DataHub
from .theme import Fonts, Palette, mono_font
from .widgets import fit_dialog_to_screen
from .timeline_editor import (
    _RowHeader, _TimelineCanvas, TimelineEditor, DRAG_THRESHOLD, LANES_TOP, LANE_H, AXIS_GAP,
    ZOOM_MIN, ZOOM_MAX, ZOOM_WHEEL, _tool_icon, task_signals_from_yaml,
)

# ── Stage geometry ────────────────────────────────────────────────────────────
PLAN_HDR_W = 250            # the plan's row-header column (units · sequences · steps)
STAGE_TOP = 24              # y of the first row on the stage
ROW_UNIT_H = 34             # a unit band
ROW_SEQC_H = 46             # a collapsed sequence (pill + mini strip)
ROW_SEQX_H = 30             # an expanded sequence's slim window bar row
ROW_GAP = 6
PAD = 34                    # empty stage margin beyond the pre-roll / cool-down
WARM_S = 60.0               # pre-roll before the plan's on-air, cool-down after its off-air (s)
COOL_S = 60.0
HATCH_PX = 150              # the relative middle's fixed drawn width (its length is set at arm)
MIN_EAR = 10                # min width of a warm-up / cool-down stub outside a sequence's window
AXIS_H = 34                 # the plan axis under the rows
HANDLE_R = 6                # a sequence edge dot's radius
HANDLE_HIT = 9
PILL_MIN_W = 60
SEQ_INK = "#5C6675"         # the neutral sequence frame (never a task hue)
SEQ_FRAME = "#8A93A2"
_RECOVERY_CHOICES = [
    ("Inherit the sequence's policy", "", ""),
    ("On RF fault: operator restart", "manual", "resync"),
    ("On RF fault: auto-restart (resync)", "auto", "resync"),
    ("On RF fault: auto-restart (replay)", "auto", "replay"),
]


def _parse_task_commands(yaml_text) -> Dict[str, List[str]]:
    """task_name -> command list, parsed from a tasks.yaml document."""
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return {}
    try:
        doc = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}
    out: Dict[str, List[str]] = {}
    for entry in (doc.get("tasks") or []):
        name = entry.get("name")
        cmd = entry.get("command")
        if name and isinstance(cmd, list):
            out[name] = list(cmd)
    return out


def _load_unit_meta(timeline: TimelineEditor, hub, hostname: str) -> None:
    """Feed a step editor from the shared LIBRARY, not the selected unit: every unit runs the
    same library tasks/scripts (they differ only in the parameters this plan sets), so the task
    list, their commands and each script's parameter schema come from the library — a plan can be
    authored with no unit connected. The unit only decides which calibration governs absolute
    power (the unit this copy will arm on)."""
    timeline.set_context(hub, LIBRARY_HOST)
    timeline.set_calibration(hub, hostname)
    try:
        lib = hub.fleet.get(LIBRARY_HOST)
    except Exception:  # noqa: BLE001 — no library ⇒ author with none
        timeline.set_tasks([]); timeline.set_task_commands({}); timeline.set_task_signals({})
        return
    try:
        names = [t.name for t in lib.list_tasks()]
    except Exception:  # noqa: BLE001 — an empty library still authors
        names = []
    timeline.set_tasks(names)
    try:
        yaml_text = lib.get_tasks_yaml()
    except Exception:  # noqa: BLE001
        yaml_text = ""
    timeline.set_task_commands(_parse_task_commands(yaml_text))
    timeline.set_task_signals(task_signals_from_yaml(yaml_text))


def _unit_label(hub, hostname: str, fallback: str = "") -> str:
    try:
        return getattr(hub.fleet.get(hostname), "label", None) or fallback or hostname
    except Exception:  # noqa: BLE001
        return fallback or hostname


def _mmss(t: float) -> str:
    sign = "−" if t < 0 else ""
    t = abs(t); mm, ss = int(t // 60), int(round(t % 60))
    return f"{sign}{mm}:{ss:02d}"


def _fmt_off(t: float) -> str:
    return ("+" if t >= 0 else "") + _mmss(t)


# ── Add / edit one plan sequence (unit + source picker; the steps are edited on the stage) ──

class PlanItemDialog(QDialog):
    """Add a sequence to the plan — pick the unit and the library sequence to COPY — or edit an
    existing plan sequence's unit / source / recovery policy. The copy's STEPS are edited in place
    on the plan timeline (the embedded sequence editor), not here. Returns a PlanItem whose .steps
    hold the copy. Remove is offered when editing (result code REMOVE)."""

    REMOVE = 2   # custom result code (distinct from Accepted=1 / Rejected=0)

    def __init__(self, hub: DataHub, sequences_by_host: Dict[str, List[m.Sequence]],
                 item: Optional[m.PlanItem] = None, parent=None, preselect_host: str = ""):
        super().__init__(parent)
        self._hub = hub
        self._seqs = sequences_by_host
        self._item = item
        self.result_item: Optional[m.PlanItem] = None
        self.setWindowTitle("Edit plan sequence" if item else "Add sequence to the plan")
        self._build()
        fit_dialog_to_screen(self, 520, 300)
        self._populate_units()
        if item is not None:
            self._select_combos(item.hostname, item.sequence_id)
            self._set_recovery(item.recovery_policy, item.recovery_mode)
        else:
            if preselect_host:
                i = self._unit.findData(preselect_host)
                if i >= 0:
                    self._unit.setCurrentIndex(i)
            self._on_unit_changed()

    def _build(self) -> None:
        from .dialog_style import editor_qss
        from .param_widgets import Dropdown
        self.setStyleSheet(editor_qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)
        form = QFormLayout(); form.setSpacing(8)
        self._unit = Dropdown()
        self._unit.currentIndexChanged.connect(lambda _=0: self._on_unit_changed())
        form.addRow("Unit", self._unit)
        self._seq = Dropdown()
        self._seq.setToolTip("The library sequence to copy into this plan. Its steps are then "
                             "edited on the plan timeline; the library copy is untouched.")
        form.addRow("Copy of sequence", self._seq)
        self._recovery = Dropdown()
        for label, _p, _m in _RECOVERY_CHOICES:
            self._recovery.addItem(label)
        self._recovery.setToolTip("RF-fault recovery for this plan sequence: inherit the library "
                                  "sequence's policy, or override it here.")
        form.addRow("Recovery", self._recovery)
        outer.addLayout(form)
        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)
        self._buttons = QDialogButtonBox()
        if self._item is not None:
            remove = QPushButton("Remove from plan")
            remove.setStyleSheet(f"color: {Palette.CRASH};")
            self._buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: self.done(self.REMOVE))
        self._buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    def _populate_units(self) -> None:
        self._unit.blockSignals(True)
        for hostname in self._seqs:
            self._unit.addItem(_unit_label(self._hub, hostname), hostname)
        self._unit.blockSignals(False)

    def _select_combos(self, hostname: str, sequence_id: str) -> None:
        self._unit.blockSignals(True)
        idx = self._unit.findData(hostname)
        if idx < 0:   # unit not currently in the fleet — add a stub entry
            self._unit.addItem((self._item.unit_label if self._item else "") or hostname, hostname)
            idx = self._unit.findData(hostname)
        self._unit.setCurrentIndex(idx)
        self._unit.blockSignals(False)
        self._seq.blockSignals(True)
        self._seq.clear()
        for s in self._seqs.get(hostname, []):
            self._seq.addItem(s.name or s.id, s.id)
        i = self._seq.findData(sequence_id)
        if i < 0:     # the source sequence is no longer in the library
            self._seq.addItem(f"{(self._item.sequence_name if self._item else '') or sequence_id} "
                              f"(missing)", sequence_id)
            i = self._seq.findData(sequence_id)
        self._seq.setCurrentIndex(i)
        self._seq.blockSignals(False)

    def _set_recovery(self, pol: str, mode: str) -> None:
        for i, (_l, p, md) in enumerate(_RECOVERY_CHOICES):
            if p == (pol or "") and (not p or md == (mode or "resync")):
                self._recovery.setCurrentIndex(i); return
        self._recovery.setCurrentIndex(0)

    def _current_hostname(self) -> str:
        return self._unit.currentData() or ""

    def _on_unit_changed(self) -> None:
        hostname = self._current_hostname()
        self._seq.blockSignals(True)
        self._seq.clear()
        for s in self._seqs.get(hostname, []):
            self._seq.addItem(s.name or s.id, s.id)
        self._seq.blockSignals(False)

    def _current_source(self) -> Optional[m.Sequence]:
        sid = self._seq.currentData()
        for s in self._seqs.get(self._current_hostname(), []):
            if s.id == sid:
                return s
        return None

    def _accept(self) -> None:
        sid = self._seq.currentData()
        if not sid:
            self._status.setText("pick a sequence to copy")
            return
        hostname = self._current_hostname()
        src = self._current_source()
        _l, pol, mode = _RECOVERY_CHOICES[max(0, self._recovery.currentIndex())]
        if self._item is not None:
            seq_name = self._item.sequence_name
            steps = list(self._item.steps)
            if src is not None and (src.id != self._item.sequence_id or not steps):
                seq_name = src.name
                steps = [s.model_copy(deep=True) for s in src.steps]   # a NEW source: reseed
            self.result_item = self._item.model_copy(update={
                "hostname": hostname, "unit_label": _unit_label(self._hub, hostname,
                                                                 self._item.unit_label),
                "sequence_id": sid, "sequence_name": seq_name or sid, "steps": steps,
                "recovery_policy": pol, "recovery_mode": mode})
        else:
            steps = [s.model_copy(deep=True) for s in src.steps] if src is not None else []
            self.result_item = m.PlanItem(
                id=pg.new_item_id(), hostname=hostname,
                unit_label=_unit_label(self._hub, hostname),
                sequence_id=sid, sequence_name=(src.name if src is not None else sid) or sid,
                steps=steps, overrides=[], recovery_policy=pol, recovery_mode=mode)
        self.accept()


# ── Compatibility shims (the pre-redesign bar dataclass; round-trips a PlanItem) ──────────

_bar_ids = itertools.count(1)


@dataclass
class PlanBar:
    """A PlanItem's editor-side record (kept for the round-trip helpers below)."""
    hostname: str
    unit_label: str
    sequence_id: str
    sequence_name: str
    steps: List[m.SequenceStep] = field(default_factory=list)
    overrides: List[m.StepOverride] = field(default_factory=list)
    start_offset: float = 0.0
    stop_offset: float = 0.0
    item: Optional[m.PlanItem] = None
    uid: int = 0
    kind: str = "bar"

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_bar_ids)


def _bar_from_item(item: m.PlanItem, uid: int = 0) -> PlanBar:
    return PlanBar(hostname=item.hostname, unit_label=item.unit_label,
                   sequence_id=item.sequence_id, sequence_name=item.sequence_name,
                   steps=list(item.steps), overrides=list(item.overrides),
                   start_offset=item.on_air_offset_s, stop_offset=item.off_air_offset_s,
                   item=item, uid=uid)


def _item_from_bar(bar: PlanBar) -> m.PlanItem:
    base = bar.item if bar.item is not None else m.PlanItem(
        hostname=bar.hostname, unit_label=bar.unit_label,
        sequence_id=bar.sequence_id, sequence_name=bar.sequence_name)
    return base.model_copy(update={
        "hostname": bar.hostname, "unit_label": bar.unit_label,
        "sequence_id": bar.sequence_id, "sequence_name": bar.sequence_name,
        "steps": list(bar.steps), "overrides": list(bar.overrides),
        "on_air_offset_s": bar.start_offset, "off_air_offset_s": bar.stop_offset})


# ── The embedded sequence canvas ──────────────────────────────────────────────

class _EmbeddedCanvas(_TimelineCanvas):
    """The sequence editor's canvas, hosted per plan sequence on the plan stage. Its on-air /
    off-air x are DICTATED by the stage (the sequence's resolved window on the plan's timeline),
    it paints only its rows (the stage paints the windows, the guide lines and the plan axis),
    and a drag-to-anchor that leaves the canvas is handed to the stage (cross-sequence anchors).
    Everything else — steps, chips, ramps, connectors, menus, editors, undo — is the sequence
    editor's own code."""

    def __init__(self, editor):
        self._stage = None
        self._plan_on = float(tlm.EDGE_PAD)
        self._plan_off = float(tlm.EDGE_PAD + tlm.MIDDLE_GAP)
        self._plan_w = int(tlm.EDGE_PAD * 2 + tlm.MIDDLE_GAP)
        super().__init__(editor)
        self._did_autofit = True

    # ── plan-dictated geometry ────────────────────────────────────────────────
    def set_plan_window(self, on_x: float, off_x: float, width: int, zoom: float) -> None:
        self._plan_on, self._plan_off, self._plan_w = float(on_x), float(off_x), int(width)
        self._zoom = zoom

    def _compute_anchors(self):
        return self._plan_on, self._plan_off, self._plan_w

    def _recompute_band(self) -> None:
        self._hold_off = tlm.hold_offset(self._items)
        self._step_bases = tlm.resolve_step_offsets(self._items, self._hold_off, self._ext_bases)
        self._step_off_bases = tlm.resolve_step_offsets_off(self._items, self._hold_off, self._ext_bases)
        self._c_on, self._c_off, self._content_w = self._compute_anchors()
        # Holds are deferred in the plan editor: a Hold draws as a plain divider at its offset
        # (the arm paths compile it out / honour it exactly as before).
        self._hold_present = False

    def relayout(self, keep_on: bool = False) -> None:
        super().relayout(keep_on)
        self._content_h = int(getattr(self, "_rows_bottom", LANES_TOP)) + 4
        self.setMinimumHeight(self._content_h)
        self.setMaximumHeight(self._content_h)
        if self._stage is not None:
            self._stage.child_relayout(self)

    def _place(self, keep_on: Optional[float] = None) -> None:
        self._on, self._off = self._c_on, self._c_off
        self._baseline = getattr(self, "_rows_bottom", LANES_TOP) + AXIS_GAP
        self._set_hold_edges()
        self._rebuild_geom()

    def content_height(self) -> int:
        return int(self._content_h)

    # ── painting: rows only ───────────────────────────────────────────────────
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The stage's under-layer first (this sequence's window tint + guide lines, the plan's
        # windows and anchors, every cross-sequence connector), translated to this row — so the
        # layering is the sequence editor's own: lines under items.
        p.fillRect(self.rect(), QColor(Palette.SURFACE))
        if self._stage is not None:
            self._stage.paint_under(p, self)
        self._rmchip = None
        self._rmchip_pos = None
        self._paint_connectors(p)
        for it in self._rows:
            if it.kind == "bar":
                self._paint_bar(p, it)
            elif tlm._is_ramp(it):
                self._paint_ramp(p, it)
            else:
                self._paint_pin(p, it)
        for it in self._holds:
            self._paint_hold_divider(p, it)
        self._paint_root_anchor_hint(p)
        self._paint_selection(p)
        if self._rmchip_pos is not None:
            self._paint_remove_chip(p, *self._rmchip_pos)
        self._paint_marquee(p)
        self._paint_snap_guide(p)
        self._paint_connect_drag(p)
        self._paint_drag_readout(p)
        p.end()

    def _paint_hold_divider(self, p, it):
        g = self._geom.get(it.uid)
        if not g:
            return
        x = int(g["cx"]); amber = QColor(Palette.ARMED)
        pen = QPen(amber, 2); pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
        p.drawLine(x, 2, x, int(self._content_h) - 2)
        f = QFont(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        fm = QFontMetrics(f); tw = fm.horizontalAdvance("⏸ HOLD") + 10
        r = QRectF(x - tw / 2, 1, tw, 13)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(amber)); p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor(Palette.SURFACE)); p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "⏸ HOLD")

    def _paint_connect_drag(self, p):
        conn = self._connect
        if conn is None or not conn.get("moved") or conn.get("target") is not None:
            return super()._paint_connect_drag(p)
        ext = conn.get("ext")
        g = self._geom.get(conn["src"])
        if not g:
            return
        if conn.get("from_edge") == "end":
            x1 = g.get("stop_x", g.get("start_x", g.get("cx", 0.0)))
        else:
            x1 = g.get("start_x", g.get("cx", 0.0))
        y1 = g["y"] + LANE_H / 2
        cur = conn["cursor"]; x2, y2 = cur.x(), cur.y()
        ok = ext is not None
        col = QColor(Palette.ACCENT) if ok else QColor(Palette.TEXT_FAINT)
        if ok and self._stage is not None:
            tx, ty = self._stage.target_point(ext)
            lp = self.mapFromGlobal(self._stage.mapToGlobal(QPointF(tx, ty).toPoint()))
            x2, y2 = float(lp.x()), float(lp.y())
            p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(QColor(Palette.ACCENT))
            p.drawEllipse(QPointF(x2, y2), 6.0, 6.0)
        pen = QPen(col, 2.2)
        pen.setStyle(Qt.PenStyle.SolidLine if ok else Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(); path.moveTo(x1, y1)
        path.cubicTo(x1 + (x2 - x1) * 0.5, y1, x1 + (x2 - x1) * 0.5, y2, x2, y2)
        p.drawPath(path)
        p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(col)
        p.drawEllipse(QPointF(x1, y1), 5.5, 5.5)
        if ok:
            off = tlm._snap((x1 - x2) / self._eff())
            label = f"{self._offset_chip_text(off)} after {self._stage.target_label(ext)}"
        else:
            label = "drop on a step edge, a sequence edge or an anchor line"
        self._paint_tag(p, x2, y2 - 16, label, ok)

    # ── interaction hooks ─────────────────────────────────────────────────────
    def mouseMoveEvent(self, e):  # noqa: N802
        super().mouseMoveEvent(e)
        conn = self._connect
        if conn is None or not conn.get("moved") or self._stage is None:
            return
        if conn.get("target") is not None:
            conn["ext"] = None
        else:
            conn["ext"] = self._stage.external_target(e.globalPosition(), exclude_canvas=self,
                                                      src_uid=conn["src"])
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        conn = self._connect
        if (conn is not None and e.button() == Qt.MouseButton.LeftButton and conn.get("moved")
                and conn.get("target") is None and conn.get("ext") is not None
                and self._stage is not None):
            self._connect = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            src = next((it for it in self._items if it.uid == conn["src"]), None)
            if src is not None:
                self._stage.apply_cross_anchor(("step", self, src, conn.get("from_edge", "start")),
                                               conn["ext"])
            self.update()
            return
        super().mouseReleaseEvent(e)

    def _select_only(self, uid) -> None:
        super()._select_only(uid)
        if self._stage is not None:
            self._stage.on_child_selected(self)

    def _toggle_select(self, uid) -> None:
        super()._toggle_select(uid)
        if self._stage is not None and self._selection:
            self._stage.on_child_selected(self)

    def wheelEvent(self, e):  # noqa: N802
        e.ignore()                       # the stage zooms (Ctrl) / the scroll area pans

    def _apply_zoom(self, factor: float, cursor_x: float) -> None:
        if self._stage is not None:
            self._stage.zoom_by(factor, cursor_x)


class _NullEditor:
    """The editor stand-in behind the stage's ROUTER canvas (only its routing helpers are used)."""

    def available_tasks(self):
        return []

    def _sync_zoom(self):
        pass


class _EmbeddedEditor(TimelineEditor):
    """A full sequence editor 'brain' (context, calibration, script params, dialogs, achievability)
    whose canvas + row header are taken out and hosted on the plan stage. The editor widget itself
    is never shown."""

    CANVAS_CLS = _EmbeddedCanvas

    def __init__(self, parent=None):
        super().__init__(parent)
        scroll = self._canvas._scroll
        if scroll is not None:
            scroll.takeWidget()
        self._canvas._scroll = None
        self._canvas.setParent(None)
        self._rowhdr_e = _RowHeader(self._canvas, embedded=True)
        self._canvas.changed.connect(self._rowhdr_e.refresh)
        self.set_hold_authoring(False)
        self.hide()

    def _fit(self) -> None:
        return          # the stage owns the zoom

    def showEvent(self, e):  # noqa: N802
        QWidget.showEvent(self, e)


# ── One sequence on the stage ─────────────────────────────────────────────────

_node_ids = itertools.count(1)


class _SeqNode:
    """A plan item on the stage: its PlanItem (unit, source, the anchor fields + offsets live
    here) and the embedded editor holding its steps."""

    def __init__(self, item: m.PlanItem, editor: _EmbeddedEditor):
        self.item = item
        self.editor = editor
        self.uid = next(_node_ids)
        self.expanded = bool(getattr(item, "expanded", True))
        self.lane = 0
        self.span: Tuple[float, float] = (0.0, 0.0)
        self.on_x = 0.0
        self.off_x = 0.0
        self.cy = 0.0
        self.row_y = 0.0
        self.row_h = 0.0
        self.canvas_y = 0.0
        self.fault: Optional[str] = None
        # This sequence's OWN windows on the plan axis: its defined on-air region ends at def_end_x
        # (None = nothing on the on clock), its defined off-air region begins at bwd_start_x (None =
        # nothing on the off clock — a fixed-length sequence has no relative region at all).
        self.def_end_x: Optional[float] = None
        self.bwd_start_x: Optional[float] = None

    @property
    def canvas(self) -> _EmbeddedCanvas:
        return self.editor._canvas

    @property
    def id(self) -> str:
        return self.item.id

    def wire_steps(self) -> List[dict]:
        return tlm.items_to_steps(self.canvas.items())

    def graph_item(self):
        it = self.item
        return SimpleNamespace(
            id=it.id, sequence_name=it.sequence_name, hostname=it.hostname,
            on_air_anchor=it.on_air_anchor, on_air_anchor_item=it.on_air_anchor_item,
            on_air_anchor_edge=it.on_air_anchor_edge, on_air_anchor_step=it.on_air_anchor_step,
            on_air_offset_s=it.on_air_offset_s,
            off_air_anchor=it.off_air_anchor, off_air_anchor_item=it.off_air_anchor_item,
            off_air_anchor_edge=it.off_air_anchor_edge, off_air_anchor_step=it.off_air_anchor_step,
            off_air_offset_s=it.off_air_offset_s, steps=self.wire_steps())

    def anchor_desc(self, edge: str) -> Tuple[str, str]:
        """(kind, chip text) for an edge's offset chip: 'on' / 'off' (the plan clock) or 'anc'."""
        it = self.item
        pre = "on_air" if edge == "on" else "off_air"
        anchor = getattr(it, f"{pre}_anchor") or "plan"
        off = float(getattr(it, f"{pre}_offset_s") or 0.0)
        if anchor == "plan":
            a_edge = getattr(it, f"{pre}_anchor_edge") or edge
            return ("off" if a_edge == "off" else "on", _fmt_off(off))
        return ("anc", "⚓ " + _fmt_off(off))


# ── The stage ─────────────────────────────────────────────────────────────────

class _PlanStage(QWidget):
    """The plan's drawing + interaction surface: windows, unit bands, sequence pills / window bars
    with their edge handles, the group frames around expanded sequences with their own on-/off-air
    guide lines, every cross-sequence connector, the plan axis — and it hosts the embedded canvases
    (one per expanded sequence) as children positioned on their rows."""

    changed = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, owner: "PlanTimelineEditor"):
        super().__init__()
        self._owner = owner
        self._nodes: List[_SeqNode] = []
        self._units: List[str] = []
        self._folded: set = set()
        self._zoom = 1.0
        self._rows: List[dict] = []
        self._graph: Optional[pg.PlanGraph] = None
        self._on_x = self._off_x = self._def_end = self._bwd_start = 0.0
        self._w, self._h = 400, 200
        self._conflicts: set = set()
        self._conflict_pairs: List[Tuple[str, str]] = []
        self._sel_node: Optional[int] = None         # uid of the selected sequence
        self._sel_canvas: Optional[_EmbeddedCanvas] = None
        self._drag: Optional[dict] = None
        self._connect: Optional[dict] = None
        self._hscroll = 0
        self._router = _TimelineCanvas(_NullEditor())   # the sequence editor's connector router
        self._router.hide()
        self._in_relayout = False
        self._last_timing: Dict[int, pg.ItemTiming] = {}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)

    # ── nodes ─────────────────────────────────────────────────────────────────
    def nodes(self) -> List[_SeqNode]:
        return list(self._nodes)

    def set_nodes(self, nodes: List[_SeqNode], units: List[str]) -> None:
        for n in self._nodes:
            n.canvas._stage = None
            n.canvas.setParent(None)
        self._nodes = list(nodes)
        self._units = list(units)
        for n in self._nodes:
            self._adopt(n)
        self._sel_node = None; self._sel_canvas = None
        self.relayout()

    def add_node(self, node: _SeqNode) -> None:
        self._nodes.append(node)
        if node.item.hostname not in self._units:
            self._units.append(node.item.hostname)
        self._adopt(node)
        self._sel_node = node.uid
        self.relayout()
        self.changed.emit()

    def remove_node(self, node: _SeqNode) -> None:
        if node not in self._nodes:
            return
        self._nodes.remove(node)
        node.canvas._stage = None
        node.canvas.setParent(None)
        if self._sel_node == node.uid:
            self._sel_node = None
        self.relayout()
        self.changed.emit()

    def _adopt(self, node: _SeqNode) -> None:
        c = node.canvas
        c._stage = self
        c.setParent(self)
        node.editor.changed.connect(self._on_child_changed)

    def node_of(self, canvas) -> Optional[_SeqNode]:
        return next((n for n in self._nodes if n.canvas is canvas), None)

    def node_by_uid(self, uid) -> Optional[_SeqNode]:
        return next((n for n in self._nodes if n.uid == uid), None)

    def node_by_id(self, iid: str) -> Optional[_SeqNode]:
        return next((n for n in self._nodes if n.item.id == iid), None)

    def selected_node(self) -> Optional[_SeqNode]:
        return self.node_by_uid(self._sel_node) if self._sel_node is not None else None

    def units(self) -> List[str]:
        return list(self._units)

    def set_units(self, units: List[str]) -> None:
        """The unit bands to show (fleet order); a node's unit is always included."""
        seen: List[str] = []
        for h in list(units) + [n.item.hostname for n in self._nodes]:
            if h not in seen:
                seen.append(h)
        self._units = seen
        self.relayout()

    # ── geometry ──────────────────────────────────────────────────────────────
    def eff(self) -> float:
        return tlm.SCALE * self._zoom

    def xof(self, c: Optional[pg.Clocked]) -> float:
        if c is None:
            return self._on_x
        return (self._on_x if c[0] == "on" else self._off_x) + c[1] * self.eff()

    def graph(self) -> pg.PlanGraph:
        return self._graph or pg.PlanGraph([n.graph_item() for n in self._nodes])

    def child_relayout(self, canvas) -> None:
        """A child canvas re-laid out on its own (an edit / a drag): keep the stage in step,
        unless it is the stage's own relayout driving it."""
        if not self._in_relayout:
            self._schedule_relayout()

    def _schedule_relayout(self) -> None:
        if getattr(self, "_relayout_pending", False):
            return
        self._relayout_pending = True
        QTimer.singleShot(0, self._deferred_relayout)

    def _deferred_relayout(self) -> None:
        self._relayout_pending = False
        self.relayout()

    def _on_child_changed(self) -> None:
        self.relayout()
        self.changed.emit()

    def relayout(self) -> None:
        if self._in_relayout:
            return
        self._in_relayout = True
        try:
            self._relayout()
        finally:
            self._in_relayout = False

    def _relayout(self) -> None:
        pg.ensure_item_ids([n.item for n in self._nodes])
        eff = self.eff()
        g = pg.PlanGraph([n.graph_item() for n in self._nodes])
        self._graph = g
        self._heal_dangling(g)
        g = pg.PlanGraph([n.graph_item() for n in self._nodes])
        self._graph = g
        fwd, bwd = g.extents()
        self._on_x = PAD + WARM_S * eff
        self._def_end = self._on_x + fwd * eff
        self._bwd_start = self._def_end + HATCH_PX * self._zoom
        self._off_x = self._bwd_start + bwd * eff
        self._w = int(self._off_x + COOL_S * eff + PAD)
        # 1) each sequence's window + its canvas (cross-item step bases in ITS clock)
        for n in self._nodes:
            iid = n.item.id
            tm = g.item_timing(iid)
            n.fault = g.describe_fault(iid)
            on_c = tm.on if tm.on is not None else ("on", float(n.item.on_air_offset_s))
            off_c = tm.off if tm.off is not None else ("off", float(n.item.off_air_offset_s))
            n.on_x, n.off_x = self.xof(on_c), self.xof(off_c)
            self._last_timing[n.uid] = pg.ItemTiming(on_c, off_c)
            on_last, off_first = g.item_windows(iid)
            n.def_end_x = self.xof(("on", on_last)) if on_last is not None else None
            n.bwd_start_x = self.xof(("off", off_first)) if off_first is not None else None
            c = n.canvas
            ext: Dict[int, Tuple[str, float]] = {}
            for it in c.items():
                if not tlm.is_cross_item(it):
                    continue
                a_item = tlm.anchor_item_of(it)
                ref, edge = tlm.step_source_ref(it)
                tc = g.step_edge_by_id(a_item, ref, edge)
                if tc is None:
                    continue
                base_x = self.xof(tc) + tlm.step_wire_offset(it) * eff
                ext[it.uid] = ("start", (base_x - n.on_x) / eff)
            c._ext_bases = ext
            c.set_plan_window(n.on_x, n.off_x, self._w, self._zoom)
            c.relayout()
            xs = [n.on_x, n.off_x]
            for it in c.items():
                gg = c._geom.get(it.uid) or {}
                if "start_x" in gg:
                    xs += [gg["start_x"], gg["stop_x"]]
                elif "cx" in gg:
                    xs.append(gg["cx"])
            n.span = (min(xs), max(xs))
        # 2) rows
        rows: List[dict] = []
        y = float(STAGE_TOP)
        by_unit: Dict[str, List[_SeqNode]] = {}
        for n in self._nodes:
            by_unit.setdefault(n.item.hostname, []).append(n)
        units = [u for u in self._units] + [u for u in by_unit if u not in self._units]
        for host in units:
            mine = by_unit.get(host, [])
            rows.append({"type": "unit", "host": host, "y": y, "h": ROW_UNIT_H, "n": len(mine)})
            y += ROW_UNIT_H + ROW_GAP
            if host in self._folded and mine:
                lanes = pg.pack_lanes([(str(n.uid), n.span[0], n.span[1]) for n in mine])
                n_lanes = (max(lanes.values()) + 1) if lanes else 1
                h = n_lanes * (ROW_SEQC_H - 6) + 6
                for n in mine:
                    n.lane = lanes.get(str(n.uid), 0)
                    n.row_y, n.row_h = y, h
                    n.cy = y + 3 + n.lane * (ROW_SEQC_H - 6) + 18
                    n.canvas.hide()
                rows.append({"type": "ulane", "host": host, "nodes": mine, "y": y, "h": h,
                             "lanes": n_lanes})
                y += h + ROW_GAP
                continue
            for n in mine:
                n.lane = 0
                if n.expanded:
                    rows.append({"type": "seqx", "node": n, "y": y, "h": ROW_SEQX_H})
                    n.row_y, n.row_h = y, ROW_SEQX_H
                    n.cy = y + ROW_SEQX_H - 9
                    y += ROW_SEQX_H + ROW_GAP
                    ch = n.canvas.content_height()
                    n.canvas_y = y
                    n.canvas.setGeometry(0, int(y), self._w, ch)
                    n.canvas.show()
                    rows.append({"type": "canvas", "node": n, "y": y, "h": ch})
                    y += ch + ROW_GAP
                else:
                    rows.append({"type": "seqc", "node": n, "y": y, "h": ROW_SEQC_H})
                    n.row_y, n.row_h = y, ROW_SEQC_H
                    n.cy = y + 5 + 18
                    n.canvas.hide()
                    y += ROW_SEQC_H + ROW_GAP
        self._rows = rows
        self._h = int(y + AXIS_H)
        self._conflict_pairs = pg.channel_conflict_pairs({str(n.uid): n.span for n in self._nodes},
                                                         {str(n.uid): n.item.hostname for n in self._nodes})
        self._conflicts = {u for pair in self._conflict_pairs for u in pair}
        self.setFixedSize(self._w, self._h)
        self.update()
        self._owner._on_stage_relayout()

    def _heal_dangling(self, g: pg.PlanGraph) -> None:
        """A reference whose target left the plan (a removed sequence / a deleted step) is re-rooted
        at the instant it last resolved to, so nothing silently jumps or dangles."""
        for n in self._nodes:
            it = n.item
            last = self._last_timing.get(n.uid)
            for edge, pre in (("on", "on_air"), ("off", "off_air")):
                anchor = getattr(it, f"{pre}_anchor") or "plan"
                if anchor == "plan" or g.item_edge(it.id, edge) is not None:
                    continue
                keep = (last.on if edge == "on" else last.off) if last else None
                if keep is None:
                    keep = ("on" if edge == "on" else "off",
                            float(getattr(it, f"{pre}_offset_s") or 0.0))
                setattr(it, f"{pre}_anchor", "plan")
                setattr(it, f"{pre}_anchor_item", "")
                setattr(it, f"{pre}_anchor_step", "")
                setattr(it, f"{pre}_anchor_edge", keep[0])
                setattr(it, f"{pre}_offset_s", tlm._snap(keep[1]))
            c = n.canvas
            for st in c.items():
                if not tlm.is_cross_item(st):
                    continue
                a_item = tlm.anchor_item_of(st)
                ref, edge = tlm.step_source_ref(st)
                if g.step_edge_by_id(a_item, ref, edge) is None:
                    c._revert_to_root(st)          # at its last resolved base (clears the ref)

    # ── selection ─────────────────────────────────────────────────────────────
    def on_child_selected(self, canvas) -> None:
        for n in self._nodes:
            if n.canvas is not canvas and n.canvas._selection:
                n.canvas._clear_selection(); n.canvas.update()
        node = self.node_of(canvas)
        self._sel_node = node.uid if node else None
        self._sel_canvas = canvas
        self.update()
        self.selection_changed.emit()

    def select_node(self, node: Optional[_SeqNode]) -> None:
        for n in self._nodes:
            if n.canvas._selection:
                n.canvas._clear_selection(); n.canvas.update()
        self._sel_node = node.uid if node else None
        self._sel_canvas = None
        self.update()
        self.selection_changed.emit()

    def target_node(self) -> Optional[_SeqNode]:
        """The sequence the step tools act on: the selected sequence, or the selected step's."""
        return self.selected_node()

    # ── expand / fold ─────────────────────────────────────────────────────────
    def toggle_expanded(self, node: _SeqNode) -> None:
        node.expanded = not node.expanded
        node.item.expanded = node.expanded
        self._sel_node = node.uid
        self.relayout()
        self.changed.emit()

    def set_all_expanded(self, expanded: bool, host: Optional[str] = None) -> None:
        for n in self._nodes:
            if host is None or n.item.hostname == host:
                n.expanded = expanded; n.item.expanded = expanded
        if expanded:
            self._folded.clear() if host is None else self._folded.discard(host)
        self.relayout()
        self.changed.emit()

    def toggle_folded(self, host: str) -> None:
        if host in self._folded:
            self._folded.discard(host)
        else:
            self._folded.add(host)
        self.relayout()

    def is_folded(self, host: str) -> bool:
        return host in self._folded

    # ── zoom ──────────────────────────────────────────────────────────────────
    def zoom_by(self, factor: float, cursor_x: Optional[float] = None) -> None:
        old = self._zoom
        new = max(ZOOM_MIN, min(ZOOM_MAX, old * factor))
        if abs(new - old) < 1e-6:
            return
        self._owner._zoom_to(new, cursor_x)

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))
        self.relayout()

    def zoom(self) -> float:
        return self._zoom

    def set_hscroll(self, value: int) -> None:
        self._hscroll = int(value)
        self.update()

    # ── points / targets ──────────────────────────────────────────────────────
    def seq_edge_point(self, node: _SeqNode, edge: str) -> Tuple[float, float]:
        return (node.on_x if edge == "on" else node.off_x), node.cy

    def seq_edge_exit_point(self, node: _SeqNode, edge: str, toward_y: float) -> Tuple[float, float]:
        """Where a connector LEAVES a sequence's edge: an expanded sequence's on-/off-air line runs
        down through its whole group, so the line continues from the group edge nearest the
        dependent (the guide line reads as part of the connector); a collapsed pill's handle."""
        x = node.on_x if edge == "on" else node.off_x
        if not node.expanded or not node.canvas.isVisible():
            return x, node.cy
        top = node.row_y - 3
        bottom = node.canvas_y + node.canvas.content_height() + 3
        return x, (bottom if toward_y > bottom else (top if toward_y < top else node.cy))

    def step_point(self, node: _SeqNode, it, edge: str) -> Tuple[float, float]:
        c = node.canvas
        g = c._geom.get(it.uid) or {}
        y = node.canvas_y + g.get("y", 0.0) + LANE_H / 2
        if not node.expanded:
            y = node.cy
        if "start_x" in g:
            x = g["stop_x"] if edge == "end" else g["start_x"]
        else:
            x = g.get("cx", node.on_x)
        return x, y

    def target_point(self, desc) -> Tuple[float, float]:
        if desc[0] == "seq":
            return self.seq_edge_point(desc[1], desc[2])
        _k, canvas, it, edge = desc
        node = self.node_of(canvas)
        return self.step_point(node, it, edge) if node else (0.0, 0.0)

    def target_label(self, desc) -> str:
        if desc[0] == "seq":
            n = desc[1]
            return f"{n.item.sequence_name or '?'} · {desc[2]}-air ({n.item.unit_label or n.item.hostname})"
        _k, canvas, it, edge = desc
        n = self.node_of(canvas)
        who = f" ({n.item.unit_label or n.item.hostname})" if n else ""
        return f"{getattr(it, 'task_name', '') or '?'} · {'end' if edge == 'end' else 'start'}{who}"

    def _handle_at(self, x: float, y: float, exclude_node=None):
        for n in self._nodes:
            if n is exclude_node:
                continue
            if abs(y - n.cy) > HANDLE_HIT + 2:
                continue
            if abs(x - n.on_x) <= HANDLE_HIT:
                return ("seq", n, "on")
            if abs(x - n.off_x) <= HANDLE_HIT:
                return ("seq", n, "off")
        return None

    def external_target(self, global_pos, exclude_canvas=None, src_uid=None, src_node=None):
        """What a connect-drag is over, anywhere on the stage: a sequence's edge handle / its own
        on-air or off-air guide line (through its expanded group), or a step edge in another
        sequence's canvas. None away from everything."""
        gp = global_pos.toPoint() if hasattr(global_pos, "toPoint") else global_pos
        lp = self.mapFromGlobal(gp)
        x, y = float(lp.x()), float(lp.y())
        hit = self._handle_at(x, y, exclude_node=src_node)
        if hit is not None:
            return hit
        for n in self._nodes:
            c = n.canvas
            if not n.expanded or not c.isVisible():
                continue
            cl = c.mapFromGlobal(gp)
            cx, cy = float(cl.x()), float(cl.y())
            inside = 0 <= cy <= c.height()
            if c is not exclude_canvas and inside:
                e = c._drop_edge_at(cx, cy)
                if e is not None:
                    it, edge = e
                    if it.uid != src_uid and not tlm._is_hold(it):
                        return ("step", c, it, edge)
            if n is not src_node and inside:
                root = c._root_anchor_at(cx)
                if root == "start":
                    return ("seq", n, "on")
                if root == "stop":
                    return ("seq", n, "off")
        return None

    # ── anchoring ─────────────────────────────────────────────────────────────
    def apply_cross_anchor(self, src, tgt) -> None:
        """Anchor `src` (a sequence edge, or a step from a child canvas) to `tgt` (a sequence edge or
        a step edge), keeping the source visually in place — the offset is the current pixel gap.
        A drop that would loop is refused."""
        eff = self.eff()
        tx, _ty = self.target_point(tgt)
        if tgt[0] == "seq":
            t_node, t_edge = tgt[1], tgt[2]
            t_item_id, t_sid, t_wire_edge = t_node.item.id, "", ("start" if t_edge == "on" else "end")
        else:
            _k, t_canvas, t_it, t_edge = tgt
            t_node = self.node_of(t_canvas)
            if t_node is None:
                return
            sid = tlm.ensure_step_id(t_it)
            if not sid:
                return
            bar_ids = {sid} if getattr(t_it, "kind", None) == "bar" else set()
            t_sid, t_wire_edge = tlm._encode_anchor_ref(sid, t_edge, bar_ids)
            t_item_id = t_node.item.id
        if src[0] == "seq":
            _k, node, edge = src
            it = node.item
            pre = "on_air" if edge == "on" else "off_air"
            sx = node.on_x if edge == "on" else node.off_x
            snapshot = it.model_copy()
            setattr(it, f"{pre}_offset_s", tlm._snap((sx - tx) / eff))
            if tgt[0] == "seq":
                setattr(it, f"{pre}_anchor", "item")
                setattr(it, f"{pre}_anchor_item", "" if t_node is node else t_node.item.id)
                setattr(it, f"{pre}_anchor_edge", t_edge)
                setattr(it, f"{pre}_anchor_step", "")
            else:
                setattr(it, f"{pre}_anchor", "step")
                setattr(it, f"{pre}_anchor_item", t_item_id)
                setattr(it, f"{pre}_anchor_edge", t_wire_edge)
                setattr(it, f"{pre}_anchor_step", t_sid)
            g = pg.PlanGraph([n.graph_item() for n in self._nodes])
            if g.item_edge(it.id, edge) is None:
                for f_ in ("on_air_anchor", "on_air_anchor_item", "on_air_anchor_edge",
                           "on_air_anchor_step", "on_air_offset_s", "off_air_anchor",
                           "off_air_anchor_item", "off_air_anchor_edge", "off_air_anchor_step",
                           "off_air_offset_s"):
                    setattr(it, f_, getattr(snapshot, f_))
                self._notice("That anchor would loop back on itself — not applied.")
                self.update()
                return
            self._sel_node = node.uid
            self.relayout()
            self.changed.emit()
            return
        # a step from a child canvas
        _k, canvas, s_it, from_edge = src
        s_node = self.node_of(canvas)
        if s_node is None or tlm._is_hold(s_it):
            return
        if t_node is s_node and tgt[0] == "step":
            return                       # a same-sequence step target is the canvas's own job
        g = canvas._geom.get(s_it.uid) or {}
        end_tied = tlm._is_ramp(s_it) and from_edge == "end"
        s_x = g.get("stop_x", g.get("start_x")) if end_tied else g.get("start_x", g.get("cx"))
        if s_x is None:
            s_x = tx
        off = tlm._snap((s_x - tx) / eff)
        canvas._record()
        if getattr(s_it, "kind", None) == "bar":
            before = (s_it.start_anchor, s_it.start_anchor_step_id, s_it.start_anchor_edge,
                      s_it.start_anchor_item, s_it.start_offset)
            s_it.start_anchor = "step"
            s_it.start_anchor_step_id = t_sid
            s_it.start_anchor_edge = t_wire_edge
            s_it.start_anchor_item = t_item_id
            s_it.start_offset = off
        else:
            before = (s_it.anchor, s_it.anchor_step_id, s_it.anchor_edge, s_it.anchor_item,
                      s_it.anchor_own_edge, s_it.offset)
            s_it.anchor = "step"
            s_it.anchor_step_id = t_sid
            s_it.anchor_edge = t_wire_edge
            s_it.anchor_item = t_item_id
            s_it.anchor_own_edge = "end" if end_tied else "start"
            s_it.offset = off
        gph = pg.PlanGraph([n.graph_item() for n in self._nodes])
        wire = s_node.wire_steps()
        looped = False
        for i, s in enumerate(wire):
            if s.get("anchor_item") and gph.step_edge(s_node.item.id, i, "start") is None:
                looped = True
        if looped:
            if getattr(s_it, "kind", None) == "bar":
                (s_it.start_anchor, s_it.start_anchor_step_id, s_it.start_anchor_edge,
                 s_it.start_anchor_item, s_it.start_offset) = before
            else:
                (s_it.anchor, s_it.anchor_step_id, s_it.anchor_edge, s_it.anchor_item,
                 s_it.anchor_own_edge, s_it.offset) = before
            canvas._undo.pop() if canvas._undo else None
            self._notice("That anchor would loop back on itself — not applied.")
            return
        canvas._select_only(s_it.uid)
        canvas.relayout()
        canvas.changed.emit()

    def detach_edge(self, node: _SeqNode, edge: str) -> None:
        """Re-root a sequence edge on the plan's own clock at the instant it resolves to now."""
        it = node.item
        pre = "on_air" if edge == "on" else "off_air"
        tm = self._last_timing.get(node.uid)
        c = (tm.on if edge == "on" else tm.off) if tm else None
        if c is None:
            c = ("on" if edge == "on" else "off", float(getattr(it, f"{pre}_offset_s") or 0.0))
        setattr(it, f"{pre}_anchor", "plan")
        setattr(it, f"{pre}_anchor_item", "")
        setattr(it, f"{pre}_anchor_step", "")
        setattr(it, f"{pre}_anchor_edge", c[0])
        setattr(it, f"{pre}_offset_s", tlm._snap(c[1]))
        self.relayout()
        self.changed.emit()

    def _notice(self, text: str) -> None:
        QToolTip.showText(QCursor.pos(), text, self)

    # ── painting ──────────────────────────────────────────────────────────────
    def _paint_under_layer(self, p, with_bands: bool) -> None:
        """Everything that sits UNDER the rows: the unit bands, the expanded groups' frames + their
        own windows + guide lines, the plan's anchor lines and every cross-sequence
        connector. Shared by the stage's own paint and each embedded canvas (which paints it
        translated to its row, so the layering is identical on and off a canvas)."""
        top = STAGE_TOP - 4
        bottom = self._h - AXIS_H
        on_x, off_x = self._on_x, self._off_x
        # No plan-wide defined / relative / defined bands: every sequence paints ITS OWN three
        # windows (its defined on-air region, its relative middle whose length is set at arm, its
        # defined off-air region) inside its frame or pill — see _paint_regions.
        for r in self._rows:
            if r["type"] == "unit" and with_bands:
                self._paint_unit_band(p, r)
            elif r["type"] == "seqx":
                self._paint_group(p, r["node"])
        p.setPen(QPen(QColor(Palette.ONLINE), 2)); p.drawLine(int(on_x), int(top - 6), int(on_x), int(bottom + 6))
        p.setPen(QPen(QColor(Palette.CRASH), 2)); p.drawLine(int(off_x), int(top - 6), int(off_x), int(bottom + 6))
        self._paint_connectors(p)

    def paint_under(self, p, canvas) -> None:
        """Paint the under-layer into an embedded canvas's painter (child coordinates: the canvas
        spans the stage's full width at x = 0, so only its row offset differs)."""
        node = self.node_of(canvas)
        if node is None:
            return
        p.save()
        p.translate(0.0, -float(node.canvas_y))
        self._paint_under_layer(p, with_bands=False)
        p.restore()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        # The stage paints its whole background itself (the app's grey ground must never show
        # through a sequence's header / collapsed row as a full-width stripe — owner).
        p.fillRect(self.rect(), QColor(Palette.SURFACE))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        top = STAGE_TOP - 4
        bottom = self._h - AXIS_H
        on_x, off_x = self._on_x, self._off_x
        self._paint_under_layer(p, with_bands=True)
        self._paint_plan_anchor(p, on_x, top, bottom, "ON-AIR", Palette.ONLINE)
        self._paint_plan_anchor(p, off_x, top, bottom, "OFF-AIR", Palette.CRASH)
        # sequence rows
        for r in self._rows:
            if r["type"] == "seqc":
                self._paint_pill(p, r["node"], r["node"].row_y + 5, slim=False)
            elif r["type"] == "seqx":
                self._paint_pill(p, r["node"], r["node"].row_y + r["node"].row_h - 16, slim=True)
            elif r["type"] == "ulane":
                for n in r["nodes"]:
                    self._paint_pill(p, n, r["y"] + 3 + n.lane * (ROW_SEQC_H - 6) + 2, slim=False)
        self._paint_axis(p, bottom)
        self._paint_seq_connect_drag(p)
        self._paint_seq_drag_readout(p)
        p.end()

    def _paint_plan_anchor(self, p, x, top, bottom, label, color):
        col = QColor(color)
        p.setPen(QPen(col, 2)); p.drawLine(int(x), int(top - 6), int(x), int(bottom + 6))
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPointSize(8); f.setBold(True)
        p.setFont(f); fm = QFontMetrics(f); tw = fm.horizontalAdvance(label) + 12
        r = QRectF(x - tw / 2, top - 15, tw, 15)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(col); p.drawRoundedRect(r, 5, 5)
        p.setPen(QColor("#FFFFFF")); p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), label)

    def _unit_state(self, host: str) -> Tuple[str, str]:
        """(label, status line) for a unit band."""
        hub = self._owner._hub
        label = _unit_label(hub, host)
        try:
            client = hub.fleet.get(host)
            st = getattr(client, "state", None)
            sv = getattr(st, "value", st)
            status = "online" if sv == "online" else ("offline" if sv == "offline" else "")
        except Exception:  # noqa: BLE001
            status = ""
        return label, status

    def _paint_unit_band(self, p, r):
        y, h = r["y"], r["h"]
        # The mockup's unit band: a soft grey that fades out to the right (anchored at the viewport's
        # left edge, like the sticky label), between a border above and a hairline below.
        x0 = float(self._hscroll)
        grad = QLinearGradient(x0, 0.0, x0 + 1400.0, 0.0)
        grad.setColorAt(0.0, QColor(238, 242, 246, 235))
        grad.setColorAt(0.6, QColor(246, 248, 250, 140))
        grad.setColorAt(1.0, QColor(246, 248, 250, 50))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(grad))
        p.drawRect(QRectF(0, y, self._w, h))
        p.setPen(QPen(QColor(Palette.BORDER), 1))
        p.drawLine(0, int(y), self._w, int(y))
        hair = QColor(Palette.BORDER); hair.setAlpha(120); p.setPen(QPen(hair, 1))
        p.drawLine(0, int(y + h), self._w, int(y + h))
        label, status = self._unit_state(r["host"])
        x = max(8.0, float(self._hscroll) + 8.0)            # sticky: rides the viewport's left edge
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(12); f.setBold(True)
        p.setFont(f); fm = QFontMetrics(f)
        p.setPen(QColor(Palette.TEXT))
        p.drawText(QRectF(x, y, 400, h), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   label)
        fs = QFont(Fonts.SANS.split(",")[0].strip('"')); fs.setPixelSize(10)
        p.setFont(fs); p.setPen(QColor(Palette.TEXT_FAINT))
        n = r["n"]
        note = f"· {r['host']}{(' · ' + status) if status else ''} · {n} sequence{'' if n == 1 else 's'}"
        p.drawText(QRectF(x + fm.horizontalAdvance(label) + 8, y, 600, h),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), note)

    def _paint_regions(self, p, node: _SeqNode, top: float, bottom: float, clip=None) -> None:
        """A sequence's OWN three windows between its on-air and off-air, washed across its expanded
        frame / its collapsed pill (the owner likes them): the defined on-air region (green, up to its
        last on-clock item), the relative middle whose length is set at arm (hatched, dashed edges),
        and the defined off-air region (red, from its first off-clock item). A sequence with nothing
        on one clock has no relative region — a fixed-length sequence is all green."""
        x0, x1 = node.on_x, node.off_x
        if x1 - x0 <= 1.0:
            return
        de, bs = node.def_end_x, node.bwd_start_x
        if de is None and bs is None:
            return
        if de is None:
            g_end, h_lo, h_hi, r_start = x0, x0, x0, x0            # all off-clock: red only
        elif bs is None:
            g_end, h_lo, h_hi, r_start = x1, x1, x1, x1            # fixed length: green only
        else:
            g_end = max(x0, min(x1, de)); r_start = max(g_end, min(x1, bs))
            h_lo, h_hi = g_end, r_start
        p.save()
        if clip is not None:
            p.setClipPath(clip)
        p.setPen(Qt.PenStyle.NoPen)
        gt = QColor(Palette.ONLINE); gt.setAlpha(13); p.setBrush(gt)
        p.drawRect(QRectF(x0, top, max(0.0, g_end - x0), bottom - top))
        rt = QColor(Palette.CRASH); rt.setAlpha(13); p.setBrush(rt)
        p.drawRect(QRectF(r_start, top, max(0.0, x1 - r_start), bottom - top))
        if h_hi - h_lo > 2.0:
            hb = QColor("#F4F6F9"); hb.setAlpha(170); p.setBrush(hb)
            p.drawRect(QRectF(h_lo, top, h_hi - h_lo, bottom - top))
            p.setClipRect(QRectF(h_lo, top, h_hi - h_lo, bottom - top), Qt.ClipOperation.IntersectClip)
            p.setPen(QPen(QColor("#DFE4EA"), 1)); p.setBrush(Qt.BrushStyle.NoBrush)
            h = bottom - top; xx = h_lo - h
            while xx < h_hi:
                p.drawLine(QPointF(xx, bottom), QPointF(xx + h, top)); xx += 7
            p.setClipRect(QRectF(x0 - 20, top - 20, x1 - x0 + 40, bottom - top + 40), Qt.ClipOperation.ReplaceClip)
            if clip is not None:
                p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            pen = QPen(QColor(Palette.BORDER_STRONG), 1); pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
            p.drawLine(QPointF(h_lo, top), QPointF(h_lo, bottom))
            p.drawLine(QPointF(h_hi, top), QPointF(h_hi, bottom))
        p.restore()

    def _paint_group(self, p, node: _SeqNode):
        c = node.canvas
        x1, x2 = node.span
        y1 = node.row_y - 3
        y2 = node.canvas_y + c.content_height() + 3
        frame = QColor(SEQ_FRAME); frame.setAlpha(170)
        pen = QPen(frame, 1.2); pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(x1 - MIN_EAR - 6, y1, (x2 - x1) + 2 * MIN_EAR + 12, y2 - y1), 10, 10)
        clip = QPainterPath(); clip.addRoundedRect(QRectF(node.on_x, y1 + 1, max(0.0, node.off_x - node.on_x), y2 - y1 - 2), 8, 8)
        self._paint_regions(p, node, y1 + 1, y2 - 1, clip)      # its own defined / relative / defined windows
        gpen = QPen(QColor(Palette.ONLINE), 1.5); gpen.setStyle(Qt.PenStyle.DashLine); p.setPen(gpen)
        p.drawLine(int(node.on_x), int(y1), int(node.on_x), int(y2))
        rpen = QPen(QColor(Palette.CRASH), 1.5); rpen.setStyle(Qt.PenStyle.DashLine); p.setPen(rpen)
        p.drawLine(int(node.off_x), int(y1), int(node.off_x), int(y2))

    def _paint_ears(self, p, node: _SeqNode, y: float, h: float):
        """Hatched stubs for the part of the sequence outside its own window (warm-up / cool-down)."""
        x1, x2 = node.span
        ink = QColor(SEQ_FRAME); ink.setAlpha(120)
        for lo, hi in ((min(x1, node.on_x - MIN_EAR), node.on_x) if x1 < node.on_x - 0.5 else (None, None),
                       (node.off_x, max(x2, node.off_x + MIN_EAR)) if x2 > node.off_x + 0.5 else (None, None)):
            if lo is None:
                continue
            r = QRectF(lo, y, hi - lo, h)
            p.save(); p.setClipRect(r)
            p.setPen(QPen(ink, 1)); p.setBrush(Qt.BrushStyle.NoBrush)
            xx = lo - h
            while xx < hi + h:
                p.drawLine(QPointF(xx, y + h), QPointF(xx + h, y))
                xx += 6
            p.restore()
            p.setPen(QPen(ink, 1)); p.drawRect(r)

    def _chip(self, p, x, y, text, kind, align_right=False):
        f = mono_font(10); p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 12
        col = {"on": QColor(Palette.ONLINE), "off": QColor(Palette.CRASH)}.get(kind, QColor(Palette.ACCENT))
        r = QRectF(x - w if align_right else x, y, w, 16)
        p.setPen(QPen(col, 1)); p.setBrush(QColor(Palette.SURFACE))
        p.drawRoundedRect(r, 8, 8)
        p.setPen(col); p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
        return w

    def _paint_pill(self, p, node: _SeqNode, y: float, slim: bool):
        x, x2 = node.on_x, node.off_x
        w = max(float(PILL_MIN_W), x2 - x)
        sel = node.uid == self._sel_node
        conflict = str(node.uid) in self._conflicts
        frame = QColor(Palette.CRASH) if conflict else (QColor(Palette.ACCENT) if sel else QColor(SEQ_FRAME))
        if slim:
            r = QRectF(x, y + 2, w, 14)
            p.setPen(QPen(frame, 1.5 if (sel or conflict) else 1)); p.setBrush(QColor(Palette.INSET))
            p.drawRoundedRect(r, 5, 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(Palette.ONLINE)); p.drawRoundedRect(QRectF(x, y + 2, 4, 14), 2, 2)
            p.setBrush(QColor(Palette.CRASH)); p.drawRoundedRect(QRectF(x + w - 4, y + 2, 4, 14), 2, 2)
            on_k, on_t = node.anchor_desc("on"); off_k, off_t = node.anchor_desc("off")
            self._chip(p, x, y - 13, on_t, on_k)
            self._chip(p, x + w, y - 13, off_t, off_k, align_right=True)
            cy = y + 9
        else:
            self._paint_ears(p, node, y + 7, 22)
            r = QRectF(x, y, w, 36)
            p.setPen(QPen(frame, 1.5 if (sel or conflict) else 1)); p.setBrush(QColor(Palette.SURFACE))
            p.drawRoundedRect(r, 9, 9)
            pclip = QPainterPath(); pclip.addRoundedRect(r, 9, 9)
            self._paint_regions(p, node, y, y + 36, pclip)          # its own defined / relative windows
            p.save(); clip = QPainterPath(); clip.addRoundedRect(r, 9, 9); p.setClipPath(clip)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(Palette.ONLINE)); p.drawRect(QRectF(x, y, 4, 36))
            p.setBrush(QColor(Palette.CRASH)); p.drawRect(QRectF(x + w - 4, y, 4, 36))
            p.restore()
            on_k, on_t = node.anchor_desc("on"); off_k, off_t = node.anchor_desc("off")
            cw1 = self._chip(p, x + 9, y + 3, on_t, on_k)
            cw2 = self._chip(p, x + w - 9, y + 3, off_t, off_k, align_right=True)
            f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(12); f.setBold(True)
            p.setFont(f); fm = QFontMetrics(f)
            avail = max(10.0, w - cw1 - cw2 - 30)
            name = node.item.sequence_name or node.item.sequence_id
            steps = len(node.canvas.items())
            txt = fm.elidedText(f"{name}  · {steps} step{'' if steps == 1 else 's'}",
                                Qt.TextElideMode.ElideRight, int(avail))
            p.setPen(QColor(Palette.TEXT))
            p.drawText(QRectF(x + 9 + cw1 + 6, y + 3, avail, 16),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), txt)
            self._paint_mini(p, node, x + 12, y + 23, w - 24, 9)
            cy = y + 18
        node.cy = cy
        self._paint_handle(p, x, cy, Palette.ONLINE, self._edge_linked(node, "on"))
        self._paint_handle(p, x2, cy, Palette.CRASH, self._edge_linked(node, "off"))
        if node.fault:
            f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(9); f.setBold(True)
            p.setFont(f); p.setPen(QColor(Palette.CRASH))
            p.drawText(QRectF(x, y - 14, 400, 12), int(Qt.AlignmentFlag.AlignLeft), "⚠ can't be timed")

    def _edge_linked(self, node: _SeqNode, edge: str) -> bool:
        it = node.item
        pre = "on_air" if edge == "on" else "off_air"
        if (getattr(it, f"{pre}_anchor") or "plan") != "plan":
            return True
        for n in self._nodes:
            o = n.item
            for pre2 in ("on_air", "off_air"):
                if ((getattr(o, f"{pre2}_anchor") or "plan") == "item"
                        and ((getattr(o, f"{pre2}_anchor_item") or n.item.id) == it.id)
                        and (getattr(o, f"{pre2}_anchor_edge") or "") == edge):
                    return True
            for st in n.canvas.items():
                if (tlm.is_cross_item(st) and tlm.anchor_item_of(st) == it.id
                        and not tlm.step_source_ref(st)[0]
                        and tlm.step_source_ref(st)[1] == ("start" if edge == "on" else "end")):
                    return True
        return False

    def _paint_handle(self, p, x, cy, color, linked):
        r = QRectF(x - HANDLE_R, cy - HANDLE_R, 2 * HANDLE_R, 2 * HANDLE_R)
        if linked:
            p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(QColor(color))
        else:
            p.setPen(QPen(QColor(color), 2)); p.setBrush(QColor(Palette.SURFACE))
        p.drawEllipse(r)

    def _paint_mini(self, p, node: _SeqNode, x0, y0, w, h):
        c = node.canvas
        span = max(1.0, node.off_x - node.on_x)

        def px(x):
            return x0 + max(0.0, min(w, (x - node.on_x) / span * w))
        for it in c.items():
            g = c._geom.get(it.uid) or {}
            base, _e, _fa, _fb, _ink = c._item_colors(it)
            col = QColor(base)
            if it.kind == "bar":
                col.setAlpha(110); p.setPen(QPen(col, 2))
                p.drawLine(QPointF(px(g.get("start_x", 0)), y0 + h - 1.5), QPointF(px(g.get("stop_x", 0)), y0 + h - 1.5))
            elif tlm._is_ramp(it):
                rising = (dict(it.ramp or {}).get("stop", 0) or 0) >= (dict(it.ramp or {}).get("start", 0) or 0)
                col.setAlpha(210); p.setPen(QPen(col, 1.6))
                a, b = px(g.get("start_x", 0)), px(g.get("stop_x", 0))
                p.drawLine(QPointF(a, y0 + (h - 2 if rising else 1)), QPointF(b, y0 + (1 if rising else h - 2)))
            elif not tlm._is_hold(it):
                col.setAlpha(190); p.setPen(QPen(col, 1.6))
                xx = px(g.get("cx", 0)); p.drawLine(QPointF(xx, y0), QPointF(xx, y0 + h))

    # ── connectors (the sequence editor's router) ─────────────────────────────
    def _obstacles_between(self, y1: float, y2: float) -> List[Tuple[float, float]]:
        lo, hi = sorted((y1, y2))
        out: List[Tuple[float, float]] = []
        for n in self._nodes:
            if lo + 2 < n.cy < hi - 2:
                out.append((n.span[0], n.span[1]))
            if n.expanded:
                c = n.canvas
                for it in c.items():
                    g = c._geom.get(it.uid) or {}
                    cy = n.canvas_y + g.get("y", 0.0) + LANE_H / 2
                    if not (lo + 2 < cy < hi - 2):
                        continue
                    if "start_x" in g:
                        out.append(tuple(sorted((g["start_x"], g["stop_x"]))))
                    elif "cx" in g:
                        out.append((g["cx"] - 8.0, g["cx"] + 8.0))
        return out

    def _connector_specs(self) -> List[dict]:
        specs: List[dict] = []
        rt = self._router
        for n in self._nodes:
            it = n.item
            for edge, pre in (("on", "on_air"), ("off", "off_air")):
                anchor = getattr(it, f"{pre}_anchor") or "plan"
                if anchor == "plan":
                    continue
                t_id = getattr(it, f"{pre}_anchor_item") or it.id
                t_node = self.node_by_id(t_id)
                if t_node is None:
                    continue
                if anchor == "item":
                    if t_node is n:
                        continue                              # self-anchored (fixed length): chip only
                    t_edge = getattr(it, f"{pre}_anchor_edge") or "on"
                    x1, y1 = self.seq_edge_point(t_node, t_edge)
                    exit_dir = -1.0 if t_edge == "on" else 1.0
                    two_sided = True
                    hue = QColor(SEQ_INK); ink = QColor(Palette.TEXT)
                    cap = None
                else:
                    sid = getattr(it, f"{pre}_anchor_step") or ""
                    st = self._find_step(t_node, sid)
                    if st is None:
                        continue
                    s_it, s_edge = st
                    if s_edge != "end":
                        s_edge = "end" if (getattr(it, f"{pre}_anchor_edge") or "") == "end" else "start"
                    x1, y1 = self.step_point(t_node, s_it, s_edge)
                    two_sided = tlm._is_ramp(s_it) or getattr(s_it, "kind", "") == "bar"
                    exit_dir = (-1.0 if s_edge == "start" else 1.0) if two_sided else 1.0
                    base, _e, _fa, _fb, ink = t_node.canvas._item_colors(s_it)
                    hue = QColor(base)
                    cap = None
                x2, y2 = self.seq_edge_point(n, edge)
                if anchor == "item":
                    x1, y1 = self.seq_edge_exit_point(t_node, t_edge, y2)
                efr = (edge == "off")
                if not two_sided and anchor == "step":
                    exit_dir = 1.0 if x2 >= x1 else -1.0
                off = float(getattr(it, f"{pre}_offset_s") or 0.0)
                specs.append(dict(x1=x1, y1=y1, x2=x2, y2=y2, exit_dir=exit_dir, two_sided=two_sided,
                                  entry_from_right=efr, text=rt._offset_chip_text(off), base=hue,
                                  ink=ink, sel=(n.uid == self._sel_node), anchor_cap=cap,
                                  line_anchor=(anchor == "item"),
                                  chip_w=QFontMetrics(mono_font(10)).horizontalAdvance(
                                      rt._offset_chip_text(off)) + 14.0))
            if not n.expanded:
                continue
            c = n.canvas
            for st in c.items():
                if not tlm.is_cross_item(st):
                    continue
                a_item = tlm.anchor_item_of(st)
                ref, s_edge = tlm.step_source_ref(st)
                t_node = self.node_by_id(a_item)
                if t_node is None:
                    continue
                x2, efr, dep_two = c._dep_entry(st)
                y2 = n.canvas_y + (c._geom.get(st.uid) or {}).get("y", 0.0) + LANE_H / 2
                if ref:
                    found = self._find_step(t_node, ref)
                    if found is None:
                        continue
                    t_it, t_edge = found
                    if t_edge != "end":
                        t_edge = "end" if s_edge == "end" else "start"
                    x1, y1 = self.step_point(t_node, t_it, t_edge)
                    two_sided = tlm._is_ramp(t_it) or getattr(t_it, "kind", "") == "bar"
                    exit_dir = (-1.0 if t_edge == "start" else 1.0) if two_sided else 1.0
                else:
                    t_edge = "on" if s_edge == "start" else "off"
                    x1, y1 = self.seq_edge_exit_point(t_node, t_edge, y2)
                    two_sided = True
                    exit_dir = -1.0 if t_edge == "on" else 1.0
                if efr is None:
                    efr = x2 < x1 - 1.0
                if not two_sided:
                    exit_dir = 1.0 if x2 >= x1 else -1.0
                base, _e, _fa, _fb, ink = c._item_colors(st)
                off = c._dep_offset(st)
                specs.append(dict(x1=x1, y1=y1, x2=x2, y2=y2, exit_dir=exit_dir, two_sided=two_sided,
                                  entry_from_right=efr, text=rt._offset_chip_text(off), base=QColor(base),
                                  ink=ink, sel=(st.uid == c._selected), anchor_cap=None,
                                  line_anchor=(not ref), chip_w=c._chip_w(st)))
        return specs

    def _find_step(self, node: _SeqNode, wire_id: str):
        """(canvas item, edge) addressed by a WIRE step id: a bar's stop step (id + suffix) is the
        bar's END edge; anything else is the item's start edge."""
        if not wire_id:
            return None
        if wire_id.endswith(tlm.BAR_STOP_SUFFIX):
            base_id, edge = wire_id[: -len(tlm.BAR_STOP_SUFFIX)], "end"
        else:
            base_id, edge = wire_id, "start"
        for it in node.canvas.items():
            if (getattr(it, "step_id", "") or "") == base_id:
                return it, edge
        return None

    @staticmethod
    def _route_obstacles(obstacles, x1: float, x2: float, entry_from_right: bool):
        """The obstacles the router is asked to avoid. An obstacle it could only dodge by pushing the
        drop column back PAST THE ANCHOR — a bar spanning its sequence's whole window, or a sequence
        pill in between — is CROSSED instead (the line passes behind it), never routed around via
        the far end of the window: a +3:20 line into a task's down-ramp must not loop back to that
        sequence's on-air first."""
        GAP = 14.0
        keep = []
        for lo, hi in obstacles:
            if entry_from_right and x1 > x2:
                if hi + GAP >= x1 - 8.0:
                    continue
            elif lo - GAP <= x1 + 8.0:
                continue
            keep.append((lo, hi))
        return keep

    def _line_anchor_route(self, s: dict, obstacles) -> Optional[List[Tuple[float, float]]]:
        """A sequence's on-/off-air LINE as the anchor. The wire leaves the line at the group edge
        nearest the dependent (or a pill's handle), so there is no body to stub away from — the
        sequence editor's near-zero-offset WRAP (stub out, hook back over the line, drop, run in)
        only draws a hook here (owner report). When the general route would wrap — the drop column
        falls on the exit's far side because the dependent is too close for the chip's entry run —
        drop STRAIGHT from the exit point to the dependent's row and run in (the chip rides the
        drop), or, when that column is blocked or the dependent sits behind the line, jog to the
        column with no stub. None = the general route is fine (no wrap)."""
        rt = self._router
        x1, y1, x2, y2 = s["x1"], s["y1"], s["x2"], s["y2"]
        efr = s["entry_from_right"]
        xd = rt._drop_column(x1, x2, efr, s["chip_w"] + 24.0, obstacles)
        if abs(xd - x1) <= 1.0 or (xd - x1) * s["exit_dir"] > 0:
            return None
        beyond = (x2 <= x1 + 1.0) if efr else (x2 >= x1 - 1.0)
        blocked = any(lo - 6.0 <= x1 <= hi + 6.0 for lo, hi in obstacles)
        if beyond and not blocked:
            return [(x1, y1), (x1, y2), (x2, y2)]
        return rt._connector_points(x1, y1, x2, y2, 0.0, obstacles, s["chip_w"] + 24.0, None, efr,
                                    two_sided=False)

    def _routed_connectors(self) -> List[dict]:
        """Every cross-sequence connector with its routed waypoints (`pts`), as the painter draws it.
        Two passes: each wire is routed on its own first, then re-routed so its drop column also
        steers clear of where the OTHER wires ENTER their dependents (the entry run + offset chip +
        arrowhead) on the rows it drops through — the stage's other cross-sequence wires AND every
        expanded sequence's own wires — so a line never drops through another line's chip."""
        rt = self._router
        specs = self._connector_specs()

        def route(s, extra):
            obstacles = self._route_obstacles(self._obstacles_between(s["y1"], s["y2"]) + extra,
                                              s["x1"], s["x2"], s["entry_from_right"])
            if s.get("line_anchor"):
                pts = self._line_anchor_route(s, obstacles)
                if pts is not None:
                    return pts
            return rt._connector_points(s["x1"], s["y1"], s["x2"], s["y2"], s["exit_dir"], obstacles,
                                        s["chip_w"] + 24.0, s["anchor_cap"], s["entry_from_right"],
                                        two_sided=s["two_sided"])
        first = [route(s, []) for s in specs]
        zones = [(s["y2"], pts[-2][0] if len(pts) >= 2 else s["x2"], s["x2"]) for s, pts in zip(specs, first)]
        for n in self._nodes:
            if not n.expanded:
                continue
            for c in n.canvas._routed_connectors():
                pts = c["pts"]
                zones.append((n.canvas_y + c["y2"], pts[-2][0] if len(pts) >= 2 else c["x2"], c["x2"]))
        conns = []
        for i, s in enumerate(specs):
            lo, hi = sorted((s["y1"], s["y2"]))
            extra = [(min(xd, ex) - 6.0, max(xd, ex) + 6.0)
                     for j, (yz, xd, ex) in enumerate(zones) if j != i and lo + 2 < yz < hi - 2]
            pts = route(s, extra) if extra else first[i]
            conns.append(dict(pts=pts, base=s["base"], ink=s["ink"], sel=s["sel"], x1=s["x1"], y1=s["y1"],
                              x2=s["x2"], y2=s["y2"], entry_from_right=s["entry_from_right"],
                              text=s["text"], chip_w=s["chip_w"], spec=s))
        return conns

    def _paint_connectors(self, p):
        rt = self._router
        conns = self._routed_connectors()
        for c in conns:
            rt._draw_connector_path(p, c)
        polylines = [c["pts"] for c in conns]
        rt._rmchip_pos = None
        for i, c in enumerate(conns):
            others = [polylines[j] for j in range(len(conns)) if j != i]
            rt._draw_connector_head(p, c, others)

    def _paint_axis(self, p, bottom):
        eff = self.eff()
        baseline = int(bottom + 8)
        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 1))
        p.drawLine(int(PAD), baseline, int(self._w - PAD), baseline)
        fwd = (self._def_end - self._on_x) / eff
        bwd = (self._off_x - self._bwd_start) / eff
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(9); p.setFont(f)

        def tick(x, major, label=None, anchor=False):
            p.setPen(QPen(QColor(Palette.BORDER_STRONG if major else Palette.BORDER), 1))
            p.drawLine(int(x), baseline - (5 if major else 3), int(x), baseline + (5 if major else 3))
            if label is not None:
                p.setPen(QColor(Palette.TEXT if anchor else Palette.TEXT_FAINT))
                p.drawText(QRectF(x - 30, baseline + 6, 60, 12), int(Qt.AlignmentFlag.AlignHCenter), label)
        t = 0.0
        while t <= fwd + 1e-6:
            major = (int(t) % 60 == 0)
            tick(self._on_x + t * eff, major, (("0" if t == 0 else _mmss(t)) if major else None), t == 0)
            t += 30.0
        t = 0.0
        while t <= bwd + 1e-6:
            major = (int(t) % 60 == 0)
            tick(self._off_x - t * eff, major, (("0" if t == 0 else "−" + _mmss(t)) if major else None), t == 0)
            t += 30.0
        tick(self._on_x - WARM_S * eff, False, "−" + _mmss(WARM_S))
        tick(self._off_x + COOL_S * eff, False, "+" + _mmss(COOL_S))
        fi = QFont(Fonts.SANS.split(",")[0].strip('"')); fi.setPixelSize(9); fi.setItalic(True)
        p.setFont(fi); p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(QRectF(PAD, baseline + 18, WARM_S * eff, 12), int(Qt.AlignmentFlag.AlignHCenter), "pre-roll")
        p.drawText(QRectF(self._off_x, baseline + 18, COOL_S * eff, 12), int(Qt.AlignmentFlag.AlignHCenter),
                   "cool-down")

    def _paint_seq_connect_drag(self, p):
        conn = self._connect
        if conn is None or not conn.get("moved"):
            return
        x1, y1 = conn["x"], conn["y"]
        cur = conn["cursor"]; x2, y2 = cur.x(), cur.y()
        tgt = conn.get("target")
        ok = tgt is not None
        col = QColor(Palette.ACCENT) if ok else QColor(Palette.TEXT_FAINT)
        if ok:
            x2, y2 = self.target_point(tgt)
            p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(QColor(Palette.ACCENT))
            p.drawEllipse(QPointF(x2, y2), 6.0, 6.0)
        pen = QPen(col, 2.2)
        pen.setStyle(Qt.PenStyle.SolidLine if ok else Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(); path.moveTo(x1, y1)
        path.cubicTo(x1 + (x2 - x1) * 0.5, y1, x1 + (x2 - x1) * 0.5, y2, x2, y2)
        p.drawPath(path)
        p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(col)
        p.drawEllipse(QPointF(x1, y1), 5.5, 5.5)
        if ok:
            off = tlm._snap((x1 - x2) / self.eff())
            label = f"{self._router._offset_chip_text(off)} after {self.target_label(tgt)}"
        else:
            label = "drop on a step edge, a sequence edge or an anchor line"
        self._router._paint_tag(p, x2, y2 - 16, label, ok)

    def _paint_seq_drag_readout(self, p):
        d = self._drag
        if d is None or not d.get("moved"):
            return
        n = d["node"]
        self._router._paint_tag(p, n.on_x, n.cy - 18, _fmt_off(float(n.item.on_air_offset_s)), True)

    # ── mouse ─────────────────────────────────────────────────────────────────
    def _pill_at(self, x: float, y: float) -> Optional[_SeqNode]:
        for r in self._rows:
            if r["type"] in ("seqc", "seqx"):
                n = r["node"]
                if r["type"] == "seqc":
                    ry, rh = n.row_y + 5, 36
                else:
                    ry, rh = n.row_y + n.row_h - 16, 18
                w = max(float(PILL_MIN_W), n.off_x - n.on_x)
                if n.on_x - 2 <= x <= n.on_x + w + 2 and ry - 14 <= y <= ry + rh:
                    return n
            elif r["type"] == "ulane":
                for n in r["nodes"]:
                    ry = r["y"] + 3 + n.lane * (ROW_SEQC_H - 6) + 2
                    w = max(float(PILL_MIN_W), n.off_x - n.on_x)
                    if n.on_x - 2 <= x <= n.on_x + w + 2 and ry <= y <= ry + 36:
                        return n
        return None

    def _unit_at(self, y: float) -> Optional[str]:
        for r in self._rows:
            if r["type"] == "unit" and r["y"] <= y <= r["y"] + r["h"]:
                return r["host"]
        return None

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position(); x, y = pos.x(), pos.y()
        self.setFocus()
        hit = self._handle_at(x, y)
        if hit is not None:
            _k, node, edge = hit
            hx, hy = self.seq_edge_point(node, edge)
            self._connect = {"node": node, "edge": edge, "x": hx, "y": hy, "cursor": pos,
                             "target": None, "moved": False, "press_x": x}
            self.select_node(node)
            return
        node = self._pill_at(x, y)
        if node is not None:
            self.select_node(node)
            self._drag = {"node": node, "press_x": x, "moved": False,
                          "on0": float(node.item.on_air_offset_s),
                          "off0": float(node.item.off_air_offset_s)}
            return
        self.select_node(None)

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position(); x, y = pos.x(), pos.y()
        if self._connect is not None:
            if not (e.buttons() & Qt.MouseButton.LeftButton):
                return
            if not self._connect["moved"] and abs(x - self._connect["press_x"]) < DRAG_THRESHOLD:
                return
            self._connect["moved"] = True
            self._connect["cursor"] = pos
            self._connect["target"] = self.external_target(e.globalPosition(),
                                                           src_node=self._connect["node"])
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        if self._drag is None:
            if self._handle_at(x, y) is not None:
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self._pill_at(x, y) is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self._update_tooltip(x, y, e.globalPosition().toPoint())
            return
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        d = self._drag
        if not d["moved"] and abs(x - d["press_x"]) < DRAG_THRESHOLD:
            return
        d["moved"] = True
        n = d["node"]; it = n.item
        ds = tlm._snap((x - d["press_x"]) / self.eff())
        if (it.on_air_anchor or "plan") == "plan" and (it.on_air_anchor_edge or "on") == "on":
            ds = max(-d["on0"], ds)                       # never before the plan's on-air
        if (it.off_air_anchor or "plan") == "plan" and (it.off_air_anchor_edge or "off") == "off":
            ds = min(-d["off0"], ds)                      # never after the plan's off-air
        it.on_air_offset_s = d["on0"] + ds
        fixed_len = (it.off_air_anchor == "item" and not it.off_air_anchor_item)
        if not fixed_len:
            it.off_air_offset_s = d["off0"] + ds
        self.relayout()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._connect is not None:
            conn = self._connect
            self._connect = None
            if conn["moved"] and conn["target"] is not None:
                self.apply_cross_anchor(("seq", conn["node"], conn["edge"]), conn["target"])
            else:
                self.update()
            return
        if self._drag is None:
            return
        d = self._drag
        self._drag = None
        if d["moved"]:
            self.relayout()
            self.changed.emit()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        node = self._pill_at(e.position().x(), e.position().y())
        if node is not None:
            self.toggle_expanded(node)

    def wheelEvent(self, e):  # noqa: N802
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            if delta:
                self.zoom_by(1.0 + ZOOM_WHEEL * delta, e.position().x())
            e.accept()
        else:
            e.ignore()

    def _update_tooltip(self, x, y, global_pt) -> None:
        node = self._pill_at(x, y)
        if node is None:
            QToolTip.hideText()
            return
        it = node.item
        g = self.graph()
        lines = [f"<b>{it.sequence_name or it.sequence_id}</b> · {it.unit_label or it.hostname}"]
        for edge, pre in (("on", "on_air"), ("off", "off_air")):
            lines.append(f"{edge}-air: {self._ref_text(node, pre)}")
        c = g.item_timing(it.id)
        if c.on is not None and c.off is not None:
            lines.append(f"resolves {self._clock_text(c.on)} → {self._clock_text(c.off)}")
        if str(node.uid) in self._conflicts:
            lines.append(f"<span style='color:{Palette.CRASH}'>⚠ overlaps another sequence on this unit</span>")
        QToolTip.showText(global_pt, "<br>".join(lines), self)

    def _clock_text(self, c: pg.Clocked) -> str:
        return f"{_fmt_off(c[1])} from the plan's {'on' if c[0] == 'on' else 'off'}-air"

    def _ref_text(self, node: _SeqNode, pre: str) -> str:
        it = node.item
        anchor = getattr(it, f"{pre}_anchor") or "plan"
        off = float(getattr(it, f"{pre}_offset_s") or 0.0)
        if anchor == "plan":
            e = getattr(it, f"{pre}_anchor_edge") or ("on" if pre == "on_air" else "off")
            return f"{_fmt_off(off)} from the plan's {e}-air"
        t = self.node_by_id(getattr(it, f"{pre}_anchor_item") or it.id)
        tname = (t.item.sequence_name if t else "?") if t is not node else "its own"
        if anchor == "item":
            return f"{_fmt_off(off)} after {tname} {getattr(it, f'{pre}_anchor_edge')}-air"
        return f"{_fmt_off(off)} after a step's {getattr(it, f'{pre}_anchor_edge')} in {tname}"

    # ── context menus ─────────────────────────────────────────────────────────
    def contextMenuEvent(self, e):  # noqa: N802
        x, y = e.pos().x(), e.pos().y()
        node = self._pill_at(x, y)
        if node is not None:
            self.select_node(node)
            self.open_seq_menu(node, e.globalPos())
            return
        host = self._unit_at(y)
        if host is not None:
            self.open_unit_menu(host, e.globalPos())

    def open_seq_menu(self, node: _SeqNode, global_pos) -> None:
        it = node.item
        menu = QMenu(self)
        a_exp = menu.addAction("Collapse" if node.expanded else "Expand")
        a_set = menu.addAction("Unit / source / recovery…")
        menu.addSeparator()
        a_don = menu.addAction("Detach on-air (keep in place)")
        a_don.setEnabled((it.on_air_anchor or "plan") != "plan")
        a_doff = menu.addAction("Detach off-air (keep in place)")
        a_doff.setEnabled((it.off_air_anchor or "plan") != "plan")
        a_fix = menu.addAction("Fixed length (off-air follows on-air)")
        a_fix.setCheckable(True)
        a_fix.setChecked(it.off_air_anchor == "item" and not it.off_air_anchor_item)
        menu.addSeparator()
        a_bar = menu.addAction("Add duration task…")
        a_run = menu.addAction("Add one-shot…")
        a_tune = menu.addAction("Add tune…")
        a_ramp = menu.addAction("Add ramp…")
        menu.addSeparator()
        a_dup = menu.addAction("Duplicate onto another unit…")
        a_rm = menu.addAction("Remove from plan")
        act = menu.exec(global_pos)
        if act is None:
            return
        if act is a_exp:
            self.toggle_expanded(node)
        elif act is a_set:
            self._owner.edit_sequence(node)
        elif act is a_don:
            self.detach_edge(node, "on")
        elif act is a_doff:
            self.detach_edge(node, "off")
        elif act is a_fix:
            self.set_fixed_length(node, a_fix.isChecked())
        elif act in (a_bar, a_run, a_tune, a_ramp):
            kind = {a_bar: "bar", a_run: "run", a_tune: "tune", a_ramp: "ramp"}[act]
            if not node.expanded:
                self.toggle_expanded(node)
            node.canvas.add_new(kind)
        elif act is a_dup:
            self._owner.duplicate_sequence(node)
        elif act is a_rm:
            self._owner.remove_sequence(node)

    def set_fixed_length(self, node: _SeqNode, fixed: bool) -> None:
        it = node.item
        tm = self._last_timing.get(node.uid)
        if fixed:
            if tm and tm.on is not None and tm.off is not None and tm.on[0] == tm.off[0]:
                length = tm.off[1] - tm.on[1]
            else:
                length = max(60.0, (node.off_x - node.on_x) / self.eff())
            it.off_air_anchor, it.off_air_anchor_item = "item", ""
            it.off_air_anchor_edge, it.off_air_anchor_step = "on", ""
            it.off_air_offset_s = tlm._snap(max(1.0, length))
        else:
            self.detach_edge(node, "off")
            return
        self.relayout()
        self.changed.emit()

    def open_unit_menu(self, host: str, global_pos) -> None:
        menu = QMenu(self)
        a_add = menu.addAction("Add sequence to this unit…")
        a_fold = menu.addAction("Unfold unit" if self.is_folded(host) else "Fold unit into lanes")
        menu.addSeparator()
        a_exp = menu.addAction("Expand all its sequences")
        a_col = menu.addAction("Collapse all its sequences")
        act = menu.exec(global_pos)
        if act is a_add:
            self._owner.add_sequence(preselect_host=host)
        elif act is a_fold:
            self.toggle_folded(host)
        elif act is a_exp:
            self.set_all_expanded(True, host)
        elif act is a_col:
            self.set_all_expanded(False, host)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            n = self.selected_node()
            if n is not None and self._sel_canvas is None:
                self._owner.remove_sequence(n)
                return
        super().keyPressEvent(e)

    # ── layout info for the header column ─────────────────────────────────────
    def rows(self) -> List[dict]:
        return list(self._rows)

    def conflicts(self) -> set:
        return set(self._conflicts)

    def conflict_message(self) -> str:
        """The banner text for the channel conflicts ("" when there are none): the first clashing
        pair, named, and the KIND of clash — the two on air at the same time, or only the later
        one's warm-up (lead-in) starting before the earlier one's cool-down (tail) ends, with the
        gap between the two windows that would clear it (the agent's arm rule: lead-in + tail)."""
        pairs = self._conflict_pairs
        if not pairs:
            return ""
        by = {str(n.uid): n for n in self._nodes}
        a, b = by.get(pairs[0][0]), by.get(pairs[0][1])
        if a is None or b is None:
            return "⚠ Channel conflict — overlapping sequences on one unit"
        first, second = (a, b) if a.on_x <= b.on_x else (b, a)
        unit = first.item.unit_label or first.item.hostname
        n1 = first.item.sequence_name or first.item.sequence_id
        n2 = second.item.sequence_name or second.item.sequence_id
        more = f" (+{len(pairs) - 1} more)" if len(pairs) > 1 else ""
        if first.on_x < second.off_x and second.on_x < first.off_x:
            return (f"⚠ Channel conflict on {unit} — “{n1}” and “{n2}” are on air at the same time "
                    f"(one TX channel per unit){more}")
        eff = self.eff()
        tail = max(0.0, first.span[1] - first.off_x) / eff
        lead = max(0.0, second.on_x - second.span[0]) / eff
        need = int(math.ceil(tail + lead - 1e-6))
        return (f"⚠ Channel conflict on {unit} — “{n2}”'s warm-up starts before “{n1}”'s cool-down ends "
                f"(one TX channel per unit): leave ≥ {need} s between “{n1}” off-air and “{n2}” on-air{more}")

    def first_fault(self) -> Optional[str]:
        for n in self._nodes:
            if n.fault:
                return n.fault
        return None


# ── The plan's row-header column (sticky at the left) ─────────────────────────

class _PlanHeaderColumn(QWidget):
    """Units · sequences · steps, aligned to the stage's rows. Hosts each expanded sequence's
    embedded `_RowHeader` (the sequence editor's own row labels) one level indented."""

    def __init__(self, stage: _PlanStage, owner: "PlanTimelineEditor"):
        super().__init__()
        self._stage = stage
        self._owner = owner
        self.setFixedWidth(PLAN_HDR_W)
        self.setMouseTracking(True)
        self.setAutoFillBackground(True)

    def sync(self) -> None:
        self.setFixedHeight(max(1, self._stage.height()))
        for n in self._stage.nodes():
            rh = n.editor._rowhdr_e
            if n.expanded and n.canvas.isVisible():
                rh.setParent(self)
                rh.setGeometry(PLAN_HDR_W - _RowHeader.HDR_W, int(n.canvas_y), _RowHeader.HDR_W,
                               n.canvas.content_height())
                rh.show()
                rh.refresh()
            else:
                rh.hide()
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(Palette.SURFACE))
        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 1))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        fcap = QFont(Fonts.SANS.split(",")[0].strip('"')); fcap.setPixelSize(10); fcap.setBold(True)
        p.setFont(fcap); p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(14, 4, self.width() - 24, 14,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "UNITS · SEQUENCES · STEPS")
        fn = QFont(Fonts.SANS.split(",")[0].strip('"')); fn.setPixelSize(12); fn.setBold(True)
        fs = QFont(Fonts.SANS.split(",")[0].strip('"')); fs.setPixelSize(10)
        for r in self._stage.rows():
            y, h = r["y"], r["h"]
            if r["type"] == "unit":
                p.fillRect(QRectF(0, y, self.width() - 1, h), QColor(Palette.SURFACE_ALT))
                p.setPen(QPen(QColor(Palette.BORDER), 1))
                p.drawLine(0, int(y + h), self.width() - 1, int(y + h))
                self._chevron(p, 12, y + h / 2, not self._stage.is_folded(r["host"]))
                label, status = self._stage._unit_state(r["host"])
                led = QColor(Palette.ONLINE if status == "online" else (Palette.CRASH if status == "offline" else Palette.IDLE))
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(led)
                p.drawEllipse(QPointF(28, y + h / 2), 4, 4)
                p.setFont(fn); p.setPen(QColor(Palette.TEXT))
                p.drawText(QRectF(38, y + 2, self.width() - 80, 16), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           QFontMetrics(fn).elidedText(label, Qt.TextElideMode.ElideRight, self.width() - 84))
                p.setFont(fs); p.setPen(QColor(Palette.TEXT_FAINT))
                p.drawText(QRectF(38, y + h - 15, self.width() - 80, 12), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           QFontMetrics(fs).elidedText(f"{r['host']}{(' · ' + status) if status else ''}",
                                                       Qt.TextElideMode.ElideRight, self.width() - 84))
                n = r["n"]
                self._badge(p, f"{n} seq{'' if n == 1 else 's'}", y + h / 2)
            elif r["type"] in ("seqc", "seqx"):
                node = r["node"]; it = node.item
                sel = node.uid == self._stage._sel_node
                if sel:
                    p.fillRect(QRectF(0, y, self.width() - 1, h), QColor(Palette.ACCENT_SOFT))
                self._chevron(p, 26, y + h / 2, node.expanded)
                p.setFont(fn); p.setPen(QColor(Palette.TEXT))
                name = it.sequence_name or it.sequence_id
                p.drawText(QRectF(40, y + 1, self.width() - 100, 16), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           QFontMetrics(fn).elidedText(name, Qt.TextElideMode.ElideRight, self.width() - 104))
                on = node.anchor_desc("on")[1]; off = node.anchor_desc("off")[1]
                steps = len(node.canvas.items())
                p.setFont(fs); p.setPen(QColor(Palette.TEXT_FAINT))
                sub = f"{on} → {off} · {steps} step{'' if steps == 1 else 's'}"
                p.drawText(QRectF(40, y + h - 15, self.width() - 60, 12), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           QFontMetrics(fs).elidedText(sub, Qt.TextElideMode.ElideRight, self.width() - 64))
                pol = (it.recovery_policy or "").strip()
                if pol:
                    self._badge(p, "↻ auto" if pol == "auto" else "operator", y + 9,
                                accent=(pol == "auto"))
            elif r["type"] == "ulane":
                p.setFont(fs); p.setPen(QColor(Palette.TEXT_FAINT))
                p.drawText(QRectF(40, y, self.width() - 50, h), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           f"{len(r['nodes'])} sequences folded into {r['lanes']} lane{'' if r['lanes'] == 1 else 's'}")
        p.end()

    def _chevron(self, p, x, cy, open_: bool):
        p.setPen(QPen(QColor(Palette.TEXT_FAINT), 1.6)); p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        if open_:
            path.moveTo(x - 4, cy - 2); path.lineTo(x, cy + 2); path.lineTo(x + 4, cy - 2)
        else:
            path.moveTo(x - 2, cy - 4); path.lineTo(x + 2, cy); path.lineTo(x - 2, cy + 4)
        p.drawPath(path)

    def _badge(self, p, text, cy, accent=False):
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(8); f.setBold(True)
        p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 10
        r = QRectF(self.width() - w - 12, cy - 7, w, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(Palette.ARMED_SOFT if accent else Palette.INSET))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor(Palette.ARMED if accent else Palette.TEXT_MUTED))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)

    def _row_at(self, y: float) -> Optional[dict]:
        for r in self._stage.rows():
            if r["y"] <= y <= r["y"] + r["h"]:
                return r
        return None

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        r = self._row_at(e.position().y())
        if r is None:
            return
        if r["type"] == "unit":
            if e.position().x() < 22:
                self._stage.toggle_folded(r["host"])
            return
        if r["type"] in ("seqc", "seqx"):
            node = r["node"]
            if e.position().x() < 36:
                self._stage.toggle_expanded(node)
            else:
                self._stage.select_node(node)
            return
        if r["type"] == "canvas":
            node = r["node"]; c = node.canvas
            ly = e.position().y() - node.canvas_y
            for row in c.row_layout():
                if row["y"] - 2 <= ly <= row["y"] + LANE_H + 2:
                    c._select_only(row["item"].uid); c.update()
                    return

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        r = self._row_at(e.position().y())
        if r is not None and r["type"] in ("seqc", "seqx"):
            self._stage.toggle_expanded(r["node"])
        elif r is not None and r["type"] == "canvas":
            node = r["node"]; c = node.canvas
            ly = e.position().y() - node.canvas_y
            for row in c.row_layout():
                if row["y"] - 2 <= ly <= row["y"] + LANE_H + 2:
                    c.edit_item(row["item"])
                    return

    def contextMenuEvent(self, e):  # noqa: N802
        r = self._row_at(e.pos().y())
        if r is None:
            return
        if r["type"] == "unit":
            self._stage.open_unit_menu(r["host"], e.globalPos())
        elif r["type"] in ("seqc", "seqx"):
            self._stage.select_node(r["node"])
            self._stage.open_seq_menu(r["node"], e.globalPos())


class _PlanSheet(QWidget):
    """The scrollable content: the stage at the right of the sticky header column."""

    def __init__(self, stage: _PlanStage, header: _PlanHeaderColumn):
        super().__init__()
        self._stage = stage
        self._header = header
        stage.setParent(self)
        header.setParent(self)
        stage.move(PLAN_HDR_W, 0)
        header.move(0, 0)
        header.raise_()

    def sync(self) -> None:
        self._header.sync()
        self.setFixedSize(PLAN_HDR_W + self._stage.width(), self._stage.height())

    def set_hscroll(self, v: int) -> None:
        self._header.move(int(v), 0)
        self._header.raise_()
        self._stage.set_hscroll(int(v))


# ── The plan timeline editor widget ───────────────────────────────────────────

class _Legend(QWidget):
    def __init__(self, owner: "PlanTimelineEditor"):
        super().__init__()
        self._owner = owner
        self.setFixedHeight(22)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(10); p.setFont(f)
        fm = QFontMetrics(f)
        x = 4.0
        hues: Dict[str, str] = {}
        for n in self._owner._stage.nodes():
            for task, hue in n.canvas._hue.items():
                hues.setdefault(task, hue)
        for task, hue in hues.items():
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(hue))
            p.drawRoundedRect(QRectF(x, 7, 9, 9), 2.5, 2.5)
            p.setPen(QColor(Palette.TEXT_MUTED))
            p.drawText(QRectF(x + 13, 0, 400, 22), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), task)
            x += 13 + fm.horizontalAdvance(task) + 16
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(QRectF(x + 8, 0, 800, 22), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   "sequence frame: green edge = its on-air, red = its off-air · hatched stubs = warm-up / "
                   "cool-down outside its window · drag an edge dot onto another to anchor")
        p.end()


class PlanTimelineEditor(QWidget):
    """Toolbar (Add sequence…, the step tools for the selected sequence, expand / collapse, fit
    and zoom) above the plan stage. Mirrors the sequence editor's API where the dialogs rely on
    it: set_items / items / is_empty / set_sequences / add_sequence / edit_sequence."""

    changed = pyqtSignal()

    def __init__(self, hub: DataHub, sequences_by_host: Dict[str, List[m.Sequence]],
                 parent=None):
        super().__init__(parent)
        self._hub = hub
        self._seqs = sequences_by_host
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        _chip = (f"QPushButton {{ border:1px solid {Palette.BORDER}; border-radius:999px; "
                 f"padding:5px 12px; background:{Palette.SURFACE}; color:{Palette.TEXT_MUTED}; "
                 f"font-weight:600; font-size:12px; }} "
                 f"QPushButton:hover {{ color:{Palette.TEXT}; border-color:{Palette.BORDER_STRONG}; }} "
                 f"QPushButton:disabled {{ color:{Palette.TEXT_FAINT}; }}")
        bar = QHBoxLayout()
        self._add = QPushButton("Add sequence…")
        self._add.setToolTip("Copy a library sequence onto a unit in this plan")
        self._add.setStyleSheet(_chip); self._add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add.clicked.connect(lambda: self.add_sequence())
        bar.addWidget(self._add)
        self._step_tools: List[QPushButton] = []
        for label, kind, tip in (("Duration", "bar", "A task that runs across the sequence's window"),
                                 ("One-shot", "run", "A task that fires once and exits"),
                                 ("Tune", "tune", "Change a running task's live parameters"),
                                 ("Ramp", "ramp", "Sweep a running task's live parameter")):
            b = QPushButton(label); b.setToolTip(tip + " — added to the selected sequence")
            b.setStyleSheet(_chip); b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIcon(_tool_icon(kind)); b.setIconSize(QSize(15, 15))
            b.clicked.connect(lambda _=False, k=kind: self._add_step(k))
            bar.addWidget(b); self._step_tools.append(b)
        bar.addStretch(1)
        self._hint = QLabel("select a sequence to add steps to it")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        self._exp = QPushButton("Expand all"); self._col = QPushButton("Collapse all")
        self._exp.clicked.connect(lambda: self._stage.set_all_expanded(True))
        self._col.clicked.connect(lambda: self._stage.set_all_expanded(False))
        self._fit_btn = QPushButton("Fit")
        self._fit_btn.setToolTip("Fit the whole plan to the view")
        self._fit_btn.clicked.connect(self._fit)
        for b in (self._exp, self._col, self._fit_btn):
            b.setStyleSheet(_chip); b.setCursor(Qt.CursorShape.PointingHandCursor); bar.addWidget(b)
        zoomw = QFrame(); zoomw.setObjectName("zoomseg")
        zoomw.setStyleSheet(
            f"#zoomseg {{ border:1px solid {Palette.BORDER}; border-radius:999px; background:{Palette.SURFACE}; }} "
            f"#zoomseg QPushButton {{ border:none; background:transparent; color:{Palette.TEXT_MUTED}; "
            f"padding:2px 10px; font-size:15px; }} #zoomseg QPushButton:hover {{ color:{Palette.TEXT}; }} "
            f"#zoomseg QLabel {{ color:{Palette.TEXT_MUTED}; font-size:11px; border-left:1px solid {Palette.BORDER}; "
            f"border-right:1px solid {Palette.BORDER}; padding:2px 6px; }}")
        zh = QHBoxLayout(zoomw); zh.setContentsMargins(0, 0, 0, 0); zh.setSpacing(0)
        zo = QPushButton("−"); zi = QPushButton("+")
        zo.clicked.connect(lambda: self._stage.zoom_by(1 / 1.15))
        zi.clicked.connect(lambda: self._stage.zoom_by(1.15))
        self._zoom_btn = QLabel("100%"); self._zoom_btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_btn.setFixedWidth(44)
        zh.addWidget(zo); zh.addWidget(self._zoom_btn); zh.addWidget(zi)
        bar.addWidget(zoomw)
        outer.addLayout(bar)

        self._ready = QLabel("")
        self._ready.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._ready)

        self._stage = _PlanStage(self)
        self._header = _PlanHeaderColumn(self._stage, self)
        self._sheet = _PlanSheet(self._stage, self._header)
        self._stage.changed.connect(self._on_changed)
        self._stage.selection_changed.connect(self._sync_tools)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setWidget(self._sheet)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setMinimumHeight(260)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 10px; }}")
        self._scroll.horizontalScrollBar().valueChanged.connect(self._sheet.set_hscroll)
        outer.addWidget(self._scroll, stretch=1)
        self._legend = _Legend(self)
        outer.addWidget(self._legend)
        self._did_autofit = False
        self._sync_tools()
        self._stage.relayout()

    # ── hooks from the stage ──────────────────────────────────────────────────
    def _on_stage_relayout(self) -> None:
        self._sheet.sync()
        self._zoom_btn.setText(f"{round(self._stage.zoom() * 100)}%")
        self._legend.update()
        conflicts = self._stage.conflicts()
        fault = self._stage.first_fault()
        if fault:
            self._ready.setText(f"⚠ {fault}")
            self._ready.setStyleSheet(f"font-size: 11px; color: {Palette.CRASH}; font-weight: 600;")
        elif conflicts:
            self._ready.setText(self._stage.conflict_message())
            self._ready.setStyleSheet(f"font-size: 11px; color: {Palette.CRASH}; font-weight: 600;")
        else:
            n = len(self._stage.nodes())
            self._ready.setText(f"● Ready · {n} sequence{'' if n == 1 else 's'} · no channel conflicts"
                                if n else "add a sequence to begin")
            self._ready.setStyleSheet(f"font-size: 11px; color: {Palette.ONLINE if n else Palette.TEXT_FAINT};")

    def _on_changed(self) -> None:
        self._sync_tools()
        self.changed.emit()

    def _sync_tools(self) -> None:
        n = self._stage.target_node()
        for b in self._step_tools:
            b.setEnabled(n is not None)
        if n is not None:
            self._hint.setText(f"adds to {n.item.sequence_name or n.item.sequence_id} · "
                               f"{n.item.unit_label or n.item.hostname}")
        else:
            self._hint.setText("select a sequence to add steps to it")
        self._header.update()

    def _add_step(self, kind: str) -> None:
        n = self._stage.target_node()
        if n is None:
            return
        if not n.expanded:
            self._stage.toggle_expanded(n)
        n.canvas.add_new(kind)

    def _zoom_to(self, zoom: float, cursor_x: Optional[float]) -> None:
        old = self._stage.zoom()
        hbar = self._scroll.horizontalScrollBar()
        if cursor_x is None:
            cursor_x = (hbar.value() - PLAN_HDR_W) + self._scroll.viewport().width() / 2
        vp_x = PLAN_HDR_W + cursor_x - hbar.value()
        self._stage.set_zoom(zoom)
        ratio = self._stage.zoom() / old
        new_x = PAD + (cursor_x - PAD) * ratio
        hbar.setValue(int(round(PLAN_HDR_W + new_x - vp_x)))

    def _fit(self) -> None:
        vp = self._scroll.viewport().width() - PLAN_HDR_W - 6
        nat = self._stage.width() / max(self._stage.zoom(), 1e-6)
        if nat <= 0 or vp <= 0:
            return
        z = max(ZOOM_MIN, min(1.5, vp / nat))
        self._stage.set_zoom(z)
        self._scroll.horizontalScrollBar().setValue(0)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        if not self._did_autofit and self._stage.nodes():
            self._did_autofit = True
            QTimer.singleShot(0, self._fit)

    # ── API used by the dialogs ───────────────────────────────────────────────
    def available_tasks(self) -> List[str]:
        return []

    def set_sequences(self, sequences_by_host: Dict[str, List[m.Sequence]]) -> None:
        self._seqs = sequences_by_host
        self._stage.set_units(list(sequences_by_host))

    def _make_node(self, item: m.PlanItem) -> _SeqNode:
        item = item.model_copy(deep=True)
        if not item.id:
            item.id = pg.new_item_id()
        ed = _EmbeddedEditor()
        _load_unit_meta(ed, self._hub, item.hostname)
        steps = list(item.steps)
        if not steps:                                  # legacy: seed from the library source + overrides
            src = next((s for s in self._seqs.get(item.hostname, []) if s.id == item.sequence_id), None)
            if src is not None:
                steps = [s.model_copy(deep=True) for s in src.steps]
                for ov in item.overrides:
                    if 0 <= ov.index < len(steps):
                        steps[ov.index].args = list(ov.args)
                        steps[ov.index].replace_args = ov.replace_args
                item.overrides = []
        ed.set_steps(steps)
        return _SeqNode(item, ed)

    def set_items(self, items: List[m.PlanItem]) -> None:
        nodes = [self._make_node(it) for it in items]
        units = list(self._seqs) or []
        self._stage.set_nodes(nodes, units)
        self._did_autofit = False

    def items(self) -> List[m.PlanItem]:
        out: List[m.PlanItem] = []
        for n in self._stage.nodes():
            out.append(n.item.model_copy(update={"steps": n.editor.steps(), "expanded": n.expanded}))
        return out

    def is_empty(self) -> bool:
        return not self._stage.nodes()

    def validate(self) -> Optional[str]:
        fault = self._stage.first_fault()
        if fault:
            return fault
        for n in self._stage.nodes():
            err = n.editor.validate()
            if err:
                return f"{n.item.sequence_name or n.item.sequence_id}: {err}"
        return None

    def add_sequence(self, preselect_host: str = "") -> None:
        if not self._seqs:
            self._hint.setText("no units configured — add units in units.yaml first")
            return
        dlg = PlanItemDialog(self._hub, self._seqs, parent=self.window(), preselect_host=preselect_host)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self._stage.add_node(self._make_node(dlg.result_item))

    def edit_sequence(self, node: _SeqNode) -> None:
        item = node.item.model_copy(update={"steps": node.editor.steps()})
        dlg = PlanItemDialog(self._hub, self._seqs, item=item, parent=self.window())
        r = dlg.exec()
        if r == PlanItemDialog.REMOVE:
            self._stage.remove_node(node)
        elif r == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            new = dlg.result_item
            reseed = (new.hostname != node.item.hostname or new.sequence_id != node.item.sequence_id)
            for f_ in ("hostname", "unit_label", "sequence_id", "sequence_name", "recovery_policy",
                       "recovery_mode"):
                setattr(node.item, f_, getattr(new, f_))
            if reseed:
                _load_unit_meta(node.editor, self._hub, new.hostname)
                node.editor.set_steps(list(new.steps))
                if new.hostname not in self._stage.units():
                    self._stage.set_units(self._stage.units() + [new.hostname])
            self._stage.relayout()
            self.changed.emit()

    def duplicate_sequence(self, node: _SeqNode) -> None:
        item = node.item.model_copy(update={"steps": node.editor.steps(), "id": pg.new_item_id()})
        dlg = PlanItemDialog(self._hub, self._seqs, item=item, parent=self.window())
        dlg.setWindowTitle("Duplicate onto a unit")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            new = dlg.result_item.model_copy(update={"id": pg.new_item_id(), "steps": list(item.steps)})
            # the copy hangs off the plan's own anchors at the original's resolved instants
            tm = self._stage._last_timing.get(node.uid)
            if tm and tm.on is not None and tm.off is not None:
                new.on_air_anchor, new.on_air_anchor_item, new.on_air_anchor_step = "plan", "", ""
                new.on_air_anchor_edge, new.on_air_offset_s = tm.on[0], tlm._snap(tm.on[1])
                new.off_air_anchor, new.off_air_anchor_item, new.off_air_anchor_step = "plan", "", ""
                new.off_air_anchor_edge, new.off_air_offset_s = tm.off[0], tlm._snap(tm.off[1])
            self._stage.add_node(self._make_node(new))

    def remove_sequence(self, node: _SeqNode) -> None:
        self._stage.remove_node(node)


# ── The whole plan ───────────────────────────────────────────────────────────

class PlanEditorDialog(QDialog):
    """Create or edit a plan: name, description, and the plan timeline (every unit's sequences
    on one timeline). Fetches the library's sequences once so the add-sequence picker opens
    instantly. On accept, the finished Plan is available as .result_plan (the caller persists it)."""

    def __init__(self, hub: DataHub, plan: Optional[m.Plan] = None, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._editing = plan is not None
        self._plan_id = plan.id if plan else None
        self._seqs_by_host: Dict[str, List[m.Sequence]] = {}
        self._seqs_loaded = False
        self.result_plan: Optional[m.Plan] = None

        self.setWindowTitle("Edit plan" if self._editing else "New plan")
        self.setMinimumSize(900, 600)
        self._build()
        fit_dialog_to_screen(self, 1180, 760)   # relax the floor + cap to a short/scaled screen
        self._load_sequences()
        if plan is not None:
            self._name.setText(plan.name)
            self._desc.setPlainText(plan.description)
            self._timeline.set_items(plan.items)
        self._hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())

    def _build(self) -> None:
        from .dialog_style import editor_qss
        self.setStyleSheet(editor_qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("unique plan name")
        form.addRow("Name *", self._name)
        from .desc_widget import description_editor
        self._desc = description_editor()
        form.addRow("Description", self._desc)
        outer.addLayout(form)
        self._timeline = PlanTimelineEditor(self._hub, self._seqs_by_host)
        self._timeline.changed.connect(self._refresh_status)
        outer.addWidget(self._timeline, stretch=1)
        self._status = QLabel("loading units…")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    def _load_sequences(self) -> None:
        """Seed the add-sequence picker from the LIBRARY, not from live units: every unit runs the
        same shared sequences (they differ only in the parameters a plan sets). A local read, so a
        plan can be built with no unit connected. The unit list is the fleet's configured units."""
        try:
            lib_seqs = self._hub.fleet.get(LIBRARY_HOST).list_sequences()
        except Exception:  # noqa: BLE001 — no library ⇒ author with none
            lib_seqs = []
        hosts = self._hub.fleet.hostnames()

        def _for(host: str) -> List[m.Sequence]:
            try:
                utype = self._hub.fleet.get(host).unit_type
            except Exception:  # noqa: BLE001
                utype = m.DEFAULT_UNIT_TYPE
            return [s for s in lib_seqs if m.applies_to_type(s.types, utype)]
        by_host: Dict[str, List[m.Sequence]] = {h: _for(h) for h in hosts}
        self._seqs_loaded = True
        self._seqs_by_host = by_host
        self._timeline.set_sequences(by_host)
        n_seq = len(lib_seqs)
        if not hosts:
            self._status.setText(f"{n_seq} library sequence(s) · no units configured")
        elif n_seq == 0:
            self._status.setText(f"{len(hosts)} unit(s) · no sequences in the library — "
                                 f"create one in the Library first")
        else:
            self._status.setText(f"{len(hosts)} unit(s) · {n_seq} library sequence(s) available")

    def _refresh_status(self) -> None:
        err = self._timeline.validate()
        if err:
            self._status.setText(err)
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
        else:
            n = len(self._timeline._stage.nodes())
            self._status.setText(f"{n} sequence(s) — ready")
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

    def _on_task_done(self, label: str, result) -> None:
        return

    def _on_save(self) -> None:
        name = self._name.text().strip()
        if not name:
            self._status.setText("plan name is required")
            return
        if self._timeline.is_empty():
            self._status.setText("add at least one sequence")
            return
        err = self._timeline.validate()
        if err:
            self._status.setText(err)
            self._status.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
            return
        from state import new_plan_id
        self.result_plan = m.Plan(
            id=self._plan_id or new_plan_id(),
            name=name,
            description=self._desc.toPlainText().strip(),
            items=self._timeline.items(),
        )
        self.accept()

    def _disconnect(self) -> None:
        try:
            self._hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
