"""
TimelineEditor — a visual, drag-and-drop editor for a sequence's steps.

A sequence choreographs tasks around ONE on-air window, anchored at two points
(see agent/sequence_runner.py):

    ON-AIR  (T0)     — offset ≤ 0 is warm-up (before RF), 0 is on-air.
    OFF-AIR (T_end)  — offset ≥ 0 is cool-down (after RF), 0 is off-air.

Two kinds of timeline object, matching the two ways a task runs:

  • Duration task → a **bar** with two handles: a START handle on the on-air side
    and a STOP handle on the off-air side. It compiles to two steps for one task
    (start + stop) and is added / removed as a single unit — you can't have a
    lone start or a lone stop.

  • One-shot task → a **run pill**: a single point (action="run") that fires and
    exits. There can be many of these (e.g. one attenuator-set per value), and
    they don't occupy a task slot, so nothing needs to stop them.

Everything is placed to scale from its anchor at a fixed pixels-per-second, so
dragging maps linearly to an offset. The on-air band between the anchors is to
scale too, but it widens as needed so on-air-anchored points always stay left of
off-air-anchored ones (their exact window length isn't known until arm time).

Interaction:
  - Drag a bar's START handle (on-air side) or STOP handle (off-air side); neither
    crosses the middle. Drag the bar body to shift both together.
  - Drag a run pill to change its offset (its anchor is set only in the editor).
  - Click a bar or pill to open its editor: pick the task, choose its parameters
    with the full parameter form (pre-filled from the task, never mutating it),
    set the offsets, or Remove it.
  - "+ Duration" / "+ One-shot" add a new object.
  - Zoom the time axis horizontally with Ctrl+scroll (mouse) or a pinch
    (touchpad); the zoom readout in the toolbar resets to 100% on click.

All geometry / conversion logic lives in timeline_model.py (no Qt, unit-tested);
this module is the Qt view + the step editor over it. No network I/O happens in
the canvas; the step editor fetches a script's parameter schema via the hub.
"""
from __future__ import annotations

import copy
import shlex
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea,
    QToolTip, QVBoxLayout, QWidget,
)

from api import models as m
from . import timeline_model as tlm
from api import ramp as _ramp
from api.fleet import LIBRARY_HOST

from .duration_spin import DurationSpinBox
from .param_form import ParamForm, fmt_duration, fmt_value, hz_per_unit, power_mode_of_args
from .ramp_editor import RampEditorDialog
from .theme import Fonts, Palette, mono_font
from .widgets import fit_dialog_to_screen

# ── View geometry (paint sizes; timing geometry lives in timeline_model) ──────
# Redesign: one row per item (Gantt-style), a compact capsule/pin visual language,
# a real-time axis, and per-task colour. See docs/sequence-editor-mockup.html.
LANES_TOP = 20              # y of the first row
LANE_H = 30                 # bar / pin row height
LANE_VGAP = 12              # vertical gap between rows
CAPTION_H = 16              # timing-pill height (Hold offset chip)
AXIS_GAP = 16               # gap between the last row and the time axis
BASELINE_FROM_BOTTOM = 50   # (legacy) unused now the axis rides under the rows
BAR_R = 9                   # capsule corner radius
HUE_RAIL = 3               # left hue rail width inside a bar
HANDLE_W = 10               # drawn width of a bar's grip
HANDLE_HIT = 11             # px each side of a handle centre that grabs it
PIN_HIT = 10                # px around a pin / ramp edge dot that starts a drag-to-anchor
SNAP_PX = 7                 # px a dragged edge snaps to a nearby edge / anchor / tick
HOLD_HIT = 9                # px each side of the Hold divider that grabs it
RUN_MIN_W = 120             # minimum run-pill width
RUN_MAX_W = 260
RAMP_MIN_W = 44             # minimum ramp-bar width (so a short/zero-span ramp is clickable)
RAMP_CAP_W = 40             # ramp trend end-cap width (holds the rising/falling slope mark)
CARET_W = 20                # (legacy) inline-panel caret zone — panels dropped in the redesign
TICK_S = 30                 # base tick interval (seconds); adapts with zoom
DRAG_THRESHOLD = 4          # px of movement before a press counts as a drag

# Horizontal (time-axis) zoom
ZOOM_MIN = 0.25             # zoomed all the way out
ZOOM_MAX = 6.0              # zoomed all the way in
ZOOM_WHEEL = 0.0018         # zoom change per unit of Ctrl+wheel angle-delta
MIN_TICK_PX = 46            # keep tick labels at least this far apart
TICK_CHOICES = (5, 10, 15, 30, 60, 120, 300, 600)

# Inline argument panel (shown under a task by default; collapsed via the caret)
ARG_ROW_H = 16              # height of one flag → value row
PANEL_TOP_PAD = 5
PANEL_BOT_PAD = 7
PANEL_H_PAD = 10            # horizontal padding inside the panel
PANEL_MIN_W = 112

DRAG_PARTS = ("bar_start", "bar_stop", "bar_body", "run_body", "hold_body")


def task_signals_from_yaml(yaml_text) -> Dict[str, str]:
    """task_name -> SDR_CAL_SIGNAL_ID, parsed from a tasks.yaml document. Used to
    look up a task's calibration signal so a step form can offer absolute power."""
    import yaml as _yaml
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return {}
    try:
        doc = _yaml.safe_load(yaml_text) or {}
    except _yaml.YAMLError:
        return {}
    out: Dict[str, str] = {}
    for entry in (doc.get("tasks") or []):
        name = entry.get("name")
        sid = (entry.get("env") or {}).get("SDR_CAL_SIGNAL_ID")
        if name and sid:
            out[name] = str(sid)
    return out


def _fmt_offset(offset_s: float) -> str:
    """'-2 min', '+5 s', '0 s' — signed, split into min/h past each threshold."""
    return fmt_duration(offset_s, signed=True)


def _ramp_summary(spec, anchor: str) -> str:
    """A compact 'param start→stop · 60s' (or '· fills window') label for a ramp.
    A run-mode ramp (fires the task each point) is tagged so it's not mistaken for a
    live tune."""
    spec = spec or {}
    param = spec.get("param") or "(param)"
    tag = "run " if spec.get("mode") == "run" else ""
    a, b = spec.get("start"), spec.get("stop")
    span = f"{fmt_value(a)}→{fmt_value(b)}" if a is not None and b is not None else "…"
    if anchor == "both":
        return f"{tag}{param} {span} · fills window"
    try:
        res = _ramp.resolve_ramp(a, b, steps=spec.get("steps"), step=spec.get("step"), hold_s=spec.get("hold_s"),
                                 duration_s=spec.get("duration_s"))
        return f"{tag}{param} {span} · {fmt_duration(res.duration_s)}"
    except (ValueError, TypeError):
        return f"{tag}{param} {span}"


def _timing_text(offset_s: float, side: str, with_side: bool) -> str:
    """Readable timing label for a chip. Exactly on the anchor reads as the anchor
    name ('on-air'/'off-air'/'on-resume') rather than an ambiguous '0s'; otherwise the
    signed offset, optionally with the side it's measured from.

    A window-B step (side="hold") is timed from the Hold's resume instant: 0 or after
    reads 'on-resume', before it reads 'pre-hold' (a step dwelling into the pause)."""
    if side == "hold":
        if offset_s == 0:
            return "on-resume"
        label = "pre-hold" if offset_s < 0 else "on-resume"
        return f"{_fmt_offset(offset_s)} · {label}" if with_side else _fmt_offset(offset_s)
    label = "on-air" if side == "start" else "off-air"
    if offset_s == 0:
        return label
    return f"{_fmt_offset(offset_s)} · {label}" if with_side else _fmt_offset(offset_s)


def _ramp_end_side_off(item_anchor: str, end_anchor: str, end_off: float,
                       h_off: Optional[float]) -> Tuple[str, float]:
    """(side, offset) for a ramp END's timing chip. A Hold-anchored ramp's ends are stored
    on the START axis (h_off + its offset) for geometry (see ramp_span); its chips must read
    hold-relative ('on-resume'/'pre-hold'), so map them back to side='hold' at (end - h_off).
    Every other ramp keeps its geometry anchor/offset."""
    if item_anchor == "hold":
        return "hold", end_off - (h_off or 0.0)
    return end_anchor, end_off


def _is_flag(s: str) -> bool:
    """True if `s` is a CLI flag rather than a value. A leading '-' normally marks
    a flag, but a negative number (e.g. '-20', '-3.5', '-1e6') is a value — without
    this exception a `--power -20` pair splits into two phantom rows ('power' and
    '20')."""
    if not s.startswith("-"):
        return False
    try:
        float(s)
    except ValueError:
        return True
    return False


def _arg_pairs(args: List[str]) -> List[Tuple[str, Optional[str]]]:
    """Group a flat CLI arg list into (flag, value) rows for orderly display.
    A flag with no following value (a boolean switch) gets value None; a bare
    positional gets an empty flag."""
    pairs: List[Tuple[str, Optional[str]]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if _is_flag(a):
            if i + 1 < len(args) and not _is_flag(args[i + 1]):
                pairs.append((a, args[i + 1])); i += 2
            else:
                pairs.append((a, None)); i += 1
        else:
            pairs.append(("", a)); i += 1
    return pairs


# ── The canvas: paints bars + pills and handles all dragging / hit-testing ────

def _tool_icon(kind: str, color: str = None) -> QIcon:
    """A small line icon for a toolbar chip (Duration/One-shot/Tune/Ramp/Hold)."""
    pm = QPixmap(18, 18); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    col = QColor(color or Palette.TEXT_MUTED)
    pen = QPen(col, 1.6); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "bar":
        p.drawRoundedRect(QRectF(2, 6, 14, 6), 2.5, 2.5)
    elif kind == "run":
        p.drawEllipse(QRectF(4, 4, 10, 10))
    elif kind == "tune":
        path = QPainterPath(); path.moveTo(2, 11); path.lineTo(6, 11); path.lineTo(8, 4)
        path.lineTo(11, 15); path.lineTo(13, 9); path.lineTo(16, 9); p.drawPath(path)
    elif kind == "ramp":
        p.drawLine(3, 14, 15, 5); p.drawLine(3, 14, 15, 14)
    elif kind == "hold":
        p.setBrush(col)
        p.drawRoundedRect(QRectF(5, 4, 3, 10), 1, 1); p.drawRoundedRect(QRectF(10, 4, 3, 10), 1, 1)
    p.end()
    return QIcon(pm)


class _Legend(QWidget):
    """A compact task-colour key + the 'tunes & ramps inherit their parent' caption."""

    def __init__(self, canvas: "_TimelineCanvas"):
        super().__init__()
        self._canvas = canvas
        self.setFixedHeight(22)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(11)
        p.setFont(f); fm = QFontMetrics(f)
        x = 2
        for task, hue in (getattr(self._canvas, "_hue", {}) or {}).items():
            known = self._canvas.task_known(task)
            base = QColor(hue) if known else QColor(Palette.CRASH)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(base)
            p.drawRoundedRect(QRectF(x, self.height() / 2 - 5, 10, 10), 3, 3)
            x += 15
            p.setPen(QColor(Palette.TEXT_MUTED))
            p.drawText(x, 0, fm.horizontalAdvance(task) + 4, self.height(),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), task)
            x += fm.horizontalAdvance(task) + 16
        cap = "Tunes & ramps inherit their parent task's colour"
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(0, 0, self.width() - 2, self.height(),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), cap)


