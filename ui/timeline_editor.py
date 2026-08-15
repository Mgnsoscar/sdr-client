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

import shlex
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from . import timeline_model as tlm
from api import ramp as _ramp

from .duration_spin import DurationSpinBox
from .param_form import ParamForm, fmt_duration, fmt_value
from .ramp_editor import RampEditorDialog
from .theme import Palette

# ── View geometry (paint sizes; timing geometry lives in timeline_model) ──────
LANES_TOP = 34              # y of the first lane
LANE_H = 34                 # bar / pill (name row) height
LANE_VGAP = 12              # vertical gap between lanes
CAPTION_H = 17              # the offset-timing chip row under a name
AXIS_GAP = 14               # min gap between the lowest task and the time axis
BASELINE_FROM_BOTTOM = 50   # baseline sits this far above the canvas bottom
HANDLE_W = 12               # drawn width of a bar's grip
HANDLE_HIT = 11             # px each side of a handle centre that grabs it
RUN_MIN_W = 120             # minimum run-pill width
RUN_MAX_W = 260
RAMP_MIN_W = 44             # minimum ramp-bar width (so a short/zero-span ramp is clickable)
CARET_W = 20                # width of the ▾/▴ expand-collapse zone on an item
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

DRAG_PARTS = ("bar_start", "bar_stop", "bar_body", "run_body")


def _fmt_offset(offset_s: float) -> str:
    """'-2 min', '+5 s', '0 s' — signed, split into min/h past each threshold."""
    return fmt_duration(offset_s, signed=True)


def _ramp_summary(spec, anchor: str) -> str:
    """A compact 'param start→stop · 60s' (or '· fills window') label for a ramp."""
    spec = spec or {}
    param = spec.get("param") or "(param)"
    a, b = spec.get("start"), spec.get("stop")
    span = f"{fmt_value(a)}→{fmt_value(b)}" if a is not None and b is not None else "…"
    if anchor == "both":
        return f"{param} {span} · fills window"
    try:
        res = _ramp.resolve_ramp(a, b, steps=spec.get("steps"), step=spec.get("step"), hold_s=spec.get("hold_s"),
                                 duration_s=spec.get("duration_s"))
        return f"{param} {span} · {fmt_duration(res.duration_s)}"
    except (ValueError, TypeError):
        return f"{param} {span}"


def _timing_text(offset_s: float, side: str, with_side: bool) -> str:
    """Readable timing label for a chip. Exactly on the anchor reads as the anchor
    name ('on-air'/'off-air') rather than an ambiguous '0s'; otherwise the signed
    offset, optionally with the side it's measured from."""
    label = "on-air" if side == "start" else "off-air"
    if offset_s == 0:
        return label
    return f"{_fmt_offset(offset_s)} · {label}" if with_side else _fmt_offset(offset_s)


