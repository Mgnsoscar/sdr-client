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

Because the length of the on-air window is NOT part of the sequence (it's chosen
at arm time), the middle band between the two anchors is drawn at a FIXED width —
the busiest region, so it's given the most room — and is not to scale. Each side
(warm-up / cool-down) IS to scale at a fixed pixels-per-second, so dragging maps
linearly to an offset.

Interaction:
  - Drag a bar's START handle (on-air side) or STOP handle (off-air side); neither
    crosses the middle. Drag the bar body to shift both together.
  - Drag a run pill across the middle to re-anchor it (on-air ↔ off-air).
  - Click a bar or pill to open its editor: pick the task, choose its parameters
    with the full parameter form (pre-filled from the task, never mutating it),
    set the offsets, or Remove it.
  - "+ Duration" / "+ One-shot" add a new object.

All geometry / conversion logic lives in timeline_model.py (no Qt, unit-tested);
this module is the Qt view + the step editor over it. No network I/O happens in
the canvas; the step editor fetches a script's parameter schema via the hub.
"""
from __future__ import annotations

import shlex
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import models as m
from . import timeline_model as tlm
from .param_form import ParamForm
from .theme import Palette

# ── View geometry (paint sizes; timing geometry lives in timeline_model) ──────
LANES_TOP = 34              # y of the first lane
LANE_H = 34                 # bar / pill height
LANE_VGAP = 12              # vertical gap between lanes
BASELINE_FROM_BOTTOM = 50   # baseline sits this far above the canvas bottom
HANDLE_W = 12               # drawn width of a bar's grip
HANDLE_HIT = 11             # px each side of a handle centre that grabs it
RUN_MIN_W = 120             # minimum run-pill width
RUN_MAX_W = 260
CARET_W = 20                # width of the ▾ "show arguments" zone on an item
TICK_S = 30                 # a faint tick + label every this many seconds
DRAG_THRESHOLD = 4          # px of movement before a press counts as a drag


def _fmt_offset(offset_s: float) -> str:
    """'-120s', '+5s', '0s' — integer when whole, else one decimal."""
    n = int(offset_s) if offset_s == int(offset_s) else round(offset_s, 1)
    if n > 0:
        return f"+{n}s"
    if n < 0:
        return f"{n}s"
    return "0s"


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


class _ArgsPopup(QFrame):
    """A small frameless popup listing an item's arguments as flag → value rows."""

    def __init__(self, task_name: str, args: List[str], parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            f"QFrame {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER_STRONG}; "
            f"border-radius: 8px; }}")
        grid = QGridLayout(self)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)

        title = QLabel(task_name or "(no task)")
        title.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 600; border: none;")
        grid.addWidget(title, 0, 0, 1, 2)

        pairs = _arg_pairs(args)
        if not pairs:
            none = QLabel("no arguments")
            none.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px; border: none;")
            grid.addWidget(none, 1, 0, 1, 2)
        for r, (flag, val) in enumerate(pairs, start=1):
            fl = QLabel(flag or "(positional)")
            fl.setStyleSheet(f"color: {Palette.TEXT_MUTED}; font-size: 11px; border: none;")
            grid.addWidget(fl, r, 0)
            vl = QLabel("✓" if val is None else val)
            vl.setStyleSheet(
                f"color: {Palette.TEXT}; font-size: 11px; font-weight: 600; border: none;")
            vl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(vl, r, 1)


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
        self._label_font = QFont(); self._label_font.setPointSize(10); self._label_font.setBold(True)
        self._cap_font = QFont(); self._cap_font.setPointSize(8)
        self._content_w, self._content_h = tlm.EDGE_PAD, 210
        self.setMouseTracking(True)
        self.relayout()

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
        self.relayout()
        self.changed.emit()

    def clear(self) -> None:
        self._items = []
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
        # Name only — the arguments live behind the ▾ caret / editor.
        return f"⚡ {item.task_name or '(no task)'}".strip()

    def _bar_label(self, item) -> str:
        name = item.task_name or "(no task)"
        n = len(_arg_pairs(item.args)) if item.args else 0
        return f"{name}   · {n} arg{'s' if n != 1 else ''}" if n else name

    def _span(self, item) -> Tuple[float, float]:
        """Horizontal [left, right] the item occupies (for lane packing)."""
        if item.kind == "bar":
            sx = tlm.offset_to_x("start", item.start_offset, self._on, self._off)
            px = tlm.offset_to_x("stop", item.stop_offset, self._on, self._off)
            return sx - HANDLE_W, px + HANDLE_W
        cx = tlm.offset_to_x(item.anchor, item.offset, self._on, self._off)
        w = self._run_width(item)
        return cx - w / 2, cx + w / 2

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
        self._on, self._off, width = tlm.compute_anchors(self._items)
        lanes = self._assign_lanes()
        n_lanes = (max(lanes.values()) + 1) if lanes else 1
        height = LANES_TOP + n_lanes * (LANE_H + LANE_VGAP) + BASELINE_FROM_BOTTOM
        height = max(height, 210)
        # Content size drives the scroll extents; the host QScrollArea is
        # widget-resizable, so the canvas STRETCHES to fill a wider viewport
        # (height stays exactly the content height) and only scrolls when the
        # content is wider/taller than the viewport.
        self._content_w, self._content_h = int(width), int(height)
        self.setMinimumWidth(self._content_w)
        self.setFixedHeight(self._content_h)
        self.updateGeometry()

        self._geom = {}
        for it in self._items:
            lane = lanes.get(it.uid, 0)
            y = LANES_TOP + lane * (LANE_H + LANE_VGAP)
            if it.kind == "bar":
                sx = tlm.offset_to_x("start", it.start_offset, self._on, self._off)
                px = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off)
                self._geom[it.uid] = {"kind": "bar", "y": y, "start_x": sx, "stop_x": px}
            else:
                cx = tlm.offset_to_x(it.anchor, it.offset, self._on, self._off)
                w = self._run_width(it)
                self._geom[it.uid] = {"kind": "run", "y": y, "cx": cx, "w": w}
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        baseline = self.height() - BASELINE_FROM_BOTTOM
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
            else:
                self._paint_run(p, it)
        p.end()

    def _paint_ticks(self, p, baseline, anchor_x, negative):
        step_px = TICK_S * tlm.SCALE
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
            label = f"{'-' if negative else '+'}{TICK_S * i}s"
            p.drawText(int(x) - 18, baseline + 6, 36, 12,
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

    def _paint_caret(self, p, cx, y, color):
        """A small rounded ▾ chip that reveals the item's arguments on click."""
        r = QRectF(cx - CARET_W / 2, y + 6, CARET_W, LANE_H - 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(color)))
        p.drawRoundedRect(r, 4, 4)
        f = QFont(); f.setPointSize(9); f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(Palette.SURFACE))
        p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "▾")

    def _paint_bar(self, p, it):
        g = self._geom[it.uid]
        y, sx, px = g["y"], g["start_x"], g["stop_x"]
        known = self.task_known(it.task_name)
        border = QColor(Palette.ONLINE if known else Palette.CRASH)
        body = QColor(Palette.SURFACE)
        left = min(sx, px)
        rect = QRectF(left, y, max(HANDLE_W * 2.0, abs(px - sx)), LANE_H)
        p.setPen(QPen(border, 2))
        p.setBrush(QBrush(body))
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
            self._paint_caret(p, self._caret_center(it, g), y, border)

        # Offset captions under each handle.
        p.setFont(self._cap_font)
        p.setPen(QColor(Palette.TEXT_FAINT))
        p.drawText(int(sx) - 30, int(y + LANE_H + 1), 60, 12,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   _fmt_offset(it.start_offset))
        p.drawText(int(px) - 30, int(y + LANE_H + 1), 60, 12,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   _fmt_offset(it.stop_offset))

    def _paint_run(self, p, it):
        g = self._geom[it.uid]
        y, cx, w = g["y"], g["cx"], g["w"]
        known = self.task_known(it.task_name)
        border = QColor(Palette.ARMED if known else Palette.CRASH)
        rect = QRectF(cx - w / 2, y, w, LANE_H)
        p.setPen(QPen(border, 2))
        p.setBrush(QBrush(QColor(Palette.ARMED_SOFT if known else Palette.CRASH_SOFT)))
        p.drawRoundedRect(rect, LANE_H / 2, LANE_H / 2)
        p.setFont(self._label_font)
        p.setPen(QColor(Palette.TEXT if known else Palette.CRASH))
        fm = QFontMetrics(self._label_font)
        pad = 20 + (CARET_W if it.args else 0)
        label = fm.elidedText(self._run_label(it), Qt.TextElideMode.ElideRight, max(10, int(w) - pad))
        p.drawText(rect.adjusted(10, 0, -(6 + (CARET_W if it.args else 0)), 0),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
        if it.args:
            self._paint_caret(p, self._caret_center(it, g), y, border)
        p.setFont(self._cap_font)
        p.setPen(QColor(Palette.TEXT_FAINT))
        side = "on-air" if it.anchor == "start" else "off-air"
        p.drawText(int(cx) - 50, int(y + LANE_H + 1), 100, 12,
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   f"{_fmt_offset(it.offset)} {side}")

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
            if not (g["y"] - 2 <= y <= g["y"] + LANE_H + 2):
                continue
            if it.args and abs(x - self._caret_center(it, g)) <= CARET_W / 2:
                return it, "caret"
            if g["kind"] == "bar":
                if abs(x - g["start_x"]) <= HANDLE_HIT:
                    return it, "bar_start"
                if abs(x - g["stop_x"]) <= HANDLE_HIT:
                    return it, "bar_stop"
                if min(g["start_x"], g["stop_x"]) <= x <= max(g["start_x"], g["stop_x"]):
                    return it, "bar_body"
            else:
                if abs(x - g["cx"]) <= g["w"] / 2:
                    return it, "run_body"
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
            self._show_args_popup(it, e.globalPosition().toPoint())
            return
        self._drag = {
            "item": it, "part": part, "press_x": pos.x(), "moved": False,
            "start0": getattr(it, "start_offset", 0.0),
            "stop0": getattr(it, "stop_offset", 0.0),
        }

    def _show_args_popup(self, it, global_pos: QPoint) -> None:
        popup = _ArgsPopup(it.task_name, list(it.args), self)
        popup.adjustSize()
        popup.move(global_pos + QPoint(-8, 10))
        popup.show()

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
        if not self._drag["moved"] and abs(pos.x() - self._drag["press_x"]) < DRAG_THRESHOLD:
            return
        self._drag["moved"] = True
        it, part = self._drag["item"], self._drag["part"]
        x = pos.x()
        mid = tlm.midpoint(self._on, self._off)
        if part == "bar_start":
            it.start_offset = tlm.resolve_bar_start(x, self._on, self._off)
        elif part == "bar_stop":
            it.stop_offset = tlm.resolve_bar_stop(x, self._on, self._off)
        elif part == "bar_body":
            ds = tlm._snap((x - self._drag["press_x"]) / tlm.SCALE)
            it.start_offset = min(self._drag["start0"] + ds, (mid - self._on) / tlm.SCALE)
            it.stop_offset = max(self._drag["stop0"] + ds, (mid - self._off) / tlm.SCALE)
        elif part == "run_body":
            it.anchor, it.offset = tlm.resolve_run(x, self._on, self._off)
        self._live_relayout(it)

    def _live_relayout(self, it) -> None:
        """Update just the dragged item's geometry without resizing the canvas
        (keeps anchors fixed mid-drag so the item tracks the cursor smoothly)."""
        g = self._geom.get(it.uid)
        if not g:
            return
        if it.kind == "bar":
            g["start_x"] = tlm.offset_to_x("start", it.start_offset, self._on, self._off)
            g["stop_x"] = tlm.offset_to_x("stop", it.stop_offset, self._on, self._off)
        else:
            g["cx"] = tlm.offset_to_x(it.anchor, it.offset, self._on, self._off)
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

    # ── Editing ───────────────────────────────────────────────────────────────

    def edit_item(self, item) -> None:
        dlg = StepEditorDialog(item, self._editor, new=False, parent=self)
        r = dlg.exec()
        if r == StepEditorDialog.REMOVE:
            self.remove_item(item.uid)
        elif r == QDialog.DialogCode.Accepted and dlg.result_item is not None:
            self.replace_item(item.uid, dlg.result_item)

    def add_new(self, kind: str) -> None:
        default_task = self._editor.available_tasks()[0] if self._editor.available_tasks() else ""
        if kind == "bar":
            item = tlm.BarItem(task_name=default_task, start_offset=0.0, stop_offset=0.0)
        else:
            item = tlm.RunItem(task_name=default_task, anchor="start", offset=0.0)
        dlg = StepEditorDialog(item, self._editor, new=True, parent=self)
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
        self._task_touched = False

        self.setWindowTitle("New step" if new else "Edit step")
        self.setMinimumWidth(440)
        self._build(item)

        if editor._hub is not None:
            editor._hub.task_done.connect(self._on_params)
        self.finished.connect(lambda _=0: self._disconnect())

        # Initial population.
        self._select_task(self._task.currentText(), initial=True)

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self, item) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        # Task (source) + inline New… to create a task from a script.
        self._task = QComboBox()
        tasks = self._editor.available_tasks()
        if tasks:
            self._task.addItems(tasks)
        if item.task_name and self._task.findText(item.task_name) < 0:
            self._task.addItem(item.task_name)
        if item.task_name:
            self._task.setCurrentText(item.task_name)
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        self._new_task = QPushButton("New…")
        self._new_task.setFixedWidth(56)
        self._new_task.setToolTip("Create a task from a script without leaving the sequence")
        self._new_task.clicked.connect(self._on_new_task)
        trow = QHBoxLayout(); trow.setContentsMargins(0, 0, 0, 0); trow.setSpacing(6)
        trow.addWidget(self._task, stretch=1); trow.addWidget(self._new_task)
        thost = QWidget(); thost.setLayout(trow)
        form.addRow("Task", thost)

        # Type: duration bar vs one-shot pill.
        self._type = QComboBox()
        self._type.addItem("Duration  (on-air → off-air)", "bar")
        self._type.addItem("One-shot  (fires once)", "run")
        self._type.setCurrentIndex(0 if item.kind == "bar" else 1)
        self._type.currentIndexChanged.connect(self._sync_type)
        form.addRow("Type", self._type)

        # Duration offsets (two ends).
        self._start_off = QDoubleSpinBox()
        self._start_off.setRange(-100000.0, 100000.0); self._start_off.setDecimals(1)
        self._start_off.setSingleStep(1.0); self._start_off.setSuffix(" s")
        self._stop_off = QDoubleSpinBox()
        self._stop_off.setRange(-100000.0, 100000.0); self._stop_off.setDecimals(1)
        self._stop_off.setSingleStep(1.0); self._stop_off.setSuffix(" s")
        self._row_start = self._add_row(form, "Start — from ON-AIR", self._start_off)
        self._row_stop = self._add_row(form, "Stop — from OFF-AIR", self._stop_off)

        # One-shot anchor + single offset.
        self._anchor = QComboBox()
        self._anchor.addItem("on-air (T0)", "start")
        self._anchor.addItem("off-air", "stop")
        self._run_off = QDoubleSpinBox()
        self._run_off.setRange(-100000.0, 100000.0); self._run_off.setDecimals(1)
        self._run_off.setSingleStep(1.0); self._run_off.setSuffix(" s")
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
        outer.addLayout(eform)

        hint = QLabel("Parameters are pre-filled from the task; changing them here only "
                      "affects this step (the task is left unchanged).")
        hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        hint.setWordWrap(True)
        outer.addWidget(hint)

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

    def _sync_type(self) -> None:
        is_bar = self._type.currentData() == "bar"
        self._set_row_visible(self._start_off, is_bar)
        self._set_row_visible(self._stop_off, is_bar)
        self._set_row_visible(self._anchor, not is_bar)
        self._set_row_visible(self._run_off, not is_bar)

    # ── Task → script → parameter form ────────────────────────────────────────

    def _on_new_task(self) -> None:
        name = self._editor.create_task_interactively()
        if name:
            if self._task.findText(name) < 0:
                self._task.addItem(name)
            self._task.setCurrentText(name)

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
        self._form.set_params(specs)
        self._params_status.setText("" if specs else "this script declares no parameters — use extra args")
        self._apply_prefill()

    def _apply_prefill(self) -> None:
        if self._pending_prefill is None:
            return
        extra = self._form.set_values(self._pending_prefill)
        self._extra.setText(" ".join(shlex.quote(e) for e in extra) if extra else "")

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
        args = self._build_args()
        uid = self._src.uid
        if self._type.currentData() == "bar":
            self.result_item = tlm.BarItem(
                task_name=task, args=args, replace_args=True,
                start_offset=round(self._start_off.value(), 1),
                stop_offset=round(self._stop_off.value(), 1), uid=uid)
        else:
            self.result_item = tlm.RunItem(
                task_name=task, args=args, replace_args=True,
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
        self._task_creator: Optional[Callable[[], Optional[str]]] = None
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
        self._add_bar.clicked.connect(lambda: self._canvas.add_new("bar"))
        self._add_run.clicked.connect(lambda: self._canvas.add_new("run"))
        bar.addWidget(self._add_bar)
        bar.addWidget(self._add_run)
        bar.addStretch(1)
        self._hint = QLabel("Drag handles to set timing · click to edit")
        self._hint.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        bar.addWidget(self._hint)
        outer.addLayout(bar)

        self._canvas = _TimelineCanvas(self)
        self._canvas.changed.connect(self.changed.emit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)   # canvas stretches to fill a wider window
        scroll.setWidget(self._canvas)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(240)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        outer.addWidget(scroll, stretch=1)

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
            self._hint.setText("no tasks yet — add one and use “New…” to create it")
        else:
            self._hint.setText("Drag handles to set timing · click to edit")
        self._canvas.relayout()

    def available_tasks(self) -> List[str]:
        return self._tasks

    def set_task_creator(self, fn: Callable[[], Optional[str]]) -> None:
        self._task_creator = fn

    def add_task(self, name: str) -> None:
        if name and name not in self._tasks:
            self._tasks.append(name)
            self._canvas.relayout()

    def create_task_interactively(self) -> Optional[str]:
        if self._task_creator is None:
            return None
        name = self._task_creator()
        if name:
            self.add_task(name)
        return name

    # ── Add / load / read steps ──────────────────────────────────────────────

    def set_steps(self, steps: List[m.SequenceStep]) -> None:
        dicts = [{
            "anchor": s.anchor, "offset_s": float(s.offset_s),
            "action": s.action.value if hasattr(s.action, "value") else str(s.action),
            "task_name": s.task_name, "args": list(getattr(s, "args", []) or []),
            "replace_args": bool(getattr(s, "replace_args", False)),
        } for s in steps]
        self._canvas.set_items(tlm.steps_to_items(dicts))

    def seed_default(self) -> None:
        """Pre-populate the simplest valid sequence: one duration task."""
        t = self._tasks[0] if self._tasks else ""
        self._canvas.add_item(tlm.BarItem(task_name=t, start_offset=0.0, stop_offset=0.0))

    def steps(self) -> List[m.SequenceStep]:
        out: List[m.SequenceStep] = []
        for d in tlm.items_to_steps(self._canvas.items()):
            out.append(m.SequenceStep(
                anchor=d["anchor"], offset_s=d["offset_s"],
                action=m.StepAction(d["action"]), task_name=d["task_name"],
                args=list(d["args"]), replace_args=d["replace_args"]))
        return out

    # ── Validation (mirrors the agent's _validate_steps) ─────────────────────

    def validate(self) -> Optional[str]:
        return tlm.validate(self._canvas.items(), self._tasks or None)