class _TimelineCanvas(QWidget):
    changed = pyqtSignal()

    def __init__(self, editor: "TimelineEditor"):
        super().__init__()
        self._editor = editor
        self._items: List = []                       # BarItem | RunItem
        self._on = float(tlm.EDGE_PAD)
        self._off = float(tlm.EDGE_PAD + tlm.MIDDLE_GAP)
        self._geom: Dict[int, dict] = {}             # uid -> paint/hit geometry
        self._drag: Optional[dict] = None
        self._collapsed: set = set()                 # uids collapsed to name-only
        self._label_font = QFont(); self._label_font.setPointSize(10); self._label_font.setBold(True)
        self._cap_font = QFont(); self._cap_font.setPointSize(8)
        self._arg_font = QFont(); self._arg_font.setPointSize(9)
        self._arg_font_b = QFont(); self._arg_font_b.setPointSize(9); self._arg_font_b.setBold(True)
        self._content_w, self._content_h = tlm.EDGE_PAD, 210
        self._c_on, self._c_off = self._on, self._off
        self._lane_of: Dict[int, int] = {}
        self._lane_y: Dict[int, int] = {}
        self._rows: List = []            # non-Hold items, one per row (display_order)
        self._holds: List = []           # Hold markers (own no row; paint as dividers)
        self._hue: Dict[str, str] = {}   # task_name -> hue hex (task_hue_map)
        self._hold_off: Optional[float] = None       # the Hold marker's on-air offset (None = none)
        self._step_bases: dict = {}                   # uid -> resolved on-air base for step anchors
        self._baseline = self._content_h - BASELINE_FROM_BOTTOM
        self._zoom = 1.0                 # horizontal (time-axis) zoom factor
        self._scroll = None              # host QScrollArea, for zoom-to-cursor
        self._selected: Optional[int] = None    # uid of the selected item (highlight + chip)
        self._connect: Optional[dict] = None    # active drag-to-anchor: {src, edge, cursor, target, moved}
        self._rmchip: Optional[QRectF] = None   # hit rect of the painted "Remove anchor" chip
        self._rmchip_pos: Optional[tuple] = None  # (cx, cy) where the chip paints, set each paint
        self._hover_uid: Optional[int] = None   # item currently under the cursor (tooltip throttle)
        self._snap_guide: Optional[float] = None  # x of the active snap guide line during a drag
        self._undo: List[list] = []             # past item snapshots (deepcopies) for Ctrl+Z
        self._redo: List[list] = []             # undone snapshots for Ctrl+Y / Ctrl+Shift+Z
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # so the canvas receives key shortcuts
        self.grabGesture(Qt.GestureType.PinchGesture)   # touchpad pinch (where routed as a gesture)
        self.relayout()

    def _eff(self) -> float:
        """Effective pixels-per-second at the current zoom."""
        return tlm.SCALE * self._zoom

    def _compute_anchors(self):
        """(on_air_x, off_air_x, content_width) for the current items. A subclass
        may override to change the timeline's extent (e.g. the plan editor shows
        only the on-air window)."""
        return tlm.compute_anchors(self._items, self._zoom)

    def set_scroll_area(self, scroll) -> None:
        self._scroll = scroll

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._content_w, self._content_h)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._content_w, self._content_h)

    # ── Item access ──────────────────────────────────────────────────────────

    def items(self) -> List:
        return list(self._items)

    def set_items(self, items: List) -> None:
        # Loading a fresh sequence is the new baseline — start undo history over.
        self._items = list(items)
        self._selected = None
        self._connect = None
        self._undo = []
        self._redo = []
        self.relayout()
        self.changed.emit()

    def add_item(self, item) -> None:
        self._record()
        self._items.append(item)
        self.relayout()
        self.changed.emit()

    def replace_item(self, uid: int, item) -> None:
        self._record()
        for i, it in enumerate(self._items):
            if it.uid == uid:
                self._items[i] = item
                break
        self.relayout()
        self.changed.emit()

    def remove_item(self, uid: int, record: bool = True) -> None:
        if record:
            self._record()
        self._items = [it for it in self._items if it.uid != uid]
        self._collapsed.discard(uid)
        if self._selected == uid:
            self._selected = None
        self.relayout()
        self.changed.emit()

    def clear(self) -> None:
        self._record()
        self._items = []
        self._collapsed.clear()
        self._selected = None
        self._connect = None
        self.relayout()
        self.changed.emit()

    # ── Undo / redo (item-list snapshots) ─────────────────────────────────────
    def _snapshot(self) -> list:
        return [copy.deepcopy(it) for it in self._items]

    def _record(self) -> None:
        """Push the CURRENT state onto the undo stack before a mutation, and drop any redo
        history (a fresh edit forks the timeline). Capped so history can't grow unbounded."""
        self._undo.append(self._snapshot())
        del self._undo[:-100]
        self._redo.clear()

    def _restore(self, snap: list) -> None:
        self._items = snap
        if self._selected is not None and all(it.uid != self._selected for it in self._items):
            self._selected = None
        self._connect = None
        self.relayout()
        self.changed.emit()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())

    def task_known(self, name: str) -> bool:
        tasks = self._editor.available_tasks()
        return (not tasks) or (name in tasks)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _run_width(self, item) -> int:
        fm = QFontMetrics(self._label_font)
        w = fm.horizontalAdvance(self._run_label(item)) + 34
        if item.args:
            w += CARET_W
        return int(max(RUN_MIN_W, min(RUN_MAX_W, w)))

    def _run_label(self, item) -> str:
        act = getattr(item, "action", "run")
        if act == "tune":
            # A tune point shows its parameter changes inline (no caret panel). A calibrated --power
            # controlled in a view (a chirp's live density) shows THAT quantity, not the raw base.
            overrides = self._editor._pill_power_display(item)
            summary = ", ".join(f"{k}={overrides.get(k, v)}"
                                for k, v in (item.params or {}).items())
            base = f"◈ {item.task_name or '(no task)'}"
            return f"{base}  {summary}".strip() if summary else base
        if act == "ramp":
            return f"⟋ {_ramp_summary(getattr(item, 'ramp', None), item.anchor)}"
        # Name only — the arguments live behind the ▾ caret / editor.
        return f"⚡ {item.task_name or '(no task)'}".strip()

    def _bar_label(self, item) -> str:
        return item.task_name or "(no task)"

    # ── Inline argument panel (shown by default; toggled by the caret) ────────

    def _expanded(self, item) -> bool:
        return False   # inline arg panels dropped in the redesign (args live in the editor)

    def _panel_width(self, item) -> int:
        fmf = QFontMetrics(self._arg_font)
        fmv = QFontMetrics(self._arg_font_b)
        w = PANEL_MIN_W
        for flag, val in _arg_pairs(item.args):
            row = fmf.horizontalAdvance(flag or "(positional)") + 18 \
                + fmv.horizontalAdvance("✓" if val is None else str(val))
            w = max(w, row + 2 * PANEL_H_PAD)
        return int(min(w, RUN_MAX_W + 60))

    def _panel_height(self, item) -> int:
        return PANEL_TOP_PAD + len(_arg_pairs(item.args)) * ARG_ROW_H + PANEL_BOT_PAD

    def _run_cx(self, item) -> float:
        """Centre x of a one-shot — to scale from its anchor (the band widens to
        keep on-air-anchored points left of off-air-anchored ones). A window-B
        (anchor="hold") item is placed to scale from the Hold's position."""
        a, o = tlm.effective_anchor_offset(item, self._hold_off, self._step_bases)
        return tlm.offset_to_x(a, o, self._on, self._off, self._zoom)

    def _item_left(self, item) -> float:
        """Left x the item's name/panel starts at (for panel anchoring/packing)."""
        if item.kind == "bar":
            sx = tlm.offset_to_x(*tlm.bar_start_placement(item, self._hold_off),
                                 self._on, self._off, self._zoom)
            px = tlm.offset_to_x("stop", item.stop_offset, self._on, self._off, self._zoom)
            return min(sx, px)
        return self._run_cx(item) - self._run_width(item) / 2

    def _foot_h(self, item) -> int:
        """Row footprint — uniform in the redesign (one capsule/pin per row)."""
        return LANE_H

    def _span(self, item) -> Tuple[float, float]:
        """Horizontal [left, right] the item occupies (for lane packing) — includes
        the inline argument panel when it's expanded."""
        if item.kind == "bar":
            sx = tlm.offset_to_x(*tlm.bar_start_placement(item, self._hold_off),
                                 self._on, self._off, self._zoom)
            px = tlm.offset_to_x("stop", item.stop_offset, self._on, self._off, self._zoom)
            left, right = sx - HANDLE_W, px + HANDLE_W
        elif tlm._is_ramp(item):
            (la, lo), (ra, ro) = tlm.ramp_span(item, self._hold_off, self._step_bases)
            sx = tlm.offset_to_x(la, lo, self._on, self._off, self._zoom)
            px = tlm.offset_to_x(ra, ro, self._on, self._off, self._zoom)
            left, right = min(sx, px) - RAMP_MIN_W / 2, max(sx, px) + RAMP_MIN_W / 2
        else:
            cx = self._run_cx(item)
            w = self._run_width(item)
            left, right = cx - w / 2, cx + w / 2
        if self._expanded(item):
            right = max(right, self._item_left(item) + self._panel_width(item))
        return left, right

    def _assign_lanes(self) -> Dict[int, int]:
        """Redesign: ONE ROW PER ITEM, grouped so a task's tunes/ramps sit directly under
        it (tlm.display_order). The Hold owns no row (it paints as a divider). Also caches
        the per-task hue map. Returns {uid: row index}."""
        self._rows, self._holds = tlm.display_order(self._items)
        self._hue = tlm.task_hue_map(self._items)
        return {it.uid: i for i, it in enumerate(self._rows)}

    def relayout(self) -> None:
        """Recompute content metrics (depend only on the items), then place.

        Each lane's height is the tallest footprint of the items in it (an expanded
        task is taller), and lanes stack with cumulative y so an expanded panel or
        an offset caption never overlaps the task below it."""
        # The Hold marker's position (window A's end) governs where window-B items sit,
        # so resolve it before geometry (compute_anchors reads it too).
        self._hold_off = tlm.hold_offset(self._items)
        self._step_bases = tlm.resolve_step_offsets(self._items, self._hold_off)
        # Un-centered content anchors + intrinsic content width. Factored into a
        # hook so a subclass (the plan timeline) can supply a window-only geometry.
        self._c_on, self._c_off, self._content_w = self._compute_anchors()
        self._on, self._off = self._c_on, self._c_off   # for shift-invariant lane packing
        self._lane_of = self._assign_lanes()
        n_lanes = (max(self._lane_of.values()) + 1) if self._lane_of else 1

        # Uniform rows (no inline arg panels in the redesign).
        self._lane_y = {}
        y = LANES_TOP
        for lane in range(n_lanes):
            self._lane_y[lane] = y
            y += LANE_H + LANE_VGAP
        stack_bottom = y - LANE_VGAP
        # The time axis rides directly under the rows (not pinned to the widget bottom).
        self._rows_bottom = stack_bottom
        self._content_h = max(200, stack_bottom + AXIS_GAP + 30)
        # The host QScrollArea is widget-resizable: minimums let the canvas STRETCH
        # to fill a bigger viewport (never shrinking below the content), and only
        # scroll when the content is larger.
        self.setMinimumWidth(self._content_w)
        self.setMinimumHeight(self._content_h)
        self.updateGeometry()
        self._place()

    def resizeEvent(self, e):  # noqa: N802
        # Re-place on every resize so the on-air band re-centres in the new width.
        self._place()
        super().resizeEvent(e)

    def _place(self) -> None:
        """Position anchors + items for the current widget size: centre the on-air
        band horizontally, keep the tasks top-anchored, and pin the time axis to
        the bottom (extra height opens a gap in the middle)."""
        avail_w = max(self.width(), self._content_w)
        if self._content_w <= avail_w:
            # Content fits: centre the band in the viewport (as before).
            mid0 = (self._c_on + self._c_off) / 2.0
            shift = avail_w / 2.0 - mid0
            shift = max(0.0, min(shift, max(0.0, avail_w - self._content_w)))
        else:
            # Content is wider than the viewport: LEFT-anchor it so on-air sits near the
            # left edge with only a small pre-roll gutter (no dead warm-up whitespace).
            eff = self._eff()
            offs = []
            for it in self._items:
                if it.kind == "bar":
                    offs.append(float(getattr(it, "start_offset", 0.0)))
                elif not tlm._is_hold(it):
                    a, o = tlm.effective_anchor_offset(it, self._hold_off, self._step_bases)
                    if a == "start":
                        offs.append(o)
            preroll = max(0.0, -min(offs)) if offs else 0.0
            target_on = tlm.EDGE_PAD + min(self._c_on - tlm.EDGE_PAD, (preroll + 16) * eff)
            shift = target_on - self._c_on          # <= 0: pull left, trimming empty warm-up
        self._on = self._c_on + shift
        self._off = self._c_off + shift
        # The axis rides directly under the last row (Gantt-style), not the widget bottom.
        self._baseline = getattr(self, "_rows_bottom", LANES_TOP) + AXIS_GAP

        self._geom = {}
        for it in self._items:
            y = self._lane_y.get(self._lane_of.get(it.uid, 0), LANES_TOP)
            g = {"kind": it.kind, "y": y}
            if tlm._is_hold(it):
                # The Hold marker is a vertical divider spanning the band, not a pill.
                g["kind"] = "hold"
                g["cx"] = self._run_cx(it)     # start-anchored at the hold offset
            elif it.kind == "bar":
                g["start_x"] = tlm.offset_to_x(*tlm.bar_start_placement(it, self._hold_off),
                                               self._on, self._off, self._zoom)
                g["stop_x"] = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off, self._zoom)
            elif tlm._is_ramp(it):
                # A ramp draws as a duration bar between its two anchored ends.
                (la, lo), (ra, ro) = tlm.ramp_span(it, self._hold_off, self._step_bases)
                g["start_x"] = tlm.offset_to_x(la, lo, self._on, self._off, self._zoom)
                g["stop_x"] = tlm.offset_to_x(ra, ro, self._on, self._off, self._zoom)
                g["ends"] = ((la, lo), (ra, ro))
            else:
                g["cx"] = self._run_cx(it)
                g["w"] = self._run_width(it)
            g["foot_h"] = LANE_H
            self._geom[it.uid] = g
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._rmchip = None            # recomputed below when a step-anchored item is selected
        self._rmchip_pos = None
        baseline = int(self._baseline)
        on_x, off_x = int(self._on), int(self._off)
        top = LANES_TOP - 8
        def_x = int(self._def_x())

        # Defined on-air region — a whisper of the on-air (green) tint.
        tint = QColor(Palette.ONLINE); tint.setAlpha(11)
        p.fillRect(QRectF(on_x, top, max(0, def_x - on_x), baseline - top), tint)
        # Undefined ("relative") region — diagonal hatch; the window length is only set at arm.
        self._paint_hatch(p, def_x, off_x, top, baseline)
        if off_x - def_x > 6:
            pen = QPen(QColor(Palette.BORDER_STRONG), 1)
            pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
            p.drawLine(def_x, top, def_x, baseline + 4)

        self._paint_anchor(p, on_x, top, baseline, "ON-AIR", Palette.ONLINE)
        self._paint_anchor(p, off_x, top, baseline, "OFF-AIR", Palette.CRASH)
        self._paint_axis(p, baseline, on_x, def_x, off_x)
        if off_x - def_x > 82:
            self._paint_rel_badge(p, (def_x + off_x) // 2, (top + baseline) // 2)

        self._paint_connectors(p)
        for it in self._rows:
            if it.kind == "bar":
                self._paint_bar(p, it)
            elif tlm._is_ramp(it):
                self._paint_ramp(p, it)
            else:
                self._paint_pin(p, it)
        for it in self._holds:
            self._paint_hold(p, it)
        self._paint_selection(p)
        if self._rmchip_pos is not None:
            self._paint_remove_chip(p, *self._rmchip_pos)
        self._paint_snap_guide(p)
        self._paint_connect_drag(p)
        self._paint_drag_readout(p)
        p.end()

    # ── Redesign paint helpers ────────────────────────────────────────────────
    @staticmethod
    def _mmss(t: float) -> str:
        sign = "−" if t < 0 else ""
        t = abs(t); m, s = int(t // 60), int(round(t % 60))
        return f"{sign}{m}:{s:02d}" if m else f"{sign}{s}s"

    def _def_x(self) -> float:
        """Rightmost on-air x that is actually pinned by an anchor/offset — the end of the
        DEFINED (real-time) region. Beyond it the band is hatched 'relative'."""
        x = float(self._on)
        for it in self._rows:
            g = self._geom.get(it.uid)
            if not g:
                continue
            if it.kind == "bar":
                x = max(x, g.get("start_x", x))            # start is on-air; stop is off-air
            elif tlm._is_ramp(it):
                if getattr(it, "anchor", "start") in ("start", "step"):
                    x = max(x, g.get("stop_x", x))         # the ramp's end
            else:
                a, _o = tlm.effective_anchor_offset(it, self._hold_off, self._step_bases)
                if a == "start":
                    x = max(x, g.get("cx", x))
        return x

    def _hues(self, hexstr: str):
        base = QColor(hexstr)
        edge = QColor(hexstr); edge.setAlpha(125)
        fa = QColor(hexstr); fa.setAlpha(42)
        fb = QColor(hexstr); fb.setAlpha(13)
        ink = base.darker(142)
        return base, edge, fa, fb, ink

    def _hue_for(self, it):
        return self._hue.get(getattr(it, "task_name", "") or "")

    def _item_colors(self, it):
        """(base, edge, fa, fb, ink) for an item: its task hue when the task is known,
        else the red 'unknown task' treatment (so a typo still reads as a problem)."""
        hexs = self._hue_for(it)
        if self.task_known(getattr(it, "task_name", "")) and hexs:
            return self._hues(hexs)
        base = QColor(Palette.CRASH); edge = QColor(Palette.CRASH); edge.setAlpha(150)
        fa = QColor(Palette.CRASH); fa.setAlpha(30); fb = QColor(Palette.CRASH); fb.setAlpha(10)
        return base, edge, fa, fb, QColor(Palette.CRASH)

    def _paint_hatch(self, p, x0, x1, top, bot):
        if x1 - x0 <= 0:
            return
        p.fillRect(QRectF(x0, top, x1 - x0, bot - top), QColor("#F4F6F9"))
        p.save()
        p.setClipRect(QRectF(x0, top, x1 - x0, bot - top))
        p.setPen(QPen(QColor("#DFE4EA"), 1))
        h = bot - top
        xx = x0 - h
        while xx < x1:
            p.drawLine(int(xx), int(bot), int(xx + h), int(top))
            xx += 7
        p.restore()

    def _paint_anchor(self, p, x, top, baseline, label, color):
        col = QColor(color)
        p.setPen(QPen(col, 2)); p.drawLine(x, top - 2, x, baseline + 4)
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPointSize(8); f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f); tw = fm.horizontalAdvance(label) + 12
        r = QRectF(x - tw / 2, top - 11, tw, 15)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(col); p.drawRoundedRect(r, 5, 5)
        p.setPen(QColor("#FFFFFF")); p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), label)

    def _paint_axis(self, p, baseline, on_x, def_x, off_x):
        eff = self._eff(); tick_s = self._tick_interval()
        f = QFont(Fonts.MONO.split(",")[0].strip('"')); f.setPointSize(8); p.setFont(f)

        def tick(x, t, major):
            col = QColor(Palette.TEXT_FAINT if major else Palette.BORDER_STRONG)
            p.setPen(QPen(col, 1))
            p.drawLine(int(x), baseline, int(x), baseline + (7 if major else 4))
            if major:
                p.setPen(QColor(Palette.TEXT_MUTED))
                p.drawText(int(x) - 30, baseline + 9, 60, 12,
                           int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                           "0" if t == 0 else self._mmss(t))

        # Real-time ticks across the defined region.
        t = 0
        while on_x + t * eff <= def_x + 1:
            tick(on_x + t * eff, t, True)
            if tick_s >= 2:
                mid = on_x + (t + tick_s / 2) * eff
                if mid <= def_x + 1:
                    tick(mid, t, False)
            t += tick_s
        # Warm-up (negative) ticks to the visible left edge.
        t = tick_s
        while on_x - t * eff >= tlm.EDGE_PAD:
            tick(on_x - t * eff, -t, True)
            t += tick_s
        # The off-air instant's absolute time is unknown (chosen at arm).
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(int(off_x) - 30, baseline + 9, 60, 12,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), "arm")

    def _paint_rel_badge(self, p, cx, cy):
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPointSize(8); f.setBold(True)
        p.setFont(f)
        text = "relative — length set at arm"
        fm = QFontMetrics(f); tw = fm.horizontalAdvance(text) + 22
        r = QRectF(cx - tw / 2, cy - 9, tw, 18)
        pen = QPen(QColor(Palette.BORDER_STRONG), 1); pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen); p.setBrush(QColor(Palette.SURFACE)); p.drawRoundedRect(r, 9, 9)
        p.setPen(QColor(Palette.TEXT_MUTED))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "◇  " + text)

    def _tick_interval(self) -> int:
        """Seconds between ticks — the smallest 'nice' value whose on-screen
        spacing stays ≥ MIN_TICK_PX at the current zoom, so labels never crowd."""
        eff = self._eff()
        for t in TICK_CHOICES:
            if t * eff >= MIN_TICK_PX:
                return t
        return TICK_CHOICES[-1]

    def _paint_ticks(self, p, baseline, anchor_x, negative):
        tick_s = self._tick_interval()
        step_px = tick_s * self._eff()
        i = 1
        while True:
            x = anchor_x - i * step_px if negative else anchor_x + i * step_px
            if negative and x < tlm.EDGE_PAD // 2:
                break
            if not negative and x > self.width() - tlm.EDGE_PAD // 2:
                break
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(int(x), baseline - 4, int(x), baseline + 4)
            p.setPen(QColor(Palette.TEXT_FAINT))
            label = fmt_duration(-(tick_s * i) if negative else tick_s * i,
                                 signed=True, compact=True)
            p.drawText(int(x) - 27, baseline + 6, 54, 12,
                       int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)
            i += 1

    def _paint_caret(self, p, it, g, color):
        """A small rounded chip that expands (▾) or collapses (▴) the arg panel."""
        cx, y = self._caret_center(it, g), g["y"]
        r = QRectF(cx - CARET_W / 2, y + 6, CARET_W, LANE_H - 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(color)))
        p.drawRoundedRect(r, 4, 4)
        f = QFont(); f.setPointSize(9); f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(Palette.SURFACE))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "▴" if self._expanded(it) else "▾")

    def _paint_timing(self, p, cx, top_y, text):
        """A small opaque pill holding a task's timing, so it stays legible even
        sitting on top of an anchor line or the shaded on-air band."""
        p.setFont(self._cap_font)
        fm = QFontMetrics(self._cap_font)
        w = fm.horizontalAdvance(text) + 12
        r = QRectF(cx - w / 2, top_y, w, CAPTION_H - 1)
        p.setPen(QPen(QColor(Palette.BORDER), 1))
        p.setBrush(QBrush(QColor(Palette.SURFACE)))
        p.drawRoundedRect(r, (CAPTION_H - 1) / 2, (CAPTION_H - 1) / 2)
        p.setPen(QColor(Palette.TEXT_MUTED))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)

    def _paint_panel(self, p, it, g, border):
        """The inline flag → value argument panel drawn under a task."""
        if "panel" not in g:
            return
        px, py, pw, ph = g["panel"]
        rect = QRectF(px, py, pw, ph)
        p.setPen(QPen(QColor(Palette.BORDER), 1))
        p.setBrush(QBrush(QColor(Palette.SURFACE_ALT)))
        p.drawRoundedRect(rect, 6, 6)
        # a slim accent stripe in the item's colour
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(border)))
        p.drawRoundedRect(QRectF(px, py + 3, 3, ph - 6), 1.5, 1.5)

        fmv = QFontMetrics(self._arg_font_b)
        row_y = py + PANEL_TOP_PAD
        for flag, val in _arg_pairs(it.args):
            p.setFont(self._arg_font)
            p.setPen(QColor(Palette.TEXT_MUTED))
            p.drawText(int(px + PANEL_H_PAD), int(row_y), int(pw - 2 * PANEL_H_PAD), ARG_ROW_H,
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       flag or "(positional)")
            p.setFont(self._arg_font_b)
            p.setPen(QColor(Palette.TEXT))
            vtext = "✓" if val is None else str(val)
            vw = fmv.horizontalAdvance(vtext)
            p.drawText(int(px + pw - PANEL_H_PAD - vw), int(row_y), vw, ARG_ROW_H,
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), vtext)
            row_y += ARG_ROW_H

    def _f(self, px: int, bold: bool = False) -> QFont:
        f = QFont(Fonts.SANS.split(",")[0].strip('"'))
        f.setPixelSize(px)
        f.setWeight(QFont.Weight(600 if bold else 500))
        return f

    def _capsule(self, p, rect, base, edge, fa, fb, rail=True):
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, fa); grad.setColorAt(1.0, fb)
        p.setPen(QPen(edge, 1)); p.setBrush(QBrush(grad))
        p.drawRoundedRect(rect, BAR_R, BAR_R)
        if rail:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(base)
            p.drawRoundedRect(QRectF(rect.left() + 3, rect.top() + 6, HUE_RAIL, rect.height() - 12),
                              1.5, 1.5)

    def _paint_edge_dot(self, p, x, cy, base, linked=False):
        r = QRectF(x - 5, cy - 5, 10, 10)
        if linked:
            p.setPen(QPen(QColor("#FFFFFF"), 2)); p.setBrush(base)
        else:
            p.setPen(QPen(base, 2)); p.setBrush(QColor(Palette.SURFACE))
        p.drawEllipse(r)

    def _edge_linked(self, it, edge: str) -> bool:
        sid = getattr(it, "step_id", "") or ""
        if not sid:
            return False
        return any(getattr(o, "anchor", "") == "step"
                   and (getattr(o, "anchor_step_id", "") or "") == sid
                   and (getattr(o, "anchor_edge", "end") or "end") == edge
                   for o in self._rows)

    def _chip(self, p, cx, cy, text, border, ink, mono=True):
        f = mono_font(10) if mono else self._f(10, True)
        p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 14
        r = QRectF(cx - w / 2, cy - 9, w, 18)
        p.setPen(QPen(border, 1)); p.setBrush(QColor(Palette.SURFACE))
        p.drawRoundedRect(r, 9, 9)
        p.setPen(ink); p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)

    def _paint_bar(self, p, it):
        g = self._geom[it.uid]
        y, sx, px = g["y"], g["start_x"], g["stop_x"]
        base, edge, fa, fb, ink = self._item_colors(it)
        left = min(sx, px); w = max(HANDLE_W * 2.0, abs(px - sx))
        rect = QRectF(left, y, w, LANE_H)
        self._capsule(p, rect, base, edge, fa, fb)
        p.setFont(self._f(12, True)); p.setPen(ink)
        fm = QFontMetrics(self._f(12, True))
        label = fm.elidedText(it.task_name or "(no task)", Qt.TextElideMode.ElideRight,
                              max(10, int(w) - 26))
        p.drawText(QRectF(left + 12, y, w - 22, LANE_H),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        cy = y + LANE_H / 2
        self._paint_edge_dot(p, sx, cy, base, self._edge_linked(it, "start"))
        self._paint_edge_dot(p, px, cy, base, self._edge_linked(it, "end"))

    def _paint_ramp(self, p, it):
        """A ramp draws as a capsule between its two anchored ends. Text (parent-task
        badge · from→to range · duration) sits flush-left; a dedicated right END-CAP
        holds the rising/falling slope mark, so the direction cue and the text can
        never overlap (docs/ramp-pill-mockup.html · option B)."""
        g = self._geom[it.uid]
        y, sx, px = g["y"], g["start_x"], g["stop_x"]
        base, edge, fa, fb, ink = self._item_colors(it)
        left = min(sx, px); w = max(RAMP_MIN_W, abs(px - sx))
        rect = QRectF(left, y, w, LANE_H)
        self._capsule(p, rect, base, edge, fa, fb)
        r = dict(getattr(it, "ramp", None) or {})
        a, b = r.get("start"), r.get("stop")
        rising = (a is not None and b is not None and b >= a)

        # ── right end-cap: a faint tinted zone (divider + slope mark) ─────────────
        cap_w = RAMP_CAP_W if w > RAMP_CAP_W + 22 else 0.0
        if cap_w:
            clip = QPainterPath(); clip.addRoundedRect(rect, BAR_R, BAR_R)
            p.save(); p.setClipPath(clip)
            cap = QRectF(rect.right() - cap_w, rect.top(), cap_w, rect.height())
            fill = QColor(base); fill.setAlpha(26)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(fill); p.drawRect(cap)
            div = QColor(base); div.setAlpha(75)
            p.setPen(QPen(div, 1))
            p.drawLine(QPointF(cap.left(), rect.top() + 1.0),
                       QPointF(cap.left(), rect.bottom() - 1.0))
            p.restore()
            self._paint_slope(p, cap.center().x(), rect.center().y(), rising, base)
        else:
            # too narrow for a cap — a compact slope glyph tucked at the right edge
            self._paint_slope(p, rect.right() - 12, rect.center().y(), rising, base, span=7.0)

        # ── flush-left text: badge · range · duration (right of the badge) ────────
        text_r = rect.right() - (cap_w or 6.0) - 8.0
        bx = left + 10
        if w > 66:
            badge = it.task_name or ""
            f = self._f(9, True); p.setFont(f); fm = QFontMetrics(f)
            bw = min(fm.horizontalAdvance(badge) + 12, max(20.0, text_r - bx))
            br = QRectF(bx, y + (LANE_H - 15) / 2, bw, 15)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(fa); p.drawRoundedRect(br, 4, 4)
            p.setPen(ink)
            p.drawText(br, int(Qt.AlignmentFlag.AlignCenter),
                       fm.elidedText(badge, Qt.TextElideMode.ElideRight, int(bw) - 8))
            bx += bw + 8
        # duration, right-aligned just left of the cap divider
        try:
            dur = tlm._ramp_duration(r)
        except Exception:  # noqa: BLE001
            dur = 0.0
        dw = 0.0
        if dur and text_r - bx > 88:
            f = mono_font(10); p.setFont(f); fm = QFontMetrics(f)
            dt = self._mmss(dur); dw = fm.horizontalAdvance(dt)
            p.setPen(ink)
            p.drawText(QRectF(text_r - dw, y, dw, LANE_H),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), dt)
            dw += 10.0
        # from→to range, between the badge and the duration, when there's room
        if a is not None and b is not None:
            f = mono_font(10); p.setFont(f); fm = QFontMetrics(f)
            rng = f"{fmt_value(a)} → {fmt_value(b)}"
            avail = int(text_r - dw - bx)
            if avail > 30:
                p.setPen(ink)
                p.drawText(QRectF(bx, y, avail, LANE_H),
                           int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           fm.elidedText(rng, Qt.TextElideMode.ElideRight, avail))

        cy = y + LANE_H / 2
        self._paint_edge_dot(p, sx, cy, base, self._edge_linked(it, "start"))
        self._paint_edge_dot(p, px, cy, base, self._edge_linked(it, "end"))

    def _paint_slope(self, p, mx, my, rising, base, span=9.0):
        """The rising/falling trend mark: a left→right stroke ending in a filled dot
        at the destination level (up = ends high-right, down = ends low-right)."""
        h = 6.0
        x0, x1 = mx - span, mx + span
        y_left = my + (h if rising else -h)
        y_right = my - (h if rising else -h)
        p.setPen(QPen(base, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(x0, y_left), QPointF(x1, y_right))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(base)
        p.drawEllipse(QPointF(x1, y_right), 2.4, 2.4)

    def _paint_pin(self, p, it):
        """A tune / one-shot is an INSTANT: a filled pin at the exact time + a borderless
        caption (never a capsule, so it never reads as having a duration)."""
        g = self._geom[it.uid]
        y, cx = g["y"], g["cx"]
        base, edge, fa, fb, ink = self._item_colors(it)
        cy = y + LANE_H / 2
        one_shot = getattr(it, "action", "run") == "run"
        p.setPen(QPen(QColor("#FFFFFF"), 2.4)); p.setBrush(base)
        if one_shot:
            path = QPainterPath()
            path.moveTo(cx, cy - 7); path.lineTo(cx + 7, cy)
            path.lineTo(cx, cy + 7); path.lineTo(cx - 7, cy); path.closeSubpath()
            p.drawPath(path)
        else:
            p.drawEllipse(QRectF(cx - 6.5, cy - 6.5, 13, 13))
        # caption: parent badge (tune) + text, borderless
        tx = cx + 13
        if not one_shot:
            badge = it.task_name or ""
            f = self._f(9, True); p.setFont(f); fm = QFontMetrics(f)
            bw = fm.horizontalAdvance(badge) + 12
            br = QRectF(tx, cy - 7.5, bw, 15)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(fa); p.drawRoundedRect(br, 4, 4)
            p.setPen(ink); p.drawText(br, int(Qt.AlignmentFlag.AlignCenter), badge)
            tx += bw + 6
            overrides = self._editor._pill_power_display(it)
            text = ", ".join(f"{k}={overrides.get(k, v)}" for k, v in (it.params or {}).items())
        else:
            text = it.task_name or "(no task)"
        p.setFont(self._f(12, True)); p.setPen(QColor(Palette.TEXT))
        p.drawText(QRectF(tx, y, 260, LANE_H),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

    # ── Connectors (step-to-step anchors, rendered under the bars) ────────────
    def _edge_x(self, tgt, edge: str) -> float:
        g = self._geom.get(tgt.uid) or {}
        if tgt.kind == "bar" or tlm._is_ramp(tgt):
            return g.get("stop_x", g.get("start_x", 0.0)) if edge == "end" \
                else g.get("start_x", 0.0)
        return g.get("cx", 0.0)

    def _paint_connectors(self, p):
        by_sid = {getattr(it, "step_id", "") or "": it for it in self._rows
                  if getattr(it, "step_id", "")}
        for it in self._rows:
            if getattr(it, "anchor", "") != "step":
                continue
            tgt = by_sid.get(getattr(it, "anchor_step_id", "") or "")
            gi = self._geom.get(it.uid)
            if tgt is None or gi is None or tgt.uid not in self._geom:
                continue
            edge = getattr(it, "anchor_edge", "end") or "end"
            x1 = self._edge_x(tgt, edge)
            y1 = self._geom[tgt.uid]["y"] + LANE_H / 2
            x2 = gi.get("start_x", gi.get("cx"))
            y2 = gi["y"] + LANE_H / 2
            # Exit the anchor AWAY from its body along the time axis: an END edge (body to the
            # left) exits right, a START edge (body to the right) exits left, a point exits right.
            exit_dir = -1.0 if ((tlm._is_ramp(tgt) or getattr(tgt, "kind", "") == "bar")
                                and edge == "start") else 1.0
            obstacles = self._intervening_obstacles(tgt.uid, it.uid)
            base, _e, _fa, _fb, ink = self._item_colors(it)
            sel = (it.uid == self._selected)
            self._draw_connector(p, x1, y1, x2, y2, base, ink,
                                 float(getattr(it, "offset", 0.0)), exit_dir, obstacles, sel)

    def _intervening_obstacles(self, anchor_uid, dep_uid):
        """x-intervals [(lo,hi)] of the steps whose ROW sits strictly between the anchor's and
        the dependent's — the third-party steps a connector must route AROUND (not through)."""
        a = self._lane_of.get(anchor_uid); d = self._lane_of.get(dep_uid)
        if a is None or d is None:
            return []
        lo_row, hi_row = sorted((a, d))
        out = []
        for it in self._rows:
            r = self._lane_of.get(it.uid)
            if r is None or not (lo_row < r < hi_row):
                continue
            g = self._geom.get(it.uid)
            if not g:
                continue
            if "start_x" in g:
                lo, hi = sorted((g["start_x"], g["stop_x"]))
            else:
                cx = g.get("cx", 0.0); lo, hi = cx - 8.0, cx + 8.0
            out.append((lo, hi))
        return out

    def _ortho_path(self, pts, r: float = 6.0) -> QPainterPath:
        """A rounded orthogonal path through axis-aligned waypoints (each segment is purely
        horizontal or vertical)."""
        path = QPainterPath(); path.moveTo(*pts[0])
        for i in range(1, len(pts) - 1):
            x0, y0 = pts[i - 1]; xc, yc = pts[i]; x1, y1 = pts[i + 1]
            din = abs(xc - x0) + abs(yc - y0)        # one component is 0 → == segment length
            dout = abs(x1 - xc) + abs(y1 - yc)
            ri = min(r, din / 2.0, dout / 2.0)
            if ri < 0.5 or din == 0 or dout == 0:
                path.lineTo(xc, yc); continue
            ix, iy = (xc - x0) / din, (yc - y0) / din
            ox, oy = (x1 - xc) / dout, (y1 - yc) / dout
            path.lineTo(xc - ix * ri, yc - iy * ri)
            path.quadTo(xc, yc, xc + ox * ri, yc + oy * ri)
        path.lineTo(*pts[-1])
        return path

    def _connector_points(self, x1, y1, x2, y2, exit_dir, obstacles, chip_run):
        """Waypoints for a connector that ENTERS the dependent HORIZONTALLY from the left, with
        the drop column chosen LEFT of the dependent (room for the inline chip) and clear of any
        intervening third-party step. When that column falls left of the anchor edge, WRAP via a
        channel just outside the dependent's row so the entry stays horizontal."""
        STUB, GAP = 16.0, 14.0
        sgn = 1.0 if y2 >= y1 else -1.0
        xd = x2 - chip_run                          # leave room for the chip inline on the entry run
        for _ in range(len(obstacles) + 2):         # push left of any obstacle the drop lands in
            hit = next(((lo, hi) for (lo, hi) in obstacles if lo - 6.0 <= xd <= hi + 6.0), None)
            if hit is None:
                break
            xd = hit[0] - GAP
        xd = min(xd, x2 - 20.0)
        if xd >= x1 - 1.0:
            if xd <= x1 + 1.0:                      # dependent ~at the anchor x: drop straight, run in
                return [(x1, y1), (x1, y2), (x2, y2)]
            return [(x1, y1), (xd, y1), (xd, y2), (x2, y2)]
        # xd is LEFT of the anchor edge → wrap: exit the stub, run a channel to xd, drop, run in
        xe = x1 + STUB * exit_dir
        ch = y2 - (LANE_H / 2.0 + LANE_VGAP / 2.0) * sgn
        return [(x1, y1), (xe, y1), (xe, ch), (xd, ch), (xd, y2), (x2, y2)]

    def _draw_connector(self, p, x1, y1, x2, y2, base, ink, offset, exit_dir=1.0,
                        obstacles=(), selected=False):
        stroke = QColor(Palette.ACCENT) if selected else base
        text = "+" + self._mmss(offset)
        chip_w = QFontMetrics(mono_font(10)).horizontalAdvance(text) + 14.0
        pts = self._connector_points(x1, y1, x2, y2, exit_dir, list(obstacles), chip_w + 24.0)
        path = self._ortho_path(pts, 6.0)
        if selected:                                   # soft under-glow when selected
            halo = QColor(Palette.ACCENT); halo.setAlpha(55)
            gpen = QPen(halo, 7); gpen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            gpen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(gpen); p.setBrush(Qt.BrushStyle.NoBrush); p.drawPath(path)
        pen = QPen(stroke, 2.4 if selected else 2); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush); p.drawPath(path)
        # arrowhead — the entry is always horizontal from the left into the dependent's start
        p.drawLine(int(x2 - 6), int(y2 - 4), int(x2), int(y2))
        p.drawLine(int(x2 - 6), int(y2 + 4), int(x2), int(y2))
        # offset chip INLINE on the entry run (the line runs through it), just left of the step
        chip_cx = x2 - chip_w / 2.0 - 10.0
        self._chip(p, chip_cx, y2, text, stroke, stroke if selected else ink)
        if selected:                                   # "Remove anchor" sits below the chip
            self._rmchip_pos = (chip_cx, y2 + 15.0)

    # ── Selection / drag affordances (drawn on top of the items) ──────────────
    def _paint_selection(self, p):
        """An accent ring around the selected item (bar / ramp capsule or a pin dot)."""
        if self._selected is None:
            return
        it = next((o for o in self._rows if o.uid == self._selected), None)
        g = self._geom.get(self._selected) if it is not None else None
        if not g:
            return
        accent = QColor(Palette.ACCENT)
        halo = QColor(Palette.ACCENT); halo.setAlpha(45)
        y = g["y"]
        p.setBrush(Qt.BrushStyle.NoBrush)
        if it.kind == "bar" or tlm._is_ramp(it):
            sx, px = g.get("start_x", 0.0), g.get("stop_x", 0.0)
            wmin = HANDLE_W * 2.0 if it.kind == "bar" else RAMP_MIN_W
            rect = QRectF(min(sx, px) - 2.5, y - 2.5, max(wmin, abs(px - sx)) + 5, LANE_H + 5)
            p.setPen(QPen(halo, 6)); p.drawRoundedRect(rect, BAR_R + 2, BAR_R + 2)
            p.setPen(QPen(accent, 2)); p.drawRoundedRect(rect, BAR_R + 2, BAR_R + 2)
        else:
            cx, cy = g.get("cx", 0.0), y + LANE_H / 2
            p.setPen(QPen(halo, 6)); p.drawEllipse(QPointF(cx, cy), 11.0, 11.0)
            p.setPen(QPen(accent, 2)); p.drawEllipse(QPointF(cx, cy), 11.0, 11.0)

    def _paint_remove_chip(self, p, cx, cy):
        """The clickable '✕ Remove anchor' pill on a selected connector. Records its hit
        rect in self._rmchip so a press detaches the anchor (no dialog)."""
        text = "Remove anchor"
        f = self._f(9, True); p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 32
        r = QRectF(cx - w / 2, cy - 10, w, 20)
        r.moveLeft(max(2.0, min(r.left(), self.width() - w - 2)))
        col = QColor(Palette.CRASH)
        p.setPen(QPen(col, 1)); p.setBrush(QColor(Palette.SURFACE))
        p.drawRoundedRect(r, 10, 10)
        gx, gy = r.left() + 13, r.center().y()
        p.setPen(QPen(col, 1.6))
        p.drawLine(QPointF(gx - 3, gy - 3), QPointF(gx + 3, gy + 3))
        p.drawLine(QPointF(gx - 3, gy + 3), QPointF(gx + 3, gy - 3))
        p.setPen(col)
        p.drawText(QRectF(r.left() + 20, r.top(), r.width() - 22, r.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        self._rmchip = r

    def _paint_connect_drag(self, p):
        """The live rubber-band while dragging an anchor from a source handle to a target
        edge, plus a readout of the resulting offset."""
        if self._connect is None or not self._connect.get("moved"):
            return
        src = next((o for o in self._rows if o.uid == self._connect["src"]), None)
        g = self._geom.get(self._connect["src"]) if src is not None else None
        if not g:
            return
        x1 = g.get("start_x", g.get("cx", 0.0))
        y1 = g["y"] + LANE_H / 2
        cur = self._connect["cursor"]; x2, y2 = cur.x(), cur.y()
        tgt = self._connect["target"]
        ok = tgt is not None
        col = QColor(Palette.ACCENT) if ok else QColor(Palette.TEXT_FAINT)
        if ok:
            t, edge = tgt
            x2 = self._edge_x(t, edge); y2 = self._geom[t.uid]["y"] + LANE_H / 2
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
            off = tlm.step_drop_offset(self._items, self._connect["src"], t.uid, edge,
                                       self._hold_off, self._step_bases)
            label = f"+{self._mmss(off or 0.0)} after {t.task_name or '?'} · {edge}"
        else:
            label = "drop on a step edge to anchor"
        self._paint_tag(p, x2, y2 - 16, label, ok)

    def _paint_snap_guide(self, p):
        """A thin dashed accent line at the x a dragged edge is snapping to."""
        if self._snap_guide is None:
            return
        x = float(self._snap_guide)
        pen = QPen(QColor(Palette.ACCENT), 1.4)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(int(x), int(LANES_TOP - 10), int(x), int(self._baseline + 4))

    def _paint_drag_readout(self, p):
        """While moving an item, a floating tag shows the time it now fires at."""
        if self._drag is None or not self._drag.get("moved"):
            return
        it, part = self._drag["item"], self._drag["part"]
        g = self._geom.get(it.uid)
        if not g:
            return
        y = g["y"]
        if part == "bar_stop":
            label = _timing_text(float(getattr(it, "stop_offset", 0.0)), "stop", True)
            x = g.get("stop_x", 0.0)
        elif part in ("bar_start", "bar_body"):
            side = "hold" if getattr(it, "start_anchor", "start") == "hold" else "start"
            label = _timing_text(float(getattr(it, "start_offset", 0.0)), side, True)
            x = g.get("start_x", 0.0)
        elif part == "hold_body":
            label = "Hold · " + _timing_text(float(getattr(it, "offset", 0.0)), "start", True)
            x = g.get("cx", 0.0)
        else:                                   # run_body (a tune / one-shot pin)
            anc = getattr(it, "anchor", "start")
            side = anc if anc in ("start", "stop", "hold") else "start"
            label = _timing_text(float(getattr(it, "offset", 0.0)), side, True)
            x = g.get("cx", 0.0)
        self._paint_tag(p, x, y - 13, label, True)

    def _paint_tag(self, p, cx, cy, text, strong=True):
        """A compact floating tag (accent when strong, muted otherwise), clamped on-canvas."""
        f = mono_font(10); p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 16
        r = QRectF(cx - w / 2, cy - 10, w, 20)
        r.moveLeft(max(2.0, min(r.left(), max(2.0, self.width() - w - 2))))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(Palette.ACCENT) if strong else QColor(Palette.TEXT_MUTED))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)

    def row_layout(self):
        """[{item, y, hue, child, row_h}] for the row-header column, in row order."""
        out = []
        for i, it in enumerate(self._rows):
            out.append({
                "item": it, "y": self._lane_y.get(i, LANES_TOP),
                "hue": self._hue_for(it),
                "child": getattr(it, "action", "run") in ("tune", "ramp"),
                "row_h": LANE_H,
            })
        return out

    def _paint_hold(self, p, it):
        """The Hold marker — a dashed vertical divider across the on-air band at the
        hold position, with a ⏸ HOLD tab at the top and its offset chip below. It is
        the third anchor: window-A steps sit to its left, window-B (anchor="hold")
        steps flow on from it to the right."""
        g = self._geom[it.uid]
        cx = int(g["cx"])
        top = LANES_TOP - 12
        baseline = int(self._baseline)
        color = QColor(Palette.ARMED)
        pen = QPen(color, 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(cx, top, cx, baseline + 6)
        # A small filled tab so the divider reads as an anchor (like ON-AIR/OFF-AIR).
        f = QFont(); f.setPointSize(8); f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        text = "⏸ HOLD"
        tw = fm.horizontalAdvance(text) + 12
        r = QRectF(cx - tw / 2, top - 3, tw, 15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawRoundedRect(r, 7, 7)
        p.setPen(QColor(Palette.SURFACE))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
        # Offset chip below the band (its position is start-anchored, from on-air).
        self._paint_timing(p, cx, baseline + 8,
                           _timing_text(getattr(it, "offset", 0.0), "start", with_side=False))

    # ── Hit-testing ───────────────────────────────────────────────────────────

    def _caret_center(self, it, g) -> float:
        """X of the ▾ 'show arguments' caret for an item (only meaningful if it
        has args)."""
        if it.kind == "bar":
            right = max(g["start_x"], g["stop_x"])
            return right - HANDLE_W - CARET_W / 2 - 2
        return g["cx"] + g["w"] / 2 - CARET_W / 2 - 6

    def _hit(self, x: float, y: float) -> Optional[Tuple[object, str]]:
        # The Hold divider spans the whole band, so a wide bar body overlaps its x —
        # test holds first so a click on the divider grabs it, not the bar underneath.
        for it in self._items:
            if not tlm._is_hold(it):
                continue
            g = self._geom.get(it.uid)
            if g and abs(x - g["cx"]) <= HOLD_HIT and (LANES_TOP - 14) <= y <= self._baseline + 20:
                return it, "hold_body"
        # Connection handles (ramp edge dots / a pin's dot) win over the body so a press on
        # a handle starts a drag-to-anchor, not a move/edit.
        edge = self._edge_at(x, y)
        if edge is not None:
            it, side = edge
            return it, "edge_" + side
        for it in self._items:
            g = self._geom.get(it.uid)
            if not g or g.get("kind") == "hold":
                continue
            top = g["y"]
            # Name row: handles / body / caret (draggable + caret toggle).
            if top - 2 <= y <= top + LANE_H + 2:
                if it.args and abs(x - self._caret_center(it, g)) <= CARET_W / 2:
                    return it, "caret"
                if g["kind"] == "bar":
                    if abs(x - g["start_x"]) <= HANDLE_HIT:
                        return it, "bar_start"
                    if abs(x - g["stop_x"]) <= HANDLE_HIT:
                        return it, "bar_stop"
                    if min(g["start_x"], g["stop_x"]) <= x <= max(g["start_x"], g["stop_x"]):
                        return it, "bar_body"
                elif "start_x" in g:   # a ramp bar — click anywhere to edit (no drag)
                    lo, hi = sorted((g["start_x"], g["stop_x"]))
                    if lo - RAMP_MIN_W / 2 <= x <= hi + RAMP_MIN_W / 2:
                        return it, "ramp_body"
                elif abs(x - g["cx"]) <= g["w"] / 2:
                    return it, "run_body"
            # Caption + inline-panel rows below: a click there opens the editor.
            if top + LANE_H < y <= top + g.get("foot_h", LANE_H) + 2:
                if "panel" in g:
                    pxx, pyy, pw, ph = g["panel"]
                    if pxx <= x <= pxx + pw and pyy - 2 <= y <= pyy + ph + 2:
                        return it, "open"
        return None

    # ── Drag-to-anchor (connection handles) ───────────────────────────────────
    def _edge_at(self, x: float, y: float) -> Optional[Tuple[object, str]]:
        """The (item, edge) of a connection handle under (x, y): a ramp's start/end edge
        dot, or a pin's dot. Bars and the Hold are not Phase-1 anchor participants, so they
        carry no connect handle. Returns None away from every dot."""
        for it in self._rows:
            g = self._geom.get(it.uid)
            if not g:
                continue
            cy = g["y"] + LANE_H / 2
            if abs(y - cy) > PIN_HIT + 3:
                continue
            if tlm._is_ramp(it):
                if abs(x - g.get("start_x", -1e9)) <= PIN_HIT:
                    return it, "start"
                if abs(x - g.get("stop_x", -1e9)) <= PIN_HIT:
                    return it, "end"
            elif it.kind != "bar" and not tlm._is_hold(it):
                if abs(x - g.get("cx", -1e9)) <= PIN_HIT:
                    return it, "start"
        return None

    def _is_anchor_source(self, it) -> bool:
        """An item that can be a step-anchor DEPENDENT (dragged onto a target): a point
        (tune / one-shot) or a ramp. Bars and the Hold cannot (Phase 1)."""
        return (not tlm._is_hold(it) and getattr(it, "kind", None) != "bar"
                and getattr(it, "action", "run") in ("run", "tune", "ramp"))

    def _drop_target(self, x: float, y: float, src_uid: int) -> Optional[Tuple[object, str]]:
        """The (target, edge) a connect drag from `src_uid` would land on at (x, y): an
        edge handle of an ELIGIBLE target (cycle-safe, on-air), never the source itself."""
        hit = self._edge_at(x, y)
        if hit is None:
            return None
        tgt, edge = hit
        if getattr(tgt, "uid", None) == src_uid:
            return None
        if tgt not in tlm.eligible_step_targets(self._items, src_uid):
            return None
        return tgt, edge

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        # "Remove anchor" chip on the selected connector wins over everything under it.
        if self._rmchip is not None and self._rmchip.contains(pos):
            self._detach_anchor(self._selected)
            return
        hit = self._hit(pos.x(), pos.y())
        if hit is None:
            self._drag = None
            if self._selected is not None:      # click on empty canvas → deselect
                self._selected = None
                self.update()
            return
        it, part = hit
        if part == "caret":
            self._drag = None
            self._toggle_collapsed(it)
            return
        # Selecting on press highlights the item and reveals its "Remove anchor" chip.
        if self._selected != it.uid:
            self._selected = it.uid
            self.update()
        # A press on a connection handle of an anchorable item starts a drag-to-anchor.
        if part.startswith("edge_") and part == "edge_start" and self._is_anchor_source(it):
            self._connect = {"src": it.uid, "cursor": pos, "target": None,
                             "moved": False, "press_x": pos.x()}
            self._drag = None
            return
        self._connect = None
        self.setFocus()                      # so Ctrl+Z / Delete reach the canvas
        self._drag = {
            "item": it, "part": part, "press_x": pos.x(), "moved": False,
            "start0": getattr(it, "start_offset", 0.0),
            "stop0": getattr(it, "stop_offset", 0.0),
            "undo0": self._snapshot(),       # pre-drag state, pushed only if the drag commits
        }

    def _toggle_collapsed(self, it) -> None:
        if it.uid in self._collapsed:
            self._collapsed.discard(it.uid)
        else:
            self._collapsed.add(it.uid)
        self.relayout()   # heights change → re-pack lanes

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        # A live drag-to-anchor: rubber-band from the source handle to the cursor, snapping
        # onto an eligible target edge under the pointer.
        if self._connect is not None:
            if not (e.buttons() & Qt.MouseButton.LeftButton):
                return
            if not self._connect["moved"] and abs(pos.x() - self._connect["press_x"]) < DRAG_THRESHOLD:
                return
            self._connect["moved"] = True
            self._connect["cursor"] = pos
            self._connect["target"] = self._drop_target(pos.x(), pos.y(), self._connect["src"])
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        if self._drag is None:
            self._snap_guide = None
            hit = self._hit(pos.x(), pos.y())
            if hit and hit[1].startswith("edge_"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif hit and hit[1] in ("bar_start", "bar_stop"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hit:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self._update_tooltip(hit[0] if hit else None, e.globalPosition().toPoint())
            return
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag["part"] not in DRAG_PARTS:
            return   # e.g. a click in the panel/caption region — never a drag
        if not self._drag["moved"] and abs(pos.x() - self._drag["press_x"]) < DRAG_THRESHOLD:
            return
        self._drag["moved"] = True
        it, part = self._drag["item"], self._drag["part"]
        x = pos.x()
        eff = self._eff()
        mid = tlm.midpoint(self._on, self._off)
        # Snap the dragged edge to a nearby step edge / anchor / tick (exact when snapped, else
        # the 1 s grid). `sx` is the snap target x (None when nothing is near).
        sx = self._snap_cursor(x, it.uid)
        self._snap_guide = None
        if part in ("run_body", "hold_body"):
            # A one-shot (or the Hold marker) keeps its anchor (changed only in the
            # editor); dragging only moves the offset, measured to scale from that fixed
            # anchor. A window-B (anchor="hold") one-shot is placed from the Hold's position.
            anchor_x = self._anchor_base_x(it)
            if sx is not None:
                off = (sx - anchor_x) / eff; self._snap_guide = sx
            else:
                off = tlm._snap((x - anchor_x) / eff)
            it.offset = self._clamp_tune_offset(it, off)
            self._live_relayout(it)
            return
        if part == "bar_start":
            if getattr(it, "start_anchor", "start") == "hold" and self._hold_off is not None:
                # A window-B bar's start is measured from the Hold divider (resume), never before it.
                hold_x = self._on + self._hold_off * eff
                if sx is not None and sx >= hold_x - 0.5:
                    it.start_offset = max(0.0, (sx - hold_x) / eff); self._snap_guide = sx
                else:
                    it.start_offset = max(0.0, tlm._snap((x - hold_x) / eff))
            elif sx is not None and sx <= mid:            # on-air side only
                it.start_offset = (sx - self._on) / eff; self._snap_guide = sx
            else:
                it.start_offset = tlm.resolve_bar_start(x, self._on, self._off, self._zoom)
        elif part == "bar_stop":
            if sx is not None and sx >= mid:              # off-air side only
                it.stop_offset = (sx - self._off) / eff; self._snap_guide = sx
            else:
                it.stop_offset = tlm.resolve_bar_stop(x, self._on, self._off, self._zoom)
        elif part == "bar_body":
            # Snap the START edge as the bar shifts (the STOP follows by the same delta).
            start0_x = self._on + self._drag["start0"] * eff
            sbx = self._snap_cursor(start0_x + (x - self._drag["press_x"]), it.uid)
            if sbx is not None:
                ds = (sbx - self._on) / eff - self._drag["start0"]; self._snap_guide = sbx
            else:
                ds = tlm._snap((x - self._drag["press_x"]) / eff)
            it.start_offset = min(self._drag["start0"] + ds, (mid - self._on) / eff)
            it.stop_offset = max(self._drag["stop0"] + ds, (mid - self._off) / eff)
        self._live_relayout(it)

    def _anchor_base_x(self, it) -> float:
        """The x a one-shot's offset is measured from while dragging: on-air for a start
        anchor, off-air for a stop anchor, and the Hold divider for a window-B (anchor='hold')
        one-shot (placed at hold_offset + its offset)."""
        if getattr(it, "anchor", "start") == "hold" and self._hold_off is not None:
            return self._on + self._hold_off * self._eff()
        return self._on if it.anchor == "start" else self._off

    # ── Drag snapping (to nearby step edges / anchors / ticks) ─────────────────
    def _snap_targets(self, exclude_uid):
        """Meaningful x positions a dragged edge can snap to: the on-air / off-air anchors,
        the Hold divider, every OTHER item's edges (a bar/ramp's start+stop, a pin's centre),
        and the major axis ticks across the defined region."""
        xs = [self._on, self._off]
        for h in self._holds:
            g = self._geom.get(h.uid)
            if g:
                xs.append(g["cx"])
        for it in self._rows:
            if it.uid == exclude_uid:
                continue
            g = self._geom.get(it.uid)
            if not g:
                continue
            if "start_x" in g:
                xs.append(g["start_x"]); xs.append(g["stop_x"])
            elif "cx" in g:
                xs.append(g["cx"])
        eff = self._eff(); tick_s = self._tick_interval()
        if tick_s > 0 and eff > 0:
            k = 0
            while self._on + k * tick_s * eff <= self._off + 1.0:
                xs.append(self._on + k * tick_s * eff); k += 1
            k = 1
            while self._on - k * tick_s * eff >= tlm.EDGE_PAD:
                xs.append(self._on - k * tick_s * eff); k += 1
        return xs

    def _snap_cursor(self, x, exclude_uid):
        """The nearest snap target x within SNAP_PX of `x`, or None."""
        best, best_d = None, SNAP_PX + 1e-6
        for tx in self._snap_targets(exclude_uid):
            d = abs(x - tx)
            if d < best_d:
                best_d, best = d, tx
        return best

    def _clamp_tune_offset(self, it, offset: float) -> float:
        """Keep a tune point inside the on-air span of the task it acts on: a
        start-anchored tune can't be dragged before the task's on-air start, a
        stop-anchored one can't pass its off-air stop. One-shots (not tunes), and window-B
        (anchor='hold') tunes — timed at proceed, not against the on-air window — act on
        their own task, so they're free to sit anywhere."""
        if getattr(it, "action", "run") != "tune" or it.anchor not in ("start", "stop"):
            return offset
        spans = [(b.start_offset, b.stop_offset) for b in self._items
                 if getattr(b, "kind", None) == "bar" and b.task_name == it.task_name]
        if not spans:
            return offset
        if it.anchor == "start":
            return max(offset, min(s for s, _ in spans))
        return min(offset, max(e for _, e in spans))

    def _live_relayout(self, it) -> None:
        """Update just the dragged item's geometry without resizing the canvas
        (keeps anchors fixed mid-drag so the item tracks the cursor smoothly)."""
        g = self._geom.get(it.uid)
        if not g:
            return
        if it.kind == "bar":
            g["start_x"] = tlm.offset_to_x(*tlm.bar_start_placement(it, self._hold_off),
                                           self._on, self._off, self._zoom)
            g["stop_x"] = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off, self._zoom)
        else:
            # _run_cx maps a window-B (anchor='hold') item to the Hold's side, so the pill
            # tracks the cursor correctly instead of jumping to the off-air anchor.
            g["cx"] = self._run_cx(it)
        if "panel" in g:
            g["panel"] = (self._item_left(it) + 2, g["panel"][1], g["panel"][2], g["panel"][3])
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._snap_guide = None
        if self._connect is not None:
            conn = self._connect
            self._connect = None
            if conn["moved"] and conn["target"] is not None:
                tgt, edge = conn["target"]
                self._make_anchor(conn["src"], tgt, edge)
            else:
                self.update()          # cancelled — clear the rubber-band
            return
        if self._drag is None:
            return
        drag = self._drag
        self._drag = None
        if drag["moved"]:
            self._undo.append(drag["undo0"])   # commit the pre-drag snapshot for undo
            del self._undo[:-100]
            self._redo.clear()
            self.relayout()
            self.changed.emit()
        # A non-moved press only selects (done in mousePress); double-click opens the editor.

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(e.position().x(), e.position().y())
        if hit is not None and hit[1] != "caret":
            self.edit_item(hit[0])

    def keyPressEvent(self, e):  # noqa: N802
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        key = e.key()
        if ctrl and key == Qt.Key.Key_Z and not shift:
            self.undo(); e.accept(); return
        if (ctrl and key == Qt.Key.Key_Y) or (ctrl and shift and key == Qt.Key.Key_Z):
            self.redo(); e.accept(); return
        if ctrl and key == Qt.Key.Key_D and self._selected is not None:
            it = next((o for o in self._items if o.uid == self._selected), None)
            if it is not None:
                self._duplicate_item(it)
            e.accept(); return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected is not None:
            self._delete_with_reanchor(self._selected)
            e.accept(); return
        super().keyPressEvent(e)

    # ── Hover tooltip ─────────────────────────────────────────────────────────
    def _update_tooltip(self, it, global_pt) -> None:
        uid = getattr(it, "uid", None) if it is not None else None
        if uid == self._hover_uid:
            if it is not None:
                QToolTip.showText(global_pt, self._tooltip_text(it), self)
            return
        self._hover_uid = uid
        if it is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(global_pt, self._tooltip_text(it), self)

    def _tooltip_text(self, it) -> str:
        """A rich, multi-line description of an item for hover (task, what it does, when it
        fires, and its anchor if step-anchored)."""
        if tlm._is_hold(it):
            return (f"<b>Hold</b><br>pauses the run at on-air "
                    f"{self._mmss(float(getattr(it, 'offset', 0.0)))}<br>"
                    f"<i>resume with Proceed</i>")
        act = getattr(it, "action", "run")
        lines: List[str] = []
        if it.kind == "bar":
            lines.append(f"<b>{it.task_name or '(no task)'}</b> · duration task")
            side = "hold" if getattr(it, "start_anchor", "start") == "hold" else "start"
            lines.append("starts " + _timing_text(float(getattr(it, "start_offset", 0.0)), side, True))
            lines.append("stops " + _timing_text(float(getattr(it, "stop_offset", 0.0)), "stop", True))
        elif act == "ramp":
            lines.append(f"<b>{it.task_name or '(no task)'}</b> · ramp")
            lines.append(_ramp_summary(getattr(it, "ramp", None), getattr(it, "anchor", "start")))
        elif act == "tune":
            lines.append(f"<b>{it.task_name or '(no task)'}</b> · tune")
            overrides = self._editor._pill_power_display(it)
            changes = ", ".join(f"{k}={overrides.get(k, v)}" for k, v in (it.params or {}).items())
            if changes:
                lines.append(changes)
        else:
            lines.append(f"<b>{it.task_name or '(no task)'}</b> · one-shot")
        anc = getattr(it, "anchor", "start")
        if anc == "step":
            tgt = next((o for o in self._items
                        if (getattr(o, "step_id", "") or "") == (getattr(it, "anchor_step_id", "") or "")), None)
            tname = (getattr(tgt, "task_name", "") or "?") if tgt is not None else "?"
            lines.append(f"⚓ after {tname}'s {getattr(it, 'anchor_edge', 'end')} "
                         f"+{self._mmss(float(getattr(it, 'offset', 0.0)))}")
        elif act != "ramp" and it.kind != "bar":
            side = "hold" if anc == "hold" else (anc if anc in ("start", "stop") else "start")
            lines.append("fires " + _timing_text(float(getattr(it, "offset", 0.0)), side, True))
        return "<br>".join(lines)

    def leaveEvent(self, e):  # noqa: N802
        self._hover_uid = None
        QToolTip.hideText()
        super().leaveEvent(e)

    # ── Anchor create / detach (100% UI, no forms) ─────────────────────────────
    def _make_anchor(self, src_uid: int, tgt, edge: str) -> None:
        """Anchor the source item's start to `tgt`'s `edge` (a drag-to-anchor drop). Keeps
        the source visually in place (offset = the current gap, clamped >= 0)."""
        src = next((it for it in self._items if it.uid == src_uid), None)
        if src is None:
            return
        off = tlm.step_drop_offset(self._items, src_uid, tgt.uid, edge,
                                   self._hold_off, self._step_bases)
        if off is None:
            self.update()
            return
        self._record()
        sid = tlm.ensure_step_id(tgt)
        if not sid:
            self.update()
            return
        src.anchor = "step"
        src.anchor_step_id = sid
        src.anchor_edge = edge
        src.offset = off
        self._selected = src_uid
        self.relayout()
        self.changed.emit()

    def _detach_anchor(self, uid: Optional[int]) -> None:
        """Remove a step anchor (the "Remove anchor" chip / a UI detach): revert the item to
        a plain on-air start anchor at the offset it currently resolves to, so it stays put."""
        it = next((o for o in self._items if o.uid == uid), None)
        if it is None or getattr(it, "anchor", "") != "step":
            return
        self._record()
        base = self._step_bases.get(uid)
        it.offset = base if base is not None else float(getattr(it, "offset", 0.0))
        it.anchor = "start"
        it.anchor_step_id = ""
        it.anchor_edge = "end"
        self.relayout()
        self.changed.emit()

    def _duplicate_item(self, it) -> None:
        """Clone an item (a fresh uid + no step_id — the copy is not a reference target),
        nudged a little so it doesn't sit exactly on the original, and select it. A Hold is
        unique per sequence, so it isn't duplicable."""
        if tlm._is_hold(it):
            return
        clone = copy.deepcopy(it)
        clone.uid = next(tlm._ids)
        clone.step_id = ""
        nudge = 15.0
        if clone.kind == "bar":
            clone.start_offset = float(getattr(clone, "start_offset", 0.0)) + nudge
        else:
            clone.offset = float(getattr(clone, "offset", 0.0)) + nudge
        self.add_item(clone)
        self._selected = clone.uid
        self.update()

    def _delete_with_reanchor(self, uid: Optional[int]) -> None:
        """Delete an item. If OTHER steps anchor to it, re-anchor each dependent to a plain
        on-air `start` at the time it currently resolves to — so a dependent stays at the
        instant it was meant to fire instead of orphaning when its anchor disappears."""
        target = next((o for o in self._items if o.uid == uid), None)
        if target is None:
            return
        self._record()
        sid = getattr(target, "step_id", "") or ""
        if sid:
            for dep in self._items:
                if dep is target or getattr(dep, "anchor", "") != "step":
                    continue
                if (getattr(dep, "anchor_step_id", "") or "") != sid:
                    continue
                base = self._step_bases.get(dep.uid)
                dep.offset = base if base is not None else float(getattr(dep, "offset", 0.0))
                dep.anchor = "start"
                dep.anchor_step_id = ""
                dep.anchor_edge = "end"
        self.remove_item(uid, record=False)     # one undo entry covers the reanchor + delete

    # ── Right-click context menu ──────────────────────────────────────────────
    def contextMenuEvent(self, e):  # noqa: N802
        hit = self._hit(e.pos().x(), e.pos().y())
        if hit is None:
            e.ignore()
            return
        it = hit[0]
        self._selected = it.uid
        self.update()
        self._open_context_menu(it, e.globalPos())
        e.accept()

    def _context_menu_spec(self, it) -> List[str]:
        """Labels for the right-click menu on `it`, in order ('—' = a separator)."""
        spec = ["Edit…"]
        if not tlm._is_hold(it):
            spec.append("Duplicate")
        if getattr(it, "anchor", "") == "step":
            spec.append("Remove anchor")
        spec += ["—", "Delete"]
        return spec

    def _run_context_action(self, it, label: str) -> None:
        if label == "Edit…":
            self.edit_item(it)
        elif label == "Duplicate":
            self._duplicate_item(it)
        elif label == "Remove anchor":
            self._detach_anchor(it.uid)
        elif label == "Delete":
            self._delete_with_reanchor(it.uid)

    def _open_context_menu(self, it, global_pos) -> None:
        menu = QMenu(self)
        actions = {}
        for label in self._context_menu_spec(it):
            if label == "—":
                menu.addSeparator()
            else:
                actions[menu.addAction(label)] = label
        chosen = menu.exec(global_pos)
        if chosen in actions:
            self._run_context_action(it, actions[chosen])

    # ── Zoom (Ctrl+wheel on a mouse; pinch on a touchpad) ─────────────────────

    def wheelEvent(self, e):  # noqa: N802
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            if delta:
                self._apply_zoom(1.0 + ZOOM_WHEEL * delta, e.position().x())
            e.accept()
        else:
            super().wheelEvent(e)   # no Ctrl → let the scroll area pan

    def event(self, e):  # noqa: N802
        t = e.type()
        # Touchpad pinch arrives as a native zoom gesture on macOS / Windows…
        if t == QEvent.Type.NativeGesture:
            if e.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._apply_zoom(1.0 + e.value(), e.position().x())
                return True
        # …and as a Qt PinchGesture where the platform routes it that way.
        elif t == QEvent.Type.Gesture:
            pinch = e.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                sf = pinch.scaleFactor()   # incremental scale since the last event
                if sf and abs(sf - 1.0) > 1e-4:
                    self._apply_zoom(sf, self._viewport_center_x())
                e.accept()
                return True
        return super().event(e)

    def _viewport_center_x(self) -> float:
        if self._scroll is not None:
            return self._scroll.horizontalScrollBar().value() + self._scroll.viewport().width() / 2
        return self.width() / 2

    def reset_zoom(self) -> None:
        if self._zoom != 1.0:
            self._zoom = 1.0
            self.relayout()
            self._editor._sync_zoom()

    def _apply_zoom(self, factor: float, cursor_x: float) -> None:
        old = self._zoom
        new = max(ZOOM_MIN, min(ZOOM_MAX, old * factor))
        if abs(new - old) < 1e-6:
            return
        ratio = new / old
        hbar = self._scroll.horizontalScrollBar() if self._scroll is not None else None
        old_scroll = hbar.value() if hbar is not None else 0
        # The content point under the cursor scales about the fixed edge inset, so
        # keep it stationary in the viewport by adjusting the horizontal scroll.
        new_content_x = tlm.EDGE_PAD + (cursor_x - tlm.EDGE_PAD) * ratio
        self._zoom = new
        self.relayout()
        if hbar is not None:
            viewport_x = cursor_x - old_scroll
            hbar.setValue(int(round(new_content_x - viewport_x)))
        self._editor._sync_zoom()

    # ── Editing ───────────────────────────────────────────────────────────────

    def _dialog_for(self, item, new: bool):
        # Ramps and the Hold marker have their own editors; everything else uses the
        # task-centric step editor.
        if tlm._is_hold(item):
            return HoldEditorDialog(item, self._editor, new=new, parent=self)
        if getattr(item, "action", "run") == "ramp":
            return RampEditorDialog(item, self._editor, new=new, parent=self)
        return StepEditorDialog(item, self._editor, new=new, parent=self)

    def edit_item(self, item) -> None:
        dlg = self._dialog_for(item, new=False)
        r = dlg.exec()
        if r == dlg.REMOVE:
            self.remove_item(item.uid)
        elif r == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self.replace_item(item.uid, dlg.result_item)

    def add_new(self, kind: str) -> None:
        default_task = self._editor.available_tasks()[0] if self._editor.available_tasks() else ""
        if kind == "hold":
            # One Hold per sequence (docs/sequence-hold-step.md §5.1). Seed its position
            # after the furthest window-A on-air point so it reads as "pause at the top".
            if tlm.has_hold(self._items):
                QMessageBox.information(
                    self, "Hold", "A sequence can have only one Hold — edit or remove "
                    "the existing one.")
                return
            item = tlm.RunItem(task_name="", action="hold", anchor="start",
                               offset=self._default_hold_offset())
        elif kind == "bar":
            item = tlm.BarItem(task_name=default_task, start_offset=0.0, stop_offset=0.0)
        elif kind == "tune":
            item = tlm.RunItem(task_name=default_task, action="tune", anchor="start", offset=0.0)
        elif kind == "ramp":
            item = tlm.RunItem(task_name=default_task, action="ramp", anchor="start",
                               offset=0.0, ramp={})
        else:
            item = tlm.RunItem(task_name=default_task, anchor="start", offset=0.0)
        dlg = self._dialog_for(item, new=True)
        r = dlg.exec()
        if r == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self.add_item(dlg.result_item)
            self._selected = dlg.result_item.uid
            self.update()

    def _default_hold_offset(self) -> float:
        """A sensible starting position for a new Hold: just past the furthest on-air
        (window-A) point, so it sits at the top of the run. A ramp counts by its END (so a
        Hold lands after an up-ramp, not mid-ramp). 60 s when nothing precedes it."""
        latest = 0.0
        for it in self._items:
            if tlm._is_hold(it):
                continue
            if getattr(it, "kind", None) == "bar":
                latest = max(latest, float(getattr(it, "start_offset", 0.0)))
            elif tlm._is_ramp(it):
                # A ramp's furthest on-air moment is its right (start-anchored) end.
                for anchor, off in tlm.ramp_span(it, self._hold_off, self._step_bases):
                    if anchor == "start":
                        latest = max(latest, off)
            elif getattr(it, "anchor", "start") in ("start", "step"):
                _a, o = tlm.effective_anchor_offset(it, self._hold_off, self._step_bases)
                latest = max(latest, o)
        return round(latest, 1) if latest > 0 else 60.0


# ── The step editor (source task + full parameter form + type + offsets) ──────

class StepEditorDialog(QDialog):
    """Configure one timeline object: which task, its parameters (the full form,
    pre-filled from the task and never mutating it), whether it's a duration bar
    or a one-shot, and its offsets. A Remove button deletes it."""

    REMOVE = 2   # custom result code (distinct from Accepted=1 / Rejected=0)

    def __init__(self, item, editor: "TimelineEditor", new: bool, parent=None):
        super().__init__(parent)
        self._src = item
        self._editor = editor
        self._new = new
        self.result_item: Optional[object] = None

        self._current_script = ""
        self._pending_prefill: Optional[List[str]] = None
        self._prefill_params: Dict[str, object] = dict(getattr(item, "params", {}) or {})
        self._task_touched = False
        self._built = False   # suppress task reselection during the initial build

        self.setWindowTitle("New step" if new else "Edit step")
        self.setMinimumWidth(440)
        self._build(item)
        fit_dialog_to_screen(self, 540, 660)     # room, but capped to the screen so the footer shows
        self._built = True

        if editor._hub is not None:
            editor._hub.task_done.connect(self._on_params)
        self.finished.connect(lambda _=0: self._disconnect())

        # Initial population (uses whatever task the type-filtered dropdown settled on).
        self._select_task(self._task.currentText(), initial=True)

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self, item) -> None:
        from .dialog_style import editor_qss
        from .param_widgets import Dropdown
        self.setStyleSheet(editor_qss())
        # One shared scroll for the whole dialog: the body (with the embedded parameter
        # form) scrolls as one, buttons pinned below. See TaskEditorDialog._build.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QScrollArea()
        body.setWidgetResizable(True)
        body.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # Task — only tasks already defined on the unit are selectable.
        self._task = Dropdown()
        tasks = self._editor.available_tasks()
        if tasks:
            self._task.addItems(tasks)
        if item.task_name and self._task.findText(item.task_name) < 0:
            self._task.addItem(item.task_name)
        if item.task_name:
            self._task.setCurrentText(item.task_name)
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        form.addRow("Task", self._task)

        # Type: duration bar vs one-shot pill.
        self._type = Dropdown()
        self._type.addItem("Duration  (on-air → off-air)", "bar")
        self._type.addItem("One-shot  (fires once)", "run")
        self._type.addItem("Tune  (set live params)", "tune")
        if item.kind == "bar":
            self._type.setCurrentIndex(0)
        elif getattr(item, "action", "run") == "tune":
            self._type.setCurrentIndex(2)
        else:
            self._type.setCurrentIndex(1)
        self._type.currentIndexChanged.connect(self._sync_type)
        form.addRow("Type", self._type)

        # Duration START anchor: on-air (the usual case) or the Hold — the latter makes
        # this a window-B duration task that only starts once the operator proceeds (its
        # STOP stays off-air). Offered only when a Hold exists (or the bar already uses it).
        self._start_anchor = Dropdown()
        self._start_anchor.addItem("on-air (T0)", "start")
        if self._editor.has_hold() or getattr(item, "start_anchor", "") == "hold":
            self._start_anchor.addItem("at Hold (resume)", "hold")
        self._start_anchor.currentIndexChanged.connect(self._sync_start_anchor)
        self._row_start_anchor = self._add_row(form, "Start anchor", self._start_anchor)

        # Duration offsets (two ends).
        self._start_off = DurationSpinBox()
        self._stop_off = DurationSpinBox()
        self._row_start = self._add_row(form, "Start — from ON-AIR", self._start_off)
        self._row_stop = self._add_row(form, "Stop — from OFF-AIR", self._stop_off)

        # One-shot anchor + single offset. "Hold" (window B — steps anchored to the
        # Hold, resolved at proceed) is offered only once a Hold exists on the timeline,
        # or when editing a step that already anchors to it.
        self._anchor = Dropdown()
        self._anchor.addItem("on-air (T0)", "start")
        self._anchor.addItem("off-air", "stop")
        if self._editor.has_hold() or getattr(item, "anchor", "") == "hold":
            self._anchor.addItem("hold (after Hold)", "hold")
        # Step-to-step anchoring (agent ≥ 1.24.0): fire this step relative to ANOTHER step's
        # start/end edge, so editing that step moves this one. Offered when there's an eligible
        # target (no cycle), or when the step already uses it; saving to an agent that can't
        # resolve it is blocked at save-time (the _blocks_on_step_anchor gate). The window-B
        # Hold path and a step anchor are mutually exclusive (Phase 1).
        self._step_targets = tlm.eligible_step_targets(self._editor.items(), item.uid) \
            if not self._editor.has_hold() else []
        if self._step_targets or getattr(item, "anchor", "") == "step":
            self._anchor.addItem("after another step…", "step")
        self._run_off = DurationSpinBox()
        self._row_anchor = self._add_row(form, "Anchor", self._anchor)

        # Target step + edge pickers (shown only when anchor == "step").
        self._anchor_target = Dropdown()
        for tgt in self._step_targets:
            self._anchor_target.addItem(tlm._target_label(tgt), getattr(tgt, "uid", None))
        self._anchor_edge = Dropdown()
        self._anchor_edge.addItem("its end", "end")
        self._anchor_edge.addItem("its start", "start")
        self._row_anchor_target = self._add_row(form, "Anchor to", self._anchor_target)
        self._row_anchor_edge = self._add_row(form, "Relative to", self._anchor_edge)
        self._row_run = self._add_row(form, "Offset — from anchor", self._run_off)

        # Prefill offset widgets from the source item.
        if item.kind == "bar":
            sai = self._start_anchor.findData(getattr(item, "start_anchor", "start"))
            self._start_anchor.setCurrentIndex(sai if sai >= 0 else 0)
            self._start_off.setValue(float(item.start_offset))
            self._stop_off.setValue(float(item.stop_offset))
        else:
            ai = self._anchor.findData(getattr(item, "anchor", "start"))
            self._anchor.setCurrentIndex(ai if ai >= 0 else 0)
            self._run_off.setValue(float(item.offset))
            if getattr(item, "anchor", "") == "step":
                # Select the target whose step_id matches, and the stored edge.
                want = getattr(item, "anchor_step_id", "") or ""
                for tgt in self._step_targets:
                    if (getattr(tgt, "step_id", "") or "") == want:
                        ti = self._anchor_target.findData(getattr(tgt, "uid", None))
                        if ti >= 0:
                            self._anchor_target.setCurrentIndex(ti)
                        break
                ei = self._anchor_edge.findData(getattr(item, "anchor_edge", "end") or "end")
                self._anchor_edge.setCurrentIndex(ei if ei >= 0 else 0)
        self._anchor.currentIndexChanged.connect(self._sync_anchor)

        outer.addLayout(form)

        # The full parameter form for the task's script. (_params_status is created here
        # but mounted in the pinned footer below, so a validation error stays visible even
        # when the form is scrolled.)
        self._params_status = QLabel("")
        self._params_status.setWordWrap(True)
        self._params_status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

        # A warning (not a block) when this step's effective frequency puts the running
        # --power beyond what the unit can deliver there — the runtime clamps it safely.
        self._clamp_warn = QLabel("")
        self._clamp_warn.setWordWrap(True)
        self._clamp_warn.setVisible(False)
        self._clamp_warn.setStyleSheet(
            f"font-size: 11px; color: {Palette.ARMED}; font-weight: 600;")
        outer.addWidget(self._clamp_warn)

        self._form = ParamForm()
        self._form.changed.connect(self._update_clamp_warning)
        # Moving the step changes which earlier steps precede it, hence the carried-forward
        # frequency/power/bridge params — re-fold the --power card and refresh the clamp warning.
        # (Wired here, after _clamp_warn exists.)
        self._anchor.currentIndexChanged.connect(lambda *_: self._refold_for_position())
        self._run_off.valueChanged.connect(lambda *_: self._refold_for_position())
        # The parameter form sits directly in the shared scroll (no inner scroll), so
        # the whole dialog scrolls as one and the form reads white like the Run dialog.
        pcard = QFrame()
        pcard.setObjectName("stepParamCard")
        # Scope the border to the frame itself — a bare "border: …" stylesheet cascades
        # onto every child widget (each label would get its own box), so use an id selector.
        pcard.setStyleSheet(
            f"#stepParamCard {{ background: {Palette.SURFACE}; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 8px; }}")
        pcl = QVBoxLayout(pcard)
        pcl.setContentsMargins(1, 1, 1, 1)
        pcl.addWidget(self._form)
        outer.addWidget(pcard)

        self._extra = QLineEdit()
        self._extra.setPlaceholderText("extra args not covered by the form (optional)")
        eform = QFormLayout(); eform.setContentsMargins(0, 0, 0, 0)
        eform.addRow("Extra args", self._extra)
        self._extra_row = QWidget()
        self._extra_row.setLayout(eform)
        outer.addWidget(self._extra_row)

        self._hint = QLabel("Parameters are pre-filled from the task; changing them here only "
                            "affects this step (the task is left unchanged).")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        # Mount the scrollable body, then pin the buttons below it.
        body.setWidget(content)
        root.addWidget(body, 1)

        buttons = QDialogButtonBox()
        if not self._new:
            remove = QPushButton("Remove")
            remove.setStyleSheet(f"color: {Palette.CRASH};")
            buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: self.done(self.REMOVE))
        ok_btn = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        ok_btn.setDefault(True)                  # accent primary
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        footer = QWidget()
        foot = QVBoxLayout(footer)
        foot.setContentsMargins(16, 8, 16, 12)
        foot.setSpacing(6)
        foot.addWidget(self._params_status)      # pinned, so validation errors are always seen
        foot.addWidget(buttons)
        root.addWidget(footer)

        self._sync_type()

    @staticmethod
    def _add_row(form: QFormLayout, label: str, widget: QWidget) -> QWidget:
        lbl = QLabel(label)
        form.addRow(lbl, widget)
        widget._row_label = lbl   # keep a handle so we can show/hide the pair
        return widget

    def _set_row_visible(self, widget: QWidget, visible: bool) -> None:
        widget.setVisible(visible)
        if hasattr(widget, "_row_label"):
            widget._row_label.setVisible(visible)

    def _sync_start_anchor(self) -> None:
        """Relabel a bar's START-offset row to match its anchor: measured from ON-AIR
        (T0) normally, or from the Hold's resume instant for a window-B duration task."""
        hold = self._start_anchor.currentData() == "hold"
        lbl = getattr(self._start_off, "_row_label", None)
        if lbl is not None:
            lbl.setText("Start — from Hold (resume)" if hold else "Start — from ON-AIR")

    def _sync_anchor(self) -> None:
        """Show the target + edge pickers only when anchoring to another step, and relabel
        the offset row to match (from the anchor, or from the chosen step's edge)."""
        is_step = self._anchor.currentData() == "step"
        self._set_row_visible(self._anchor_target, is_step)
        self._set_row_visible(self._anchor_edge, is_step)
        lbl = getattr(self._run_off, "_row_label", None)
        if lbl is not None:
            lbl.setText("Offset — after the step" if is_step else "Offset — from anchor")

    def _is_tune(self) -> bool:
        return self._type.currentData() == "tune"

    def _tasks_for(self, mode: str) -> List[str]:
        """The tasks selectable for a step of this type. A tune step acts on an
        already-running task, so only tasks started in this sequence qualify."""
        if mode == "tune":
            getter = getattr(self._editor, "sequence_task_names", None)
            if getter is not None:
                return getter()
        return self._editor.available_tasks()

    def _repopulate_tasks(self, mode: str) -> bool:
        """Rebuild the task dropdown for the current type. Returns True if the
        selected task changed (e.g. switching to Tune dropped a non-sequence task)."""
        tasks = self._tasks_for(mode)
        cur = self._task.currentText().strip()
        self._task.blockSignals(True)
        self._task.clear()
        self._task.addItems(tasks)
        # Editing a non-tune step whose task isn't a known unit task: keep it listed.
        if mode != "tune" and cur and self._task.findText(cur) < 0:
            self._task.addItem(cur)
        if cur and self._task.findText(cur) >= 0:
            self._task.setCurrentText(cur)
        elif self._task.count():
            self._task.setCurrentIndex(0)
        self._task.blockSignals(False)
        return self._task.currentText().strip() != cur

    def _sync_type(self) -> None:
        mode = self._type.currentData()
        task_changed = self._repopulate_tasks(mode)
        is_bar = mode == "bar"
        is_tune = mode == "tune"
        self._set_row_visible(self._start_off, is_bar)
        self._set_row_visible(self._stop_off, is_bar)
        # The START-anchor picker only matters for a bar, and only when a Hold makes the
        # window-B option meaningful (else the dropdown has a single entry — keep it hidden
        # so a normal sequence's bar editor is unchanged).
        self._set_row_visible(self._start_anchor, is_bar and self._start_anchor.count() > 1)
        self._set_row_visible(self._anchor, not is_bar)   # a point (run/tune) has one anchor
        self._set_row_visible(self._run_off, not is_bar)
        if is_bar:
            self._sync_start_anchor()
            self._set_row_visible(self._anchor_target, False)
            self._set_row_visible(self._anchor_edge, False)
        else:
            self._sync_anchor()
        # Tune sends live-parameter values, not CLI args.
        self._extra_row.setVisible(not is_tune)
        self._hint.setText(
            "Sets the running task's live parameters at this offset. The task must "
            "be started by a duration step in this sequence."
            if is_tune else
            "Parameters are pre-filled from the task; changing them here only "
            "affects this step (the task is left unchanged).")
        if not self._built:
            return   # initial build; __init__ runs the first _select_task itself
        if task_changed:
            self._select_task(self._task.currentText())   # different task → reload its params
        elif self._current_script:
            # Same task, but the form's contents differ (tune shows only live params).
            self._build_form(self._current_script)

    # ── Task → script → parameter form ────────────────────────────────────────

    def _select_task(self, task: str, initial: bool = False) -> None:
        if not initial:
            self._task_touched = True
        task = (task or "").strip()
        script, default_args = self._editor.script_for_task(task)
        self._current_script = script

        # On first open of an existing step, keep its own args; otherwise (or when
        # the task changes) start from the newly-selected task's defaults.
        if initial and not self._new and self._src.args:
            self._pending_prefill = list(self._src.args)
        else:
            self._pending_prefill = list(default_args)

        if not script:
            self._form.set_params([])
            self._apply_prefill()
            self._set_status(
                "this task has no script parameter schema — use extra args" if task else "")
            return

        cache = self._editor.param_cache()
        if script in cache:
            self._build_form(script)
            return
        self._set_status(f"loading parameters for {script}…")
        self._form.set_params([])
        if self._editor._hub is None:
            return
        if script in self._editor._params_inflight:
            return
        self._editor._params_inflight.add(script)
        self._editor._hub.run_async(
            f"stepdlg_params:{self._editor._hostname}:{script}",
            lambda s=script: self._editor._hub.fleet.get(self._editor._hostname).get_script_params(s),
        )

    def _on_params(self, label: str, result) -> None:
        if not label.startswith("stepdlg_params:"):
            return
        parts = label.split(":", 2)
        if len(parts) < 3 or parts[1] != self._editor._hostname:
            return
        script = parts[2]
        self._editor._params_inflight.discard(script)
        if isinstance(result, Exception):
            if script == self._current_script:
                self._set_status(f"could not load parameters: {result}", error=True)
            return
        self._editor.cache_script_meta(script, result)
        # These params may be the last thing the sequence-level achievability banner was waiting on
        # (a clamp can only be judged once the task's --power range is foldable), so refresh it.
        self._editor._update_achievability()
        if script == self._current_script:
            self._build_form(script)

    def _build_form(self, script: str) -> None:
        specs = self._editor.param_cache().get(script, [])
        # Absolute power is offered when a unit is targeted (plan/sequences tab) AND
        # it's calibrated for this task's signal; else relative (gain) only. Open in
        # the mode the step's args used (relative if they set --gain).
        task = self._task.currentText().strip()
        bounds = self._editor.cal_bounds_for_task(task)
        hint = self._editor.power_hint_for_task(task)     # aggregate range for Library authoring
        abs_allowed = self._editor.absolute_allowed()
        # No-safeguard caution: the task opts into no calibration signal, or a targeted
        # unit isn't calibrated for it — either way power/gain go out raw.
        from .param_form import calibration_caution
        caution = calibration_caution(self._editor.has_cal_signal(task),
                                      targeted=abs_allowed, calibrated=bounds is not None,
                                      script_calibratable=self._editor.script_calibratable(task))
        prefill = self._pending_prefill or self._src.args or []
        # Open in the mode the task was saved with (absolute if it sets --power, relative
        # if --gain) — otherwise a saved-absolute task could fall back to the form's
        # default (or a mode left over from a previously-selected task).
        mode = power_mode_of_args(prefill)
        freq_param = self._editor._script_cal_freq_params.get(script)
        # The multi-quantity --power card (companion read-outs + "Control in this →") is offered
        # whenever the task's SCRIPT declares power-quantity laws, exactly as in the Run/live-tune
        # forms — the card is gated purely on set_params seeing these laws.
        power_laws = self._editor._script_power_laws.get(script, [])
        # The frequency (and power) in effect when this step fires — replayed from the
        # task's deployed args and the earlier same-task steps — so the form folds the
        # --power range at that frequency even when this step doesn't set --freq itself.
        self._carried = self._carried_values(task, script, specs)
        carried_freq = self._carried.get(freq_param) if freq_param else None
        if self._is_tune():
            # Only live-tunable params can be changed mid-run; the checkboxes let you pick which
            # ones this step sets. But the card/limits fold through the FULL schema: a non-live
            # param (a fixed --freq, or a bridge param carried from an earlier step) and a hidden
            # derived key (GPS C/A's enbw from --sidelobes) are kept as fold CONTEXT — present in
            # the schema, seeded from the carried state, but never rendered as an editable field.
            # (Mirrors live_tune_dialog._prepare_specs, sourcing _carried, not a deployed command.)
            live_specs = [s for s in specs if s.get("live")]
            context_dests = [s.get("dest") for s in specs if not s.get("live")]
            # A LIVE bridge param the operator isn't setting in THIS step (e.g. a chirp's --bw in a
            # power-only tune step) must still fold at the CARRIED value, not its schema default —
            # otherwise the spectral-density view/limit folds at the wrong bandwidth and lets you
            # command a live density the fire-time sweep can't deliver. Seed those from carried too
            # (they stay RENDERED so the operator can still tick + change them).
            seed_dests = set(context_dests) | self._fold_bridge_dests(specs, power_laws)
            seeded = self._seed_context_from_carried(specs, seed_dests, self._carried)
            self._form.set_params(seeded, selectable=True, cal_bounds=bounds,
                                  absolute_allowed=abs_allowed, default_power_mode=mode,
                                  hint_bounds=hint, caution=caution,
                                  cal_freq_param=freq_param, cal_freq_default=carried_freq,
                                  power_laws=power_laws, context_dests=context_dests)
            self._set_status(
                "tick the parameters to set at this offset" if live_specs
                else "this task's script declares no live parameters")
            self._seed_from_params()
        else:
            self._form.set_params(specs, cal_bounds=bounds,
                                  absolute_allowed=abs_allowed, default_power_mode=mode,
                                  hint_bounds=hint, caution=caution,
                                  cal_freq_param=freq_param, cal_freq_default=carried_freq,
                                  power_laws=power_laws)
            self._set_status(
                "" if specs else "this script declares no parameters — use extra args")
            self._apply_prefill()
        # Restore the control quantity the operator authored this step in (persisted power_view),
        # so reopening shows --power in that quantity. No-op for a new step, a form with no power
        # views, or a legacy/mismatched view (falls back to the signal's default quantity).
        self._form.set_power_view(getattr(self._src, "power_view", None))
        self._update_clamp_warning()
        if bounds and self._editor.cal_is_stale():
            self._set_status("absolute power uses last-known calibration "
                                        "(unit offline) — refreshes when it reconnects")

    @staticmethod
    def _fold_bridge_dests(specs: list, power_laws: list) -> set:
        """Dests the --power laws key on that ARE fields in this schema (e.g. a chirp's --bw behind
        the spectral-density view). These must fold at the carried value even when they're live, so
        a tune step editing only --power still folds the view/limit at the running bandwidth."""
        spec_dests = {s.get("dest") for s in specs}
        return {lw.get("param") for lw in (power_laws or [])
                if lw.get("param") in spec_dests}

    @staticmethod
    def _seed_context_from_carried(specs: list, seed_dests, carried: dict) -> list:
        """Return the schema with each named param's ``default`` seeded from the carried sequence
        state, so the --power range/limits and the companions fold at what the task is actually
        running with when this step fires — not the schema default. ``seed_dests`` is the fold-
        context (non-live) params PLUS any live bridge param the view keys on (a chirp's --bw).
        Returns fresh spec copies (never mutates the shared param cache). The tune analogue of
        live_tune_dialog._prepare_specs, sourcing _carried instead of a deployed command."""
        ctx = set(seed_dests or [])
        out = []
        for s in specs:
            val = (carried or {}).get(s.get("dest"))
            if s.get("dest") in ctx and isinstance(val, (int, float)) and not isinstance(val, bool):
                out.append({**s, "default": val})
            else:
                out.append(s)
        return out

    def _refold_for_position(self) -> None:
        """This step's carried-forward state depends on where it sits (which earlier steps
        precede it), so a moved anchor/offset can change the fold frequency and bridge params.
        Recompute the carried state and re-seed the form's fold context (never touching the
        operator's live edits), then refresh the clamp caption."""
        script = self._current_script
        if script and self._editor is not None:
            task = self._task.currentText().strip()
            specs = self._editor.param_cache().get(script, [])
            freq_param = self._editor._script_cal_freq_params.get(script)
            self._carried = self._carried_values(task, script, specs)
            carried_freq = self._carried.get(freq_param) if freq_param else None
            # Re-seed the fold context (non-live params) AND any live bridge param the operator
            # isn't setting here (a chirp's --bw), so moving the step re-folds the --power view/
            # limit through the new carried bandwidth — mirrors the seed set in _build_form.
            power_laws = self._editor._script_power_laws.get(script, [])
            seed_dests = set(self._context_dests_now(specs))
            if self._is_tune():
                seed_dests |= self._fold_bridge_dests(specs, power_laws)
            context_defaults = {d: self._carried.get(d)
                                for d in seed_dests
                                if isinstance(self._carried.get(d), (int, float))
                                and not isinstance(self._carried.get(d), bool)}
            self._form.set_fold_context(cal_freq_default=carried_freq,
                                        context_defaults=context_defaults)
        self._update_clamp_warning()

    def _context_dests_now(self, specs: list) -> list:
        """The fold-context (non-live) dests for the current step type — the tune step folds the
        full schema through its non-live params; a run/bar step renders everything (no context)."""
        return [s.get("dest") for s in specs if not s.get("live")] if self._is_tune() else []

    def _current_order_key(self):
        """This step's best-effort position on the task's timeline (see
        timeline_model.carry_order_key), so state is carried forward only from earlier
        steps. A duration bar starts the task (rank 0); a tune/ramp uses its anchor+offset —
        hold-boundary aware, so a window-B (anchor='hold') step orders after window A."""
        if self._is_tune() or self._type.currentData() == "ramp":
            anchor = self._anchor.currentData() or "start"
            off = round(self._run_off.value(), 1)
            items = list(self._editor.items())
            h_off = tlm.hold_offset(items)
            if anchor == "step":
                # Order at the RESOLVED base (target edge + offset) using the LIVE pickers, so
                # carried state reflects only the steps that genuinely precede this one.
                tgt = next((it for it in items
                            if getattr(it, "uid", None) == self._anchor_target.currentData()), None)
                if tgt is not None:
                    e = tlm.step_edge_offset(items, getattr(tgt, "step_id", "") or "",
                                             self._anchor_edge.currentData() or "end", h_off)
                    if e is not None:
                        return (0, e + off)
            return tlm.carry_order_key(anchor, off, h_off)
        return (0, 0.0)                                  # a bar / run starts the task

    def _carried_values(self, task: str, script: str, specs: list) -> dict:
        """The {dest: value} parameter state (effective --freq / --power) the task is
        running with when this step fires — the task's deployed args replayed through the
        earlier same-task steps in this sequence."""
        _s, base_args = self._editor.script_for_task(task)
        try:
            return tlm.sequence_effective_values(
                self._editor.items(), task, base_args, specs, self._src.uid,
                target_key=self._current_order_key())
        except Exception:      # noqa: BLE001  — a warning helper must never break the editor
            return {}

    def _update_clamp_warning(self) -> None:
        """Warn (never block) when this step's effective frequency puts the running --power
        beyond what the unit can deliver there — the runtime clamps it, so the delivered
        power won't match the number. Effective freq/power = the carried-forward state with
        this step's own set values layered on top."""
        from state.power_fold import clamp_warning
        from .param_form import find_power_index, fold_params_from_values
        lbl = self._clamp_warn
        task = self._task.currentText().strip()
        bounds = self._editor.cal_bounds_for_task(task)
        script, _ = self._editor.script_for_task(task)
        if not bounds:
            lbl.setVisible(False); return
        specs = self._editor.param_cache().get(script, [])
        freq_param = self._editor._script_cal_freq_params.get(script)
        pidx = find_power_index(specs)
        power_dest = specs[pidx]["dest"] if pidx is not None else None
        if not freq_param or power_dest is None:
            lbl.setVisible(False); return
        carried = self._carried_values(task, script, specs)
        effective = {**carried, **self._form.values()}   # this step's set values win
        # The effective carrier is in the freq field's OWN unit (e.g. MHz); the fold expects Hz.
        # Convert before folding — a raw MHz value folded as Hz would clamp against ~0 Hz.
        freq_unit = next((s.get("unit") for s in specs if s.get("dest") == freq_param), None)
        freq_val = effective.get(freq_param)
        freq_hz = (float(freq_val) * hz_per_unit(freq_unit)
                   if isinstance(freq_val, (int, float)) and not isinstance(freq_val, bool)
                   else None)
        # Fold the ceiling through the LIVE bridge params too (e.g. a chirp's --bw, GPS C/A's
        # enbw behind --sidelobes) — the run/live-tune forms already do (via fold_params). The
        # step editor has no single ParamForm holding the full effective state (a bridge-keyed
        # source may be CARRIED, not in this step's form, and derived keys like enbw aren't in the
        # raw carried state at all), so resolve the keyed params over the effective dict instead.
        params = fold_params_from_values(bounds.get("artifact"), specs, effective)
        msg = clamp_warning(bounds.get("artifact"), freq_hz, effective.get(power_dest),
                            params=params)
        lbl.setText("⚠ " + msg if msg else "")
        lbl.setVisible(bool(msg))

    def _apply_prefill(self) -> None:
        if self._pending_prefill is None:
            return
        extra = self._form.set_values(self._pending_prefill)
        self._extra.setText(" ".join(shlex.quote(e) for e in extra) if extra else "")

    def _seed_from_params(self) -> None:
        """Prefill the (live-only) form from a tune step's stored {name: value}."""
        if not self._prefill_params:
            return
        args: List[str] = []
        specs = self._editor.param_cache().get(self._current_script, [])
        by_name = {s.get("name") or s.get("dest"): s for s in specs}
        for name, value in self._prefill_params.items():
            spec = by_name.get(name)
            flag = (spec.get("flags") or [None])[0] if spec else None
            if flag is not None:
                args += [flag, fmt_value(value)]
        if args:
            self._form.set_values(args)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _build_args(self) -> List[str]:
        args = self._form.build_args()
        raw = self._extra.text().strip()
        if raw:
            try:
                args = args + shlex.split(raw)
            except ValueError:
                args = args + raw.split()
        return args

    def _set_status(self, msg: str, error: bool = False) -> None:
        """Set the pinned status line. Errors show in the crash colour and bold so a
        failed OK (invalid values) is obvious instead of the button seeming to do nothing."""
        colour = Palette.CRASH if error else Palette.TEXT_FAINT
        weight = "600" if error else "400"
        self._params_status.setStyleSheet(
            f"font-size: 11px; color: {colour}; font-weight: {weight};")
        self._params_status.setText(msg)

    def _accept(self) -> None:
        task = self._task.currentText().strip()
        if not task:
            self._set_status("pick a task first", error=True)
            return
        err = self._form.validate()
        if err:
            self._set_status(err, error=True)
            return
        uid = self._src.uid
        mode = self._type.currentData()
        # The calibrated --power CONTROL QUANTITY the operator authored this step in (a
        # CAL_POWER_LAWS view id, or None), recorded so it is remembered on reopen and HELD from
        # this step forward. None when the form has no power card (uncalibrated / no views).
        pview = self._form.power_view() if self._form is not None else None
        if mode == "tune":
            params = self._form.values()
            if not params:
                self._set_status("set at least one live parameter to tune", error=True)
                return
            anchor = self._anchor.currentData()
            offset = round(self._run_off.value(), 1)
            sa = self._resolve_step_anchor(anchor, offset)
            if sa is False:
                return
            spans_getter = getattr(self._editor, "task_spans", None)
            # A window-B (Hold-anchored) tune OR a step-anchored tune is timed relative to
            # another event (resume / the target step), not a fixed on-air offset — so its
            # fixed-window fit can't be checked here.
            if spans_getter is not None and anchor not in ("hold", "step"):
                err = tlm.step_within_task_error(spans_getter(task), anchor, offset, kind="tune")
                if err:
                    self._set_status(err, error=True)
                    return
            self.result_item = tlm.RunItem(
                task_name=task, action="tune", params=params,
                anchor=anchor, offset=offset, uid=uid, power_view=pview,
                step_id=getattr(self._src, "step_id", "") or "", **sa)
        elif mode == "bar":
            self.result_item = tlm.BarItem(
                task_name=task, args=self._build_args(), replace_args=True,
                start_offset=round(self._start_off.value(), 1),
                stop_offset=round(self._stop_off.value(), 1),
                start_anchor=self._start_anchor.currentData() or "start",
                uid=uid, power_view=pview)
        else:
            anchor = self._anchor.currentData()
            offset = round(self._run_off.value(), 1)
            sa = self._resolve_step_anchor(anchor, offset)
            if sa is False:
                return
            self.result_item = tlm.RunItem(
                task_name=task, args=self._build_args(), replace_args=True,
                anchor=anchor, offset=offset, uid=uid, power_view=pview,
                step_id=getattr(self._src, "step_id", "") or "", **sa)
        self.accept()

    def _resolve_step_anchor(self, anchor: str, offset: float):
        """For a step-anchored point: validate + return {anchor_step_id, anchor_edge} (assigning
        the target a stable id). Returns {} for any non-step anchor, or False (after showing an
        error) when the step anchor is invalid — so _accept can bail."""
        if anchor != "step":
            return {}
        if offset < 0:
            self._set_status("offset must be ≥ 0 — a step can't fire before the one it "
                             "anchors to", error=True)
            return False
        tgt_uid = self._anchor_target.currentData()
        target = next((it for it in self._editor.items()
                       if getattr(it, "uid", None) == tgt_uid), None)
        if target is None:
            self._set_status("pick a step to anchor to", error=True)
            return False
        return {"anchor_step_id": tlm.ensure_step_id(target),
                "anchor_edge": self._anchor_edge.currentData() or "end"}

    def _disconnect(self) -> None:
        if self._editor._hub is None:
            return
        try:
            self._editor._hub.task_done.disconnect(self._on_params)
        except (TypeError, RuntimeError):
            pass


# ── The Hold marker editor (position only — the Hold carries no task work) ────

class HoldEditorDialog(QDialog):
    """Configure the Hold marker: only its position (window A's end offset, from
    ON-AIR). The Hold is a boundary — no task, no parameters — so this is a tiny
    dialog with an offset field and Remove. See docs/sequence-hold-step.md §6.1."""

    REMOVE = 2

    def __init__(self, item, editor: "TimelineEditor", new: bool, parent=None):
        super().__init__(parent)
        self._src = item
        self._editor = editor
        self._new = new
        self.result_item: Optional[object] = None
        self.setWindowTitle("Add Hold" if new else "Edit Hold")
        self.setMinimumWidth(420)
        self._build(item)

    def _build(self, item) -> None:
        from .dialog_style import editor_qss
        self.setStyleSheet(editor_qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        head = QLabel("Hold — pause here and await the operator")
        head.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(head)
        blurb = QLabel(
            "The run pauses at this point, holding the signal exactly, until you Proceed. "
            "Steps placed after the Hold (anchored to it) are scheduled only when you "
            "proceed. In the schedule the Hold is disabled and the run passes straight "
            "through it.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(blurb)

        form = QFormLayout()
        form.setSpacing(8)
        self._off = DurationSpinBox()
        self._off.setValue(float(getattr(item, "offset", 0.0)))
        self._off.setToolTip("Where the Hold sits, measured from ON-AIR (T0). Window-A "
                             "steps run up to here; window B is scheduled at proceed.")
        form.addRow("Pause at — from ON-AIR", self._off)
        outer.addLayout(form)

        buttons = QDialogButtonBox()
        if not self._new:
            remove = QPushButton("Remove")
            remove.setStyleSheet(f"color: {Palette.CRASH};")
            buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: self.done(self.REMOVE))
        ok_btn = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        ok_btn.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _accept(self) -> None:
        self.result_item = tlm.RunItem(
            task_name="", action="hold", anchor="start",
            offset=round(self._off.value(), 1), uid=self._src.uid)
        self.accept()


class _RowHeader(QWidget):
    """The Gantt-style row-header column, left of the canvas: one row per timeline item
    (a task's tunes/ramps indented beneath it in the task's hue), aligned to the canvas
    rows. Reads the canvas's row_layout() so it always tracks the same order/positions."""

    HDR_W = 210

    def __init__(self, canvas: "_TimelineCanvas"):
        super().__init__()
        self._canvas = canvas
        self.setFixedWidth(self.HDR_W)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.HDR_W, getattr(self._canvas, "_content_h", 200))

    def refresh(self) -> None:
        self.setMinimumHeight(getattr(self._canvas, "_content_h", 200))
        self.update()

    def _meta(self, it):
        """(name, sub, type_label) for a row header entry."""
        act = getattr(it, "action", "run")
        if getattr(it, "kind", None) == "bar":
            return it.task_name or "(no task)", "on-air → off-air", "Duration"
        if act == "ramp":
            r = dict(getattr(it, "ramp", None) or {})
            a, b = r.get("start"), r.get("stop")
            rng = f"{fmt_value(a)} → {fmt_value(b)}" if a is not None and b is not None else ""
            return f"{r.get('param') or 'param'} ramp", rng, "Ramp"
        if act == "tune":
            summ = ", ".join(f"{k}={v}" for k, v in (it.params or {}).items())
            return f"{it.task_name} tune", summ, "Tune"
        return it.task_name or "(no task)", "one-shot", "One-shot"

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(Palette.SURFACE))
        p.setPen(QPen(QColor(Palette.HAIRLINE if hasattr(Palette, "HAIRLINE") else Palette.BORDER), 1))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        # caption
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(10); f.setBold(True)
        p.setFont(f); p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(16, 6, self.width() - 24, 12,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "TASKS & STEPS")
        for row in self._canvas.row_layout():
            it, y, hue, child = row["item"], row["y"], row["hue"], row["child"]
            known = self._canvas.task_known(getattr(it, "task_name", ""))
            base = QColor(hue) if (hue and known) else QColor(Palette.CRASH)
            cy = y + LANE_H / 2
            name, sub, typ = self._meta(it)
            if child:
                # indent + a small kin elbow in the task hue
                kin = QColor(base); kin.setAlpha(120)
                p.setPen(QPen(kin, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
                path = QPainterPath(); path.moveTo(22, y - 6); path.lineTo(22, cy)
                path.lineTo(30, cy)
                p.drawPath(path)
                nx = 34
            else:
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(base)
                p.drawRoundedRect(QRectF(14, cy - 4.5, 9, 9), 2.5, 2.5)
                nx = 30
            # name (top) + sub (bottom), or centred if no sub
            fn = QFont(Fonts.SANS.split(",")[0].strip('"')); fn.setPixelSize(12)
            fn.setWeight(QFont.Weight(600 if not child else 500))
            fm = QFontMetrics(fn)
            badge_w = self._type_badge(p, typ, base, known, y)   # draws + returns width
            avail = self.width() - nx - badge_w - 16
            if sub:
                p.setFont(fn); p.setPen(QColor(Palette.TEXT if known else Palette.CRASH))
                p.drawText(nx, int(y + 3), avail, 15,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           fm.elidedText(name, Qt.TextElideMode.ElideRight, avail))
                fs = QFont(Fonts.SANS.split(",")[0].strip('"')); fs.setPixelSize(10)
                p.setFont(fs); p.setPen(QColor(Palette.TEXT_FAINT))
                fms = QFontMetrics(fs)
                p.drawText(nx, int(y + LANE_H - 14), avail, 12,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           fms.elidedText(sub, Qt.TextElideMode.ElideRight, avail))
            else:
                p.setFont(fn); p.setPen(QColor(Palette.TEXT if known else Palette.CRASH))
                p.drawText(nx, int(y), avail, LANE_H,
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                           fm.elidedText(name, Qt.TextElideMode.ElideRight, avail))
        p.end()

    def _type_badge(self, p, text, base, known, y) -> int:
        f = QFont(Fonts.SANS.split(",")[0].strip('"')); f.setPixelSize(8); f.setBold(True)
        p.setFont(f); fm = QFontMetrics(f)
        w = fm.horizontalAdvance(text) + 10
        r = QRectF(self.width() - w - 12, y + (LANE_H - 14) / 2, w, 14)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(Palette.INSET))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor(Palette.TEXT_MUTED))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
        return w + 12


# ── Public editor: toolbar + scrollable canvas ────────────────────────────────

class TimelineEditor(QWidget):
    """Toolbar (add buttons) above a horizontally-scrollable timeline canvas."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: List[str] = []
        # Injected by the host dialog so the step editor can fetch parameters and
        # derive a task's script + default args.
        self._hub = None
        self._hostname = ""
        self._task_commands: Dict[str, List[str]] = {}
        self._param_specs: Dict[str, list] = {}
        self._script_cal_signals: Dict[str, str] = {}   # script -> its declared CAL_SIGNAL_ID
        self._script_cal_freq_params: Dict[str, str] = {}  # script -> its CAL_FREQ_PARAM
        self._script_power_laws: Dict[str, list] = {}   # script -> its CAL_POWER_LAWS (companions)
        self._params_inflight: set = set()
        # Calibration context: params come from the library (same across units), but
        # absolute-power bounds are per-UNIT, so the calibration host is tracked
        # separately (the unit a plan will arm on / the unit whose sequences tab this
        # is). _task_signals maps a task → its SDR_CAL_SIGNAL_ID (from tasks.yaml env).
        self._cal_hostname = ""
        self._calibration = None                 # the unit's GET /calibration result
        self._cal_stale = False                  # True when served from the offline cache
        self._task_signals: Dict[str, str] = {}
        self._cal_connected = False
        # Proactive params prefetch (for the achievability banner): fetch a sequence task's
        # script params even when no step/ramp dialog has been opened for it, so the banner can
        # appear on load. Kept separate from the dialog's _params_inflight/labels so the two
        # never cross-route a result.
        self._prefetch_inflight: set = set()
        self._prefetch_connected = False
        self._hold_authoring = True              # '+ Hold' shown (off in the plan editor)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        _chip = (f"QPushButton {{ border:1px solid {Palette.BORDER}; border-radius:999px; "
                 f"padding:5px 12px; background:{Palette.SURFACE}; color:{Palette.TEXT_MUTED}; "
                 f"font-weight:600; font-size:12px; }} "
                 f"QPushButton:hover {{ color:{Palette.TEXT}; border-color:{Palette.BORDER_STRONG}; }}")
        bar = QHBoxLayout()
        self._add_bar = QPushButton("Duration")
        self._add_bar.setToolTip("A task that runs across the on-air window (start + stop)")
        self._add_run = QPushButton("One-shot")
        self._add_run.setToolTip("A task that fires once and exits (many allowed)")
        self._add_tune = QPushButton("Tune")
        self._add_tune.setToolTip("Change a running duration task's live parameters at a set time")
        self._add_ramp = QPushButton("Ramp")
        self._add_ramp.setToolTip("Sweep a running duration task's live parameter over time")
        self._add_hold = QPushButton("Hold")
        self._add_hold.setToolTip("Pause the run here and await the operator (the Hold step); "
                                  "post-hold steps anchor to it. Library / operator-present runs "
                                  "only — the schedule runs straight through it.")
        self._add_bar.clicked.connect(lambda: self._canvas.add_new("bar"))
        self._add_run.clicked.connect(lambda: self._canvas.add_new("run"))
        self._add_tune.clicked.connect(lambda: self._canvas.add_new("tune"))
        self._add_ramp.clicked.connect(lambda: self._canvas.add_new("ramp"))
        self._add_hold.clicked.connect(lambda: self._canvas.add_new("hold"))
        for b, kind in ((self._add_bar, "bar"), (self._add_run, "run"), (self._add_tune, "tune"),
                        (self._add_ramp, "ramp"), (self._add_hold, "hold")):
            b.setStyleSheet(_chip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIcon(_tool_icon(kind)); b.setIconSize(QSize(15, 15))
            bar.addWidget(b)
        bar.addStretch(1)
        # Minimum on-air duration the current steps require (ramps at both ends etc).
        self._mindur = QLabel("")
        self._mindur.setStyleSheet(f"font-size: 11px; color: {Palette.ACCENT};")
        bar.addWidget(self._mindur)
        self._hint = QLabel("Drag a bar to move it · click to select, double-click to edit")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        # Undo / redo.
        self._undo_btn = QPushButton("Undo")
        self._undo_btn.setToolTip("Undo (Ctrl+Z)")
        self._redo_btn = QPushButton("Redo")
        self._redo_btn.setToolTip("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        self._undo_btn.clicked.connect(lambda: self._canvas.undo())
        self._redo_btn.clicked.connect(lambda: self._canvas.redo())
        for b in (self._undo_btn, self._redo_btn):
            b.setStyleSheet(_chip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setEnabled(False)
            bar.addWidget(b)
        # Fit-to-view + zoom readout.
        self._fit_btn = QPushButton("Fit")
        self._fit_btn.setStyleSheet(_chip)
        self._fit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fit_btn.setToolTip("Fit the whole sequence to the view")
        self._fit_btn.clicked.connect(self._fit)
        bar.addWidget(self._fit_btn)
        # Segmented zoom control: [ − | 100% | + ].
        zoomw = QFrame(); zoomw.setObjectName("zoomseg")
        zoomw.setStyleSheet(
            f"#zoomseg {{ border:1px solid {Palette.BORDER}; border-radius:999px; "
            f"background:{Palette.SURFACE}; }} "
            f"#zoomseg QPushButton {{ border:none; background:transparent; "
            f"color:{Palette.TEXT_MUTED}; padding:2px 10px; font-size:15px; }} "
            f"#zoomseg QPushButton:hover {{ color:{Palette.TEXT}; }} "
            f"#zoomseg QLabel {{ color:{Palette.TEXT_MUTED}; font-size:11px; "
            f"border-left:1px solid {Palette.BORDER}; border-right:1px solid {Palette.BORDER}; "
            f"padding:2px 6px; }}")
        zh = QHBoxLayout(zoomw); zh.setContentsMargins(0, 0, 0, 0); zh.setSpacing(0)
        zo = QPushButton("−"); zi = QPushButton("+")
        zo.setCursor(Qt.CursorShape.PointingHandCursor); zi.setCursor(Qt.CursorShape.PointingHandCursor)
        zo.clicked.connect(lambda: self._canvas._apply_zoom(1 / 1.15, self._canvas._viewport_center_x()))
        zi.clicked.connect(lambda: self._canvas._apply_zoom(1.15, self._canvas._viewport_center_x()))
        self._zoom_btn = QLabel("100%")
        self._zoom_btn.setToolTip("Horizontal zoom — Ctrl+scroll or pinch")
        self._zoom_btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_btn.setFixedWidth(44)
        zh.addWidget(zo); zh.addWidget(self._zoom_btn); zh.addWidget(zi)
        bar.addWidget(zoomw)
        outer.addLayout(bar)

        # Sequence-level POWER ACHIEVABILITY warning (warn, never block): a ramp point that the
        # unit can't deliver at the frequency/params in effect when it fires — e.g. a power ramp
        # whose top steps clamp after a LATER tune retunes the carrier. Refreshed on every edit.
        self._achv_warn = QLabel("")
        self._achv_warn.setWordWrap(True)
        self._achv_warn.setVisible(False)
        self._achv_warn.setStyleSheet(
            f"font-size: 11px; color: {Palette.ARMED}; font-weight: 600; "
            f"background: {Palette.ARMED_SOFT}; border: 1px solid {Palette.ARMED}; "
            f"border-radius: 8px; padding: 7px 10px;")
        outer.addWidget(self._achv_warn)

        # Offline-calibration banner: the target unit is offline, so absolute power is folded from
        # its last-known (cached) calibration. Informational (accent, not alarming) — distinct from
        # the amber achievability clamp warning above. Refreshed whenever calibration resolves.
        self._cal_stale_banner = QLabel("")
        self._cal_stale_banner.setWordWrap(True)
        self._cal_stale_banner.setVisible(False)
        self._cal_stale_banner.setStyleSheet(
            f"font-size: 11px; color: {Palette.ACCENT_INK}; background: {Palette.ACCENT_SOFT}; "
            f"border: 1px solid #cfe0ee; border-radius: 8px; padding: 7px 10px;")
        outer.addWidget(self._cal_stale_banner)

        self._canvas = _TimelineCanvas(self)
        self._canvas.changed.connect(self.changed.emit)
        self._canvas.changed.connect(self._update_mindur)
        self._canvas.changed.connect(self._update_achievability)
        self._canvas.changed.connect(self._sync_hold_button)
        self._canvas.changed.connect(self._sync_undo_buttons)
        self._sync_hold_button()
        self._sync_undo_buttons()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)   # canvas stretches to fill a wider window
        scroll.setWidget(self._canvas)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(240)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background: {Palette.SURFACE}; border: none; }}")
        self._canvas.set_scroll_area(scroll)

        # Task-colour legend + the parent-inheritance caption (the mockup's row).
        self._legend = _Legend(self._canvas)
        self._canvas.changed.connect(self._legend.update)
        outer.addWidget(self._legend)

        # Gantt-style row-header column, left of the canvas; both wrapped in one bordered
        # "stage" frame so they read as a single panel (the mockup layout).
        self._rowhdr = _RowHeader(self._canvas)
        self._canvas.changed.connect(self._rowhdr.refresh)
        stage = QFrame(); stage.setObjectName("tlStage")
        stage.setStyleSheet(
            f"#tlStage {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 10px; }}")
        srow = QHBoxLayout(stage); srow.setContentsMargins(0, 0, 0, 0); srow.setSpacing(0)
        srow.addWidget(self._rowhdr)
        srow.addWidget(scroll, stretch=1)
        outer.addWidget(stage, stretch=1)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        # Open framed to the whole sequence (like the mockup), once, after layout settles.
        if not getattr(self, "_did_autofit", False) and self._canvas.items():
            self._did_autofit = True
            QTimer.singleShot(0, self._fit)

    def _sync_zoom(self) -> None:
        self._zoom_btn.setText(f"{round(self._canvas._zoom * 100)}%")

    def _fit(self) -> None:
        """Zoom so the whole sequence fits the viewport width."""
        c = self._canvas
        vp = c._scroll.viewport().width() if c._scroll is not None else self.width()
        nat = c._content_w / max(c._zoom, 1e-6)     # content width at zoom 1
        if nat <= 0 or vp <= 0:
            return
        z = max(ZOOM_MIN, min(1.5, (vp - 6) / nat))
        if abs(z - c._zoom) > 1e-4:
            c._zoom = z
            c.relayout()
            self._sync_zoom()

    def set_hold_authoring(self, enabled: bool) -> None:
        """Show/hide the '+ Hold' button. Hidden on surfaces where a Hold has no
        effect (the plan editor: a plan's Hold is compiled out for the schedule)."""
        self._hold_authoring = bool(enabled)
        self._sync_hold_button()

    def _sync_undo_buttons(self) -> None:
        cv = getattr(self, "_canvas", None)
        if cv is None:
            return
        if getattr(self, "_undo_btn", None) is not None:
            self._undo_btn.setEnabled(cv.can_undo())
        if getattr(self, "_redo_btn", None) is not None:
            self._redo_btn.setEnabled(cv.can_redo())

    def _sync_hold_button(self) -> None:
        # One Hold per sequence: once one exists, disable '+ Hold' (edit/remove the
        # existing marker instead). Hidden entirely where hold authoring is off.
        btn = getattr(self, "_add_hold", None)
        if btn is None:
            return
        btn.setVisible(getattr(self, "_hold_authoring", True))
        has = tlm.has_hold(self._canvas.items()) if getattr(self, "_canvas", None) else False
        btn.setEnabled(not has)
        btn.setToolTip(
            "A sequence can have only one Hold — edit or remove the existing one." if has else
            "Pause the run here and await the operator (the Hold step); post-hold steps "
            "anchor to it. Library / operator-present runs only — the schedule runs "
            "straight through it.")

    # ── Context injected by the host dialog ──────────────────────────────────

    def set_context(self, hub, hostname: str) -> None:
        self._hub = hub
        self._hostname = hostname

    def set_task_commands(self, mapping: Dict[str, List[str]]) -> None:
        self._task_commands = dict(mapping)

    def set_task_signals(self, mapping: Dict[str, str]) -> None:
        """task_name -> SDR_CAL_SIGNAL_ID (the task's calibration opt-in signal)."""
        self._task_signals = dict(mapping)

    def set_calibration(self, hub, hostname: str) -> None:
        """Point the step editor at the UNIT whose calibration governs absolute power
        (the plan's target unit, or the sequences-tab unit). Empty hostname — or the
        reserved LIBRARY_HOST, which is offline Library authoring, not a real unit — means
        no unit is targeted, so absolute power is free-form (see _compute_power_modes).
        Fetches GET /calibration once and caches it; step forms read it via
        cal_bounds_for_task()."""
        if hostname == LIBRARY_HOST:
            hostname = ""                            # the library isn't a real unit
        self._cal_hostname = hostname or ""
        self._calibration = None
        self._cal_stale = False
        self._update_cal_stale_banner()              # clear any prior unit's stale banner
        if not hostname or hub is None:
            return
        if not self._cal_connected:
            hub.task_done.connect(self._on_cal_result)
            self._cal_connected = True
        hub.run_async(f"tl_cal:{hostname}",
                      lambda h=hostname: hub.fleet.get(h).get_calibration())

    def _on_cal_result(self, label: str, result) -> None:
        from api.client import AgentHTTPError
        from state.calibration_cache import get_calibration_cache
        if not isinstance(label, str) or not label.startswith("tl_cal:"):
            return
        host = label.split(":", 1)[1]
        if host != self._cal_hostname:
            return
        cache = get_calibration_cache()
        if isinstance(result, dict):
            if result.get("valid"):
                self._calibration = result
                self._cal_stale = False
                cache.put(host, result)                  # remember for offline authoring
            else:
                # Reachable but uncalibrated / invalid — no absolute; don't use the cache.
                self._calibration = None
                self._cal_stale = False
        elif isinstance(result, AgentHTTPError):
            # Reachable, but the agent returned an error (404 = no calibration document) —
            # a real "uncalibrated" verdict, so do NOT serve stale cached bounds.
            self._calibration = None
            self._cal_stale = False
        else:
            # Every other failure means we couldn't reach the unit to ask: it's offline
            # (AgentConnectionError), it was never discovered this session so it isn't in the
            # fleet (KeyError from Fleet.get), a timeout, etc. Fall back to the last-known
            # calibration we cached for THIS unit, marked stale, so a plan/sequence can still
            # author absolute power for it. Refreshed the moment the unit is reachable again.
            self._calibration = cache.get(host)
            self._cal_stale = self._calibration is not None
        self._update_cal_stale_banner()   # show/hide the "using cached calibration" notice
        self._update_achievability()      # bounds just arrived — surface any ramp clamps now

    def _update_cal_stale_banner(self) -> None:
        """Show the offline-calibration banner when absolute power is folded from the last-known
        (cached) calibration because the target unit is offline; hide it once a live fetch lands."""
        banner = getattr(self, "_cal_stale_banner", None)
        if banner is None:
            return
        stale = bool(self._cal_stale and self._calibration and self._cal_hostname)
        if stale:
            host = self._cal_hostname or "the target unit"
            when = ""
            try:
                from state.calibration_cache import get_calibration_cache
                ts = get_calibration_cache().fetched_at(host)
                when = f", last seen {ts.replace('T', ' ').rstrip('Z')}" if ts else ""
            except Exception:                          # noqa: BLE001
                when = ""
            banner.setText(
                f"⚠ {host} is offline — absolute power uses its last-known calibration{when}. "
                f"It refreshes automatically when the unit reconnects.")
        banner.setVisible(stale)

    def absolute_allowed(self) -> bool:
        """Absolute (calibrated dBm) power is offered only when a unit is targeted."""
        return bool(self._cal_hostname)

    def has_hold(self) -> bool:
        """Whether the timeline currently carries a Hold marker — gates the step
        editor's 'Hold' anchor option (window-B steps anchor to the Hold)."""
        return tlm.has_hold(self._canvas.items())

    def cal_is_stale(self) -> bool:
        """True when the bounds in use came from the offline cache, not a live fetch."""
        return self._cal_stale

    def cal_bounds_for_task(self, task: str):
        """Resolved --power bounds for a task's signal on the target unit, or None."""
        if not self._calibration:
            return None
        sid = self._task_signals.get(task)
        if not sid:
            return None
        return (self._calibration.get("signals") or {}).get(sid)

    def power_hint_for_task(self, task: str):
        """A soft achievable-range hint for a task's signal, aggregated across every unit
        seen before — used when authoring absolute power in the Library, where no single
        unit is targeted. None when no cached unit resolves the signal."""
        sid = self._task_signals.get(task)
        if not sid:
            return None
        from state.calibration_cache import get_calibration_cache
        return get_calibration_cache().aggregate_power_bounds(sid)

    def has_cal_signal(self, task: str) -> bool:
        """Whether a task opts into calibration (sets SDR_CAL_SIGNAL_ID). When it doesn't,
        its power/gain are raw — no calibration limits apply on any unit."""
        return bool(self._task_signals.get(task))

    def script_calibratable(self, task: str) -> bool:
        """Whether a task's SCRIPT declares a calibration signal — i.e. its power/gain is
        MEANT to be calibrated (so a missing task signal is a real gap worth flagging). A
        script that declares none takes raw power/gain by design. Unknown (params not
        fetched yet) is treated as calibratable, so we don't hide a real gap on a race."""
        script, _ = self.script_for_task(task)
        if script not in self._script_cal_signals:
            return True
        return bool(self._script_cal_signals.get(script))

    def script_for_task(self, task: str) -> Tuple[str, List[str]]:
        """(script_filename, default_args) for a task name — for the step editor."""
        cmd = self._task_commands.get(task)
        if not cmd:
            return "", []
        return tlm.script_of_command(cmd)

    def param_cache(self) -> Dict[str, list]:
        return self._param_specs

    def cache_script_meta(self, script: str, result) -> None:
        """Populate ALL per-script caches from one get_script_params result, so whichever dialog
        (step or ramp) fetches a script FIRST leaves the caches COMPLETE — the params, the
        calibration signal, the fold frequency AND the power-quantity laws are all available to
        every later dialog that finds the param cache warm. Keep this the single writer: the ramp
        editor used to populate only a subset, so a step editor opened after it saw no power laws
        and silently dropped the multi-quantity --power card (companions + 'Control in this →')."""
        r = result or {}
        self._param_specs[script] = r.get("params", [])
        self._script_cal_signals[script] = r.get("calibration_signal")
        self._script_cal_freq_params[script] = r.get("calibration_freq_param")
        self._script_power_laws[script] = r.get("calibration_power_laws", []) or []

    # ── Task list (populated once the unit's tasks are fetched) ──────────────

    def set_tasks(self, names: List[str]) -> None:
        self._tasks = list(names)
        if not self._tasks:
            self._hint.setText("no tasks on this unit — define one in the Tasks tab first")
        else:
            self._hint.setText("Drag an edge dot to another step to anchor · click to select, "
                               "double-click to edit · right-click for more · Ctrl+Z undo")
        self._canvas.relayout()

    def available_tasks(self) -> List[str]:
        return self._tasks

    def sequence_task_names(self) -> List[str]:
        """Tasks started as duration bars in the current sequence — the only tasks a
        tune or ramp step can target, since those act on an already-running task."""
        seen: List[str] = []
        for it in self._canvas.items():
            if getattr(it, "kind", None) == "bar" and it.task_name and it.task_name not in seen:
                seen.append(it.task_name)
        return seen

    def task_spans(self, task_name: str) -> List[tuple]:
        """(start_offset, stop_offset) of each duration bar for a task — the on-air
        span(s) a tune/ramp step on that task must fall within."""
        return [(it.start_offset, it.stop_offset)
                for it in self._canvas.items()
                if getattr(it, "kind", None) == "bar" and it.task_name == task_name]

    def min_on_air_duration(self) -> float:
        return tlm.min_on_air_duration(self._canvas.items())

    def _update_mindur(self) -> None:
        d = self.min_on_air_duration()
        self._mindur.setText(f"min on-air {fmt_duration(d)}" if d > 0 else "")

    def _achievability_resolver(self):
        """Build the ``resolve(task)`` callback ``tlm.achievability_warnings`` needs — the
        per-task calibration context, from what the editor already has cached. Returns None for a
        task with no targeted-unit calibration, no fetched params, or no --power field (so that
        task is simply skipped). Kept here (not in the pure model) so the model stays cal-agnostic."""
        from .param_form import find_power_index, hz_per_unit

        def resolve(task: str):
            bounds = self.cal_bounds_for_task(task)
            artifact = (bounds or {}).get("artifact")
            if not artifact:
                return None
            script, base_args = self.script_for_task(task)
            specs = self.param_cache().get(script, [])
            pidx = find_power_index(specs)
            if pidx is None:
                return None
            freq_param = self._script_cal_freq_params.get(script)
            freq_unit = next((s.get("unit") for s in specs if s.get("dest") == freq_param), None)
            power_laws = self._script_power_laws.get(script) or []
            view_laws = {spec.get("id"): spec for spec in power_laws
                         if isinstance(spec, dict) and spec.get("id")}
            return {"artifact": artifact, "specs": specs, "base_args": base_args,
                    "freq_param": freq_param, "freq_factor": hz_per_unit(freq_unit),
                    "power_dest": specs[pidx]["dest"],
                    # The default controlled view (a step recording no power_view), plus every
                    # declared view by id so the walk can hold whichever a step was authored in
                    # (latest-set-wins); the walk itself keeps only the bw-keyed ones.
                    "view_law": self._controlled_view_law(artifact, script),
                    "view_laws": view_laws}

        return resolve

    def _controlled_view_law(self, artifact: dict, script: str):
        """The CAL_POWER_LAWS entry the operator authors --power in when the raw measured quantity
        is dropped from the control picker — the leading ``restates_measurement`` law (a chirp's
        live spectral density, keyed on --bw), or None. Mirrors ParamForm._power_views' drop-base
        rule: a declared REPORTED reading is the operator's chosen axis and is never dropped, so a
        restatement law only stands in when the reported reading is the measured base itself. The
        temporal walk folds the achievable range in this view at each event's fire-time param, so a
        held/commanded live density is checked at the live sweep width (the base range can't see it)."""
        rep = ((artifact or {}).get("readings") or {}).get("reported") or {}
        if rep.get("kind") == "law":            # a declared reported axis — not a restatement target
            return None
        for spec in (self._script_power_laws.get(script) or []):
            if isinstance(spec, dict) and spec.get("restates_measurement"):
                return spec
        return None

    def _pill_power_display(self, item) -> Dict[str, str]:
        """Override text for a tune step's canvas pill: when the step controls --power in a non-base
        view, the pill shows the quantity the operator SET (base + view_delta) with its unit — not the
        raw base it is sent in. Covers a bw-KEYED view (a chirp's live density → base + view_delta at
        the carried bw) AND a CONSTANT-offset view (full-bandwidth total power → base + a fixed delta,
        no bridge param). Returns ``{power_dest: 'value unit'}`` to replace that param's pill value, or
        ``{}`` to show the raw params. Best-effort — any gap (no view, params not cached, unresolvable
        carried bw) falls back to the raw base, so the label helper never breaks the canvas."""
        pv = getattr(item, "power_view", None)
        params = getattr(item, "params", None) or {}
        task = getattr(item, "task_name", None)
        if not pv or not task or not params:
            return {}
        try:
            resolve = self._achievability_resolver()
            info = resolve(task) if resolve else None
            if not info:
                return {}
            power_dest = info.get("power_dest")
            base = params.get(power_dest)
            if not isinstance(base, (int, float)) or isinstance(base, bool):
                return {}
            spec = (info.get("view_laws") or {}).get(pv)
            law = tlm._view_law_of(spec)
            if law is None:
                return {}
            # A bw-keyed view (density) folds its delta through the carried bridge params; a
            # constant-offset view (total power) has none and uses the law's own (representative)
            # delta — either way the pill shows what the operator set, not the base.
            if law.params():
                specs = info.get("specs") or []
                _items = self._canvas.items()
                _hoff = tlm.hold_offset(_items)
                carried = tlm.sequence_effective_values(
                    self._canvas.items(), task, info.get("base_args") or [], specs,
                    getattr(item, "uid", None),
                    target_key=tlm._carry_order_key(
                        item, _hoff, tlm.resolve_step_offsets(_items, _hoff)))
                from state.power_fold import resolve_keyed_values
                keyed = resolve_keyed_values(specs, carried, law.params())
                delta = law.delta_db(keyed) if keyed else law.rep_delta_db()
            else:
                delta = law.rep_delta_db()
            unit = str(spec.get("unit") or "").strip()
            return {power_dest: f"{base + delta:.2f}{(' ' + unit) if unit else ''}"}
        except Exception:                          # noqa: BLE001 — a label helper must never break
            return {}

    def _update_achievability(self) -> None:
        """Refresh the sequence-level power-achievability warning. Best-effort: a task whose params
        aren't cached yet is skipped, and a proactive prefetch is kicked off so it's picked up as
        soon as the params land (no need to open a step/ramp dialog first). Never raises — a warning
        helper must not break the editor."""
        try:
            issues = tlm.achievability_warnings(self._canvas.items(), self._achievability_resolver())
        except Exception:                          # noqa: BLE001
            issues = []
        self._achv_warn.setText("\n".join(i.message for i in issues))
        self._achv_warn.setVisible(bool(issues))
        try:
            self._prefetch_seq_params()            # fill any missing params → banner appears on load
        except Exception:                          # noqa: BLE001
            pass

    def _prefetch_seq_params(self) -> None:
        """Fetch script params for every sequence task whose params aren't cached yet, so the
        achievability banner can surface WITHOUT the operator first opening a step/ramp dialog for
        that task (the top follow-up in docs/sequence-power-achievability.md §9). Demand-driven —
        called from _update_achievability, so only uncached scripts fire, once each; the result
        lands in _on_prefetch_params, which caches it and re-runs the banner. Best-effort: no hub,
        no targeted unit, or an unresolvable host just leaves the task skipped."""
        if self._hub is None or not self._hostname:
            return
        fleet = getattr(self._hub, "fleet", None)
        if fleet is None or fleet.get(self._hostname) is None:
            return
        cache = self.param_cache()
        seen: List[str] = []
        for it in self._canvas.items():
            task = getattr(it, "task_name", None)
            if not task or task in seen:
                continue
            seen.append(task)
            script, _ = self.script_for_task(task)
            if not script or script in cache or script in self._prefetch_inflight:
                continue
            if not self._prefetch_connected:
                self._hub.task_done.connect(self._on_prefetch_params)
                self._prefetch_connected = True
            self._prefetch_inflight.add(script)
            self._hub.run_async(
                f"tl_prefetch:{self._hostname}:{script}",
                lambda s=script: self._hub.fleet.get(self._hostname).get_script_params(s))

    def _on_prefetch_params(self, label: str, result) -> None:
        """Cache a prefetched script's params and refresh the achievability banner. Routed by a
        distinct ``tl_prefetch:`` label so it never collides with the step dialog's own fetch."""
        if not isinstance(label, str) or not label.startswith("tl_prefetch:"):
            return
        parts = label.split(":", 2)
        if len(parts) < 3 or parts[1] != self._hostname:
            return
        script = parts[2]
        self._prefetch_inflight.discard(script)
        if isinstance(result, Exception):
            return                                 # best-effort — leave the task uncached
        self.cache_script_meta(script, result)
        self._update_achievability()               # params arrived — surface any clamps now

    # ── Add / load / read steps ──────────────────────────────────────────────

    def items(self) -> List:
        """The raw timeline objects (BarItem / RunItem) currently on the canvas — used by
        the step and ramp editors to carry parameter state (the effective --freq) forward
        along a task's steps, so the --power range folds at the frequency actually in effect."""
        return self._canvas.items()

    def set_steps(self, steps: List[m.SequenceStep]) -> None:
        dicts = []
        for s in steps:
            ramp = getattr(s, "ramp", None)
            end = getattr(s, "offset_end_s", None)
            params = dict(getattr(s, "params", {}) or {})
            # Strip a --power the hold precompute INJECTED (power_hold_dest) so the authored --bw
            # step loads clean (edited as a --bw step); steps() re-derives it fresh on the next save.
            hold_dest = getattr(s, "power_hold_dest", None)
            if hold_dest:
                params.pop(hold_dest, None)
            dicts.append({
                "anchor": s.anchor, "offset_s": float(s.offset_s),
                "offset_end_s": float(end) if end is not None else 0.0,
                "action": s.action.value if hasattr(s.action, "value") else str(s.action),
                "task_name": s.task_name, "args": list(getattr(s, "args", []) or []),
                "replace_args": bool(getattr(s, "replace_args", False)),
                "params": params,
                "ramp": (ramp.model_dump() if hasattr(ramp, "model_dump")
                         else dict(ramp)) if ramp else None,
                "power_view": getattr(s, "power_view", None),
                # Step-to-step anchoring: carry the stable id + the anchor target/edge so a
                # step-anchored step survives load (else it reloads anchor="step" with no target
                # and the editor falls back to the wrong step).
                "id": getattr(s, "id", "") or "",
                "anchor_step_id": getattr(s, "anchor_step_id", "") or "",
                "anchor_edge": getattr(s, "anchor_edge", "end") or "end",
                # power_hold_dest is deliberately NOT carried onto the canvas item — the injected
                # --power was just stripped, so the authored item is clean and re-derived on save.
            })
        self._canvas.set_items(tlm.steps_to_items(dicts))

    def steps(self) -> List[m.SequenceStep]:
        # Hold the latest-set control quantity across --bw changes: inject the base --power the unit
        # needs at each operating point so a live density stays constant (client precompute — the
        # Run/Tune form's re-send, baked onto the DEPLOYED steps). The canvas items are untouched
        # (authoring stays clean); set_steps strips the injected --power on the next load.
        try:
            held_items = tlm.hold_control_quantity(
                self._canvas.items(), self._achievability_resolver())
        except Exception:                          # noqa: BLE001 — never break the save
            held_items = self._canvas.items()
        out: List[m.SequenceStep] = []
        for d in tlm.items_to_steps(held_items):
            # Each action carries different payload (args / params / ramp); use .get
            # so a missing key never crashes the save.
            ramp = d.get("ramp")
            out.append(m.SequenceStep(
                anchor=d["anchor"], offset_s=d["offset_s"],
                offset_end_s=d.get("offset_end_s"),
                action=m.StepAction(d["action"]), task_name=d["task_name"],
                args=list(d.get("args") or []),
                replace_args=bool(d.get("replace_args", False)),
                params=dict(d.get("params") or {}),
                ramp=m.RampSpec(**ramp) if ramp else None,
                power_view=d.get("power_view"),
                power_hold_dest=d.get("power_hold_dest"),
                # Step-to-step anchoring: preserve the stable id + target/edge onto the wire step
                # (items_to_steps emits them only when present, so a plain step stays unchanged).
                id=d.get("id", "") or "",
                anchor_step_id=d.get("anchor_step_id", "") or "",
                anchor_edge=d.get("anchor_edge", "end") or "end"))
        return out

    # ── Validation (mirrors the agent's _validate_steps) ─────────────────────

    def validate(self) -> Optional[str]:
        return tlm.validate(self._canvas.items(), self._tasks or None)