def _arg_pairs(args: List[str]) -> List[Tuple[str, Optional[str]]]:
    """Group a flat CLI arg list into (flag, value) rows for orderly display.
    A flag with no following value (a boolean switch) gets value None; a bare
    positional gets an empty flag."""
    pairs: List[Tuple[str, Optional[str]]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                pairs.append((a, args[i + 1])); i += 2
            else:
                pairs.append((a, None)); i += 1
        else:
            pairs.append(("", a)); i += 1
    return pairs


# ── The canvas: paints bars + pills and handles all dragging / hit-testing ────

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
        self._baseline = self._content_h - BASELINE_FROM_BOTTOM
        self._zoom = 1.0                 # horizontal (time-axis) zoom factor
        self._scroll = None              # host QScrollArea, for zoom-to-cursor
        self.setMouseTracking(True)
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
        self._items = list(items)
        self.relayout()
        self.changed.emit()

    def add_item(self, item) -> None:
        self._items.append(item)
        self.relayout()
        self.changed.emit()

    def replace_item(self, uid: int, item) -> None:
        for i, it in enumerate(self._items):
            if it.uid == uid:
                self._items[i] = item
                break
        self.relayout()
        self.changed.emit()

    def remove_item(self, uid: int) -> None:
        self._items = [it for it in self._items if it.uid != uid]
        self._collapsed.discard(uid)
        self.relayout()
        self.changed.emit()

    def clear(self) -> None:
        self._items = []
        self._collapsed.clear()
        self.relayout()
        self.changed.emit()

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
            # A tune point shows its parameter changes inline (no caret panel).
            summary = ", ".join(f"{k}={v}" for k, v in (item.params or {}).items())
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
        return bool(item.args) and item.uid not in self._collapsed

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
        keep on-air-anchored points left of off-air-anchored ones)."""
        return tlm.offset_to_x(item.anchor, item.offset, self._on, self._off, self._zoom)

    def _item_left(self, item) -> float:
        """Left x the item's name/panel starts at (for panel anchoring/packing)."""
        if item.kind == "bar":
            sx = tlm.offset_to_x("start", item.start_offset, self._on, self._off, self._zoom)
            px = tlm.offset_to_x("stop", item.stop_offset, self._on, self._off, self._zoom)
            return min(sx, px)
        return self._run_cx(item) - self._run_width(item) / 2

    def _foot_h(self, item) -> int:
        """Total vertical footprint: name row + caption row + panel (if expanded)."""
        h = LANE_H + CAPTION_H
        if self._expanded(item):
            h += self._panel_height(item)
        return h

    def _span(self, item) -> Tuple[float, float]:
        """Horizontal [left, right] the item occupies (for lane packing) — includes
        the inline argument panel when it's expanded."""
        if item.kind == "bar":
            sx = tlm.offset_to_x("start", item.start_offset, self._on, self._off, self._zoom)
            px = tlm.offset_to_x("stop", item.stop_offset, self._on, self._off, self._zoom)
            left, right = sx - HANDLE_W, px + HANDLE_W
        elif tlm._is_ramp(item):
            (la, lo), (ra, ro) = tlm.ramp_span(item)
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
        placed: List[List[Tuple[float, float]]] = []
        lane_of: Dict[int, int] = {}
        ordered = sorted(self._items, key=lambda it: self._span(it)[0])
        for it in ordered:
            left, right = self._span(it)
            for idx, spans in enumerate(placed):
                if all(right + LANE_VGAP <= l or left >= r + LANE_VGAP for (l, r) in spans):
                    spans.append((left, right))
                    lane_of[it.uid] = idx
                    break
            else:
                placed.append([(left, right)])
                lane_of[it.uid] = len(placed) - 1
        return lane_of

    def relayout(self) -> None:
        """Recompute content metrics (depend only on the items), then place.

        Each lane's height is the tallest footprint of the items in it (an expanded
        task is taller), and lanes stack with cumulative y so an expanded panel or
        an offset caption never overlaps the task below it."""
        # Un-centered content anchors + intrinsic content width. Factored into a
        # hook so a subclass (the plan timeline) can supply a window-only geometry.
        self._c_on, self._c_off, self._content_w = self._compute_anchors()
        self._on, self._off = self._c_on, self._c_off   # for shift-invariant lane packing
        self._lane_of = self._assign_lanes()
        n_lanes = (max(self._lane_of.values()) + 1) if self._lane_of else 1

        row_h: Dict[int, int] = {}
        for it in self._items:
            lane = self._lane_of[it.uid]
            row_h[lane] = max(row_h.get(lane, LANE_H + CAPTION_H), self._foot_h(it))
        self._lane_y = {}
        y = LANES_TOP
        for lane in range(n_lanes):
            self._lane_y[lane] = y
            y += row_h.get(lane, LANE_H + CAPTION_H) + LANE_VGAP
        stack_bottom = y - LANE_VGAP
        self._content_h = max(210, stack_bottom + AXIS_GAP + BASELINE_FROM_BOTTOM)
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
        mid0 = (self._c_on + self._c_off) / 2.0
        shift = avail_w / 2.0 - mid0
        shift = max(0.0, min(shift, max(0.0, avail_w - self._content_w)))
        self._on = self._c_on + shift
        self._off = self._c_off + shift
        # Tasks are laid out from the top (lane y's); the axis rides the widget
        # bottom, so a taller widget grows the gap between them.
        self._baseline = max(self._content_h, self.height()) - BASELINE_FROM_BOTTOM

        self._geom = {}
        for it in self._items:
            y = self._lane_y.get(self._lane_of.get(it.uid, 0), LANES_TOP)
            g = {"kind": it.kind, "y": y}
            if it.kind == "bar":
                g["start_x"] = tlm.offset_to_x("start", it.start_offset, self._on, self._off, self._zoom)
                g["stop_x"] = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off, self._zoom)
            elif tlm._is_ramp(it):
                # A ramp draws as a duration bar between its two anchored ends.
                (la, lo), (ra, ro) = tlm.ramp_span(it)
                g["start_x"] = tlm.offset_to_x(la, lo, self._on, self._off, self._zoom)
                g["stop_x"] = tlm.offset_to_x(ra, ro, self._on, self._off, self._zoom)
                g["ends"] = ((la, lo), (ra, ro))
            else:
                g["cx"] = self._run_cx(it)
                g["w"] = self._run_width(it)
            if self._expanded(it):
                g["panel"] = (self._item_left(it) + 2, y + LANE_H + CAPTION_H,
                              self._panel_width(it), self._panel_height(it))
            g["foot_h"] = self._foot_h(it)
            self._geom[it.uid] = g
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        baseline = int(self._baseline)
        on_x, off_x = int(self._on), int(self._off)

        # On-air band (the fixed, not-to-scale middle) — a faint fill so it reads
        # as the "busiest" region.
        p.fillRect(on_x, LANES_TOP - 10, off_x - on_x, baseline - (LANES_TOP - 10),
                   QColor(Palette.ONLINE_SOFT))

        p.setPen(QPen(QColor(Palette.BORDER_STRONG), 2))
        p.drawLine(tlm.EDGE_PAD // 2, baseline, self.width() - tlm.EDGE_PAD // 2, baseline)

        tick_font = QFont(); tick_font.setPointSize(8)
        p.setFont(tick_font)
        self._paint_ticks(p, baseline, on_x, negative=True)
        self._paint_ticks(p, baseline, off_x, negative=False)

        self._paint_anchor(p, on_x, baseline, "ON-AIR", Palette.ONLINE)
        self._paint_anchor(p, off_x, baseline, "OFF-AIR", Palette.CRASH)

        cap_font = QFont(); cap_font.setPointSize(9); cap_font.setItalic(True)
        p.setFont(cap_font)
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(tlm.EDGE_PAD, 16, max(0, on_x - tlm.EDGE_PAD), 14,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "warm-up")
        p.drawText(on_x, baseline + 26, off_x - on_x, 14,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), "· on air ·")
        p.drawText(off_x, 16, max(0, self.width() - off_x - tlm.EDGE_PAD), 14,
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), "cool-down")

        for it in self._items:
            if it.kind == "bar":
                self._paint_bar(p, it)
            elif tlm._is_ramp(it):
                self._paint_ramp(p, it)
            else:
                self._paint_run(p, it)
        p.end()

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

    def _paint_anchor(self, p, x, baseline, label, color):
        p.setPen(QPen(QColor(color), 2))
        p.drawLine(x, LANES_TOP - 12, x, baseline + 6)
        f = QFont(); f.setPointSize(9); f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(x - 60, baseline + 8, 120, 16,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)

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

    def _paint_bar(self, p, it):
        g = self._geom[it.uid]
        y, sx, px = g["y"], g["start_x"], g["stop_x"]
        known = self.task_known(it.task_name)
        border = QColor(Palette.ONLINE if known else Palette.CRASH)
        left = min(sx, px)
        rect = QRectF(left, y, max(HANDLE_W * 2.0, abs(px - sx)), LANE_H)
        p.setPen(QPen(border, 2))
        p.setBrush(QBrush(QColor(Palette.SURFACE)))
        p.drawRoundedRect(rect, 9, 9)

        # Two handle grips.
        for hx in (sx, px):
            hr = QRectF(hx - HANDLE_W / 2, y + 3, HANDLE_W, LANE_H - 6)
            p.setBrush(QBrush(border))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(hr, 4, 4)
            p.setPen(QPen(QColor(Palette.SURFACE), 1))
            for gx in (hx - 2, hx + 1):
                p.drawLine(int(gx), int(y + 9), int(gx), int(y + LANE_H - 9))

        # Centered label.
        p.setFont(self._label_font)
        p.setPen(QColor(Palette.TEXT if known else Palette.CRASH))
        fm = QFontMetrics(self._label_font)
        pad = HANDLE_W * 2 + 8 + (CARET_W if it.args else 0)
        label = fm.elidedText(self._bar_label(it), Qt.TextElideMode.ElideRight,
                              max(10, int(rect.width()) - pad))
        p.drawText(rect.adjusted(HANDLE_W + 2, 0, -(HANDLE_W + 2 + (CARET_W if it.args else 0)), 0),
                   int(Qt.AlignmentFlag.AlignCenter), label)
        if it.args:
            self._paint_caret(p, it, g, border)

        # Timing chips under each handle (side is implied by the handle).
        cap_y = int(y + LANE_H + 1)
        self._paint_timing(p, sx, cap_y, _timing_text(it.start_offset, "start", with_side=False))
        self._paint_timing(p, px, cap_y, _timing_text(it.stop_offset, "stop", with_side=False))
        self._paint_panel(p, it, g, border)

    def _paint_ramp(self, p, it):
        """A ramp draws as a duration bar between its two anchored ends, with a
        diagonal cue for direction and a timing chip under each end (so the anchor,
        start and stop are all legible — like a duration task)."""
        g = self._geom[it.uid]
        y, sx, px = g["y"], g["start_x"], g["stop_x"]
        known = self.task_known(it.task_name)
        border = QColor(Palette.ACCENT if known else Palette.CRASH)
        fill = QColor(Palette.ACCENT_SOFT if known else Palette.CRASH_SOFT)
        left = min(sx, px)
        rect = QRectF(left, y, max(RAMP_MIN_W, abs(px - sx)), LANE_H)
        p.setPen(QPen(border, 2))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, 9, 9)

        # Diagonal direction cue: rises if the value increases, else falls.
        r = dict(getattr(it, "ramp", None) or {})
        a, b = r.get("start"), r.get("stop")
        rising = (a is not None and b is not None and b >= a)
        guide = QColor(border); guide.setAlpha(90)
        p.setPen(QPen(guide, 1.5))
        y0, y1 = ((rect.bottom() - 7, rect.top() + 7) if rising
                  else (rect.top() + 7, rect.bottom() - 7))
        p.drawLine(int(rect.left() + 7), int(y0), int(rect.right() - 7), int(y1))

        # Centered label.
        p.setFont(self._label_font)
        p.setPen(QColor(Palette.ACCENT if known else Palette.CRASH))
        fm = QFontMetrics(self._label_font)
        label = fm.elidedText(_ramp_summary(r, it.anchor), Qt.TextElideMode.ElideRight,
                              max(10, int(rect.width()) - 16))
        p.drawText(rect.adjusted(8, 0, -8, 0), int(Qt.AlignmentFlag.AlignCenter), label)

        # Timing chip under each end (its anchor tells start vs stop).
        (la, lo), (ra, ro) = g.get("ends", (("start", 0.0), ("start", 0.0)))
        cap_y = int(y + LANE_H + 1)
        self._paint_timing(p, sx, cap_y, _timing_text(lo, la, with_side=True))
        if abs(px - sx) > RAMP_MIN_W / 2:
            self._paint_timing(p, px, cap_y, _timing_text(ro, ra, with_side=True))

    def _paint_run(self, p, it):
        g = self._geom[it.uid]
        y, cx, w = g["y"], g["cx"], g["w"]
        known = self.task_known(it.task_name)
        is_live = getattr(it, "action", "run") in ("tune", "ramp")
        if not known:
            border, fill, text = Palette.CRASH, Palette.CRASH_SOFT, Palette.CRASH
        elif is_live:                       # tune/ramp points read as a distinct accent
            border, fill, text = Palette.ACCENT, Palette.ACCENT_SOFT, Palette.ACCENT
        else:
            border, fill, text = Palette.ARMED, Palette.ARMED_SOFT, Palette.TEXT
        border = QColor(border)
        rect = QRectF(cx - w / 2, y, w, LANE_H)
        p.setPen(QPen(border, 2))
        p.setBrush(QBrush(QColor(fill)))
        p.drawRoundedRect(rect, LANE_H / 2, LANE_H / 2)
        p.setFont(self._label_font)
        p.setPen(QColor(text))
        fm = QFontMetrics(self._label_font)
        pad = 20 + (CARET_W if it.args else 0)
        label = fm.elidedText(self._run_label(it), Qt.TextElideMode.ElideRight, max(10, int(w) - pad))
        p.drawText(rect.adjusted(10, 0, -(6 + (CARET_W if it.args else 0)), 0),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        if it.args:
            self._paint_caret(p, it, g, border)
        # Timing chip under the pill (a run re-anchors, so keep the side label).
        self._paint_timing(p, cx, int(y + LANE_H + 1),
                           _timing_text(it.offset, it.anchor, with_side=True))
        self._paint_panel(p, it, g, border)

    # ── Hit-testing ───────────────────────────────────────────────────────────

    def _caret_center(self, it, g) -> float:
        """X of the ▾ 'show arguments' caret for an item (only meaningful if it
        has args)."""
        if it.kind == "bar":
            right = max(g["start_x"], g["stop_x"])
            return right - HANDLE_W - CARET_W / 2 - 2
        return g["cx"] + g["w"] / 2 - CARET_W / 2 - 6

    def _hit(self, x: float, y: float) -> Optional[Tuple[object, str]]:
        for it in self._items:
            g = self._geom.get(it.uid)
            if not g:
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

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        hit = self._hit(pos.x(), pos.y())
        if hit is None:
            self._drag = None
            return
        it, part = hit
        if part == "caret":
            self._drag = None
            self._toggle_collapsed(it)
            return
        self._drag = {
            "item": it, "part": part, "press_x": pos.x(), "moved": False,
            "start0": getattr(it, "start_offset", 0.0),
            "stop0": getattr(it, "stop_offset", 0.0),
        }

    def _toggle_collapsed(self, it) -> None:
        if it.uid in self._collapsed:
            self._collapsed.discard(it.uid)
        else:
            self._collapsed.add(it.uid)
        self.relayout()   # heights change → re-pack lanes

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        if self._drag is None:
            hit = self._hit(pos.x(), pos.y())
            if hit and hit[1] in ("bar_start", "bar_stop"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif hit:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
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
        if part == "run_body":
            # A one-shot keeps its anchor (changed only in the editor); dragging
            # only moves the offset, measured to scale from that fixed anchor — so
            # the seconds scale with the distance to the anchor and never jump.
            anchor_x = self._on if it.anchor == "start" else self._off
            it.offset = self._clamp_tune_offset(it, tlm._snap((x - anchor_x) / self._eff()))
            self._live_relayout(it)
            return
        mid = tlm.midpoint(self._on, self._off)
        eff = self._eff()
        if part == "bar_start":
            it.start_offset = tlm.resolve_bar_start(x, self._on, self._off, self._zoom)
        elif part == "bar_stop":
            it.stop_offset = tlm.resolve_bar_stop(x, self._on, self._off, self._zoom)
        elif part == "bar_body":
            ds = tlm._snap((x - self._drag["press_x"]) / eff)
            it.start_offset = min(self._drag["start0"] + ds, (mid - self._on) / eff)
            it.stop_offset = max(self._drag["stop0"] + ds, (mid - self._off) / eff)
        self._live_relayout(it)

    def _clamp_tune_offset(self, it, offset: float) -> float:
        """Keep a tune point inside the on-air span of the task it acts on: a
        start-anchored tune can't be dragged before the task's on-air start, a
        stop-anchored one can't pass its off-air stop. One-shots (not tunes) act on
        their own task, so they're free to sit anywhere."""
        if getattr(it, "action", "run") != "tune":
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
            g["start_x"] = tlm.offset_to_x("start", it.start_offset, self._on, self._off, self._zoom)
            g["stop_x"] = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off, self._zoom)
        else:
            g["cx"] = tlm.offset_to_x(it.anchor, it.offset, self._on, self._off, self._zoom)
        if "panel" in g:
            g["panel"] = (self._item_left(it) + 2, g["panel"][1], g["panel"][2], g["panel"][3])
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        drag = self._drag
        self._drag = None
        if drag["moved"]:
            self.relayout()
            self.changed.emit()
        else:
            self.edit_item(drag["item"])

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
        # Ramps have their own editor; everything else uses the step editor.
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
        if kind == "bar":
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
        self._built = True

        if editor._hub is not None:
            editor._hub.task_done.connect(self._on_params)
        self.finished.connect(lambda _=0: self._disconnect())

        # Initial population (uses whatever task the type-filtered dropdown settled on).
        self._select_task(self._task.currentText(), initial=True)

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self, item) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # Task — only tasks already defined on the unit are selectable.
        self._task = QComboBox()
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
        self._type = QComboBox()
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

        # Duration offsets (two ends).
        self._start_off = DurationSpinBox()
        self._stop_off = DurationSpinBox()
        self._row_start = self._add_row(form, "Start — from ON-AIR", self._start_off)
        self._row_stop = self._add_row(form, "Stop — from OFF-AIR", self._stop_off)

        # One-shot anchor + single offset.
        self._anchor = QComboBox()
        self._anchor.addItem("on-air (T0)", "start")
        self._anchor.addItem("off-air", "stop")
        self._run_off = DurationSpinBox()
        self._row_anchor = self._add_row(form, "Anchor", self._anchor)
        self._row_run = self._add_row(form, "Offset — from anchor", self._run_off)

        # Prefill offset widgets from the source item.
        if item.kind == "bar":
            self._start_off.setValue(float(item.start_offset))
            self._stop_off.setValue(float(item.stop_offset))
        else:
            self._anchor.setCurrentIndex(0 if item.anchor == "start" else 1)
            self._run_off.setValue(float(item.offset))

        outer.addLayout(form)

        # The full parameter form for the task's script.
        self._params_status = QLabel("")
        self._params_status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._params_status)

        self._form = ParamForm()
        pscroll = QScrollArea()
        pscroll.setWidgetResizable(True)
        pscroll.setWidget(self._form)
        pscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        pscroll.setMinimumHeight(150)
        pscroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        outer.addWidget(pscroll, stretch=1)

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

        buttons = QDialogButtonBox()
        if not self._new:
            remove = QPushButton("Remove")
            remove.setStyleSheet(f"color: {Palette.CRASH};")
            buttons.addButton(remove, QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.clicked.connect(lambda: self.done(self.REMOVE))
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

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
        self._set_row_visible(self._anchor, not is_bar)   # a point (run/tune) has one anchor
        self._set_row_visible(self._run_off, not is_bar)
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
            self._params_status.setText(
                "this task has no script parameter schema — use extra args" if task else "")
            return

        cache = self._editor.param_cache()
        if script in cache:
            self._build_form(script)
            return
        self._params_status.setText(f"loading parameters for {script}…")
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
                self._params_status.setText(f"could not load parameters: {result}")
            return
        self._editor.param_cache()[script] = (result or {}).get("params", [])
        if script == self._current_script:
            self._build_form(script)

    def _build_form(self, script: str) -> None:
        specs = self._editor.param_cache().get(script, [])
        if self._is_tune():
            # Only live-tunable params can be changed mid-run; the checkboxes let
            # you pick exactly which ones this step sets.
            specs = [s for s in specs if s.get("live")]
            self._form.set_params(specs, selectable=True)
            self._params_status.setText(
                "tick the parameters to set at this offset" if specs
                else "this task's script declares no live parameters")
            self._seed_from_params()
        else:
            self._form.set_params(specs)
            self._params_status.setText(
                "" if specs else "this script declares no parameters — use extra args")
            self._apply_prefill()

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

    def _accept(self) -> None:
        task = self._task.currentText().strip()
        if not task:
            self._params_status.setText("pick a task first")
            return
        err = self._form.validate()
        if err:
            self._params_status.setText(err)
            return
        uid = self._src.uid
        mode = self._type.currentData()
        if mode == "tune":
            params = self._form.values()
            if not params:
                self._params_status.setText("set at least one live parameter to tune")
                return
            anchor = self._anchor.currentData()
            offset = round(self._run_off.value(), 1)
            spans_getter = getattr(self._editor, "task_spans", None)
            if spans_getter is not None:
                err = tlm.step_within_task_error(spans_getter(task), anchor, offset, kind="tune")
                if err:
                    self._params_status.setText(err)
                    return
            self.result_item = tlm.RunItem(
                task_name=task, action="tune", params=params,
                anchor=anchor, offset=offset, uid=uid)
        elif mode == "bar":
            self.result_item = tlm.BarItem(
                task_name=task, args=self._build_args(), replace_args=True,
                start_offset=round(self._start_off.value(), 1),
                stop_offset=round(self._stop_off.value(), 1), uid=uid)
        else:
            self.result_item = tlm.RunItem(
                task_name=task, args=self._build_args(), replace_args=True,
                anchor=self._anchor.currentData(),
                offset=round(self._run_off.value(), 1), uid=uid)
        self.accept()

    def _disconnect(self) -> None:
        if self._editor._hub is None:
            return
        try:
            self._editor._hub.task_done.disconnect(self._on_params)
        except (TypeError, RuntimeError):
            pass


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
        self._params_inflight: set = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        self._add_bar = QPushButton("+ Duration")
        self._add_bar.setToolTip("A task that runs across the on-air window (start + stop)")
        self._add_run = QPushButton("+ One-shot")
        self._add_run.setToolTip("A task that fires once and exits (many allowed)")
        self._add_tune = QPushButton("+ Tune")
        self._add_tune.setToolTip("Change a running duration task's live parameters at a set time")
        self._add_ramp = QPushButton("+ Ramp")
        self._add_ramp.setToolTip("Sweep a running duration task's live parameter over time")
        self._add_bar.clicked.connect(lambda: self._canvas.add_new("bar"))
        self._add_run.clicked.connect(lambda: self._canvas.add_new("run"))
        self._add_tune.clicked.connect(lambda: self._canvas.add_new("tune"))
        self._add_ramp.clicked.connect(lambda: self._canvas.add_new("ramp"))
        bar.addWidget(self._add_bar)
        bar.addWidget(self._add_run)
        bar.addWidget(self._add_tune)
        bar.addWidget(self._add_ramp)
        bar.addStretch(1)
        # Minimum on-air duration the current steps require (ramps at both ends etc).
        self._mindur = QLabel("")
        self._mindur.setStyleSheet(f"font-size: 11px; color: {Palette.ACCENT};")
        bar.addWidget(self._mindur)
        self._hint = QLabel("Drag handles to set timing · click to edit")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        # Zoom readout — Ctrl+wheel / pinch to zoom; click to reset to 100%.
        self._zoom_btn = QPushButton("100%")
        self._zoom_btn.setFixedWidth(52)
        self._zoom_btn.setFlat(True)
        self._zoom_btn.setToolTip("Horizontal zoom — Ctrl+scroll or pinch. Click to reset.")
        self._zoom_btn.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        self._zoom_btn.clicked.connect(lambda: self._canvas.reset_zoom())
        bar.addWidget(self._zoom_btn)
        outer.addLayout(bar)

        self._canvas = _TimelineCanvas(self)
        self._canvas.changed.connect(self.changed.emit)
        self._canvas.changed.connect(self._update_mindur)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)   # canvas stretches to fill a wider window
        scroll.setWidget(self._canvas)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(240)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        self._canvas.set_scroll_area(scroll)
        outer.addWidget(scroll, stretch=1)

    def _sync_zoom(self) -> None:
        self._zoom_btn.setText(f"{round(self._canvas._zoom * 100)}%")

    # ── Context injected by the host dialog ──────────────────────────────────

    def set_context(self, hub, hostname: str) -> None:
        self._hub = hub
        self._hostname = hostname

    def set_task_commands(self, mapping: Dict[str, List[str]]) -> None:
        self._task_commands = dict(mapping)

    def script_for_task(self, task: str) -> Tuple[str, List[str]]:
        """(script_filename, default_args) for a task name — for the step editor."""
        cmd = self._task_commands.get(task)
        if not cmd:
            return "", []
        return tlm.script_of_command(cmd)

    def param_cache(self) -> Dict[str, list]:
        return self._param_specs

    # ── Task list (populated once the unit's tasks are fetched) ──────────────

    def set_tasks(self, names: List[str]) -> None:
        self._tasks = list(names)
        if not self._tasks:
            self._hint.setText("no tasks on this unit — define one in the Tasks tab first")
        else:
            self._hint.setText("Drag handles to set timing · click to edit")
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

    # ── Add / load / read steps ──────────────────────────────────────────────

    def set_steps(self, steps: List[m.SequenceStep]) -> None:
        dicts = []
        for s in steps:
            ramp = getattr(s, "ramp", None)
            end = getattr(s, "offset_end_s", None)
            dicts.append({
                "anchor": s.anchor, "offset_s": float(s.offset_s),
                "offset_end_s": float(end) if end is not None else 0.0,
                "action": s.action.value if hasattr(s.action, "value") else str(s.action),
                "task_name": s.task_name, "args": list(getattr(s, "args", []) or []),
                "replace_args": bool(getattr(s, "replace_args", False)),
                "params": dict(getattr(s, "params", {}) or {}),
                "ramp": (ramp.model_dump() if hasattr(ramp, "model_dump")
                         else dict(ramp)) if ramp else None,
            })
        self._canvas.set_items(tlm.steps_to_items(dicts))

    def steps(self) -> List[m.SequenceStep]:
        out: List[m.SequenceStep] = []
        for d in tlm.items_to_steps(self._canvas.items()):
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
                ramp=m.RampSpec(**ramp) if ramp else None))
        return out

    # ── Validation (mirrors the agent's _validate_steps) ─────────────────────

    def validate(self) -> Optional[str]:
        return tlm.validate(self._canvas.items(), self._tasks or None)
