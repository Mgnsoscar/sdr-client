"""
TimelineTab — the Timeline tab: plans placed at absolute times, shown as a
vertical day-planner.

Layout:
    ┌───────────────┬────────────────────────────────────────┐
    │  month        │  Wednesday, 5 August 2026               │
    │  calendar     │  ┌──────── 00:00 ───────────────────┐   │
    │  (dates with  │  │ …hour grid…                       │   │
    │   plans are   │  │        ┌─────────────┐            │   │
    │   marked)     │  │ 20:00  │ Night plan  │  (block)   │   │
    │               │  │        │ 3 units …   │            │   │
    │  [Add plan…]  │  └────────────────────────────────────┘  │
    └───────────────┴────────────────────────────────────────┘

Each plan is placed by its absolute [start, stop] window (start = on-air / T0,
stop = off-air / T_end). Blocks are positioned by time down the day; overlapping
plans sit side by side. The calendar marks which dates carry plans, so a schedule
can be built days in advance. Clicking a block's body edits or removes it.

Arming is manual and per-block: each block carries an Arm button (disabled once
its start time has passed) that arms the plan's sequences at their absolute
scheduled times — a fixed window, so each sequence runs and stops on schedule
even when armed hours ahead. A block turns amber once armed and green once on
air, with a Stop to cancel/abort; state is read from the units' runs, grouped by
plan and matched to the block's window. (Arming mid-window — after the start —
is intentionally not supported yet.)

Persistence is the local ScheduleStore (schedule.json); plan names/descriptions
are resolved live from the PlanStore.
"""
from __future__ import annotations

import calendar as _calmod
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QDate, QDateTime, QSize, Qt, QRectF, QTimer, QTime, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFrame,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from state import PlanStore, ScheduleStore, new_scheduled_id
from .plan_editor import PlanEditorDialog
from .qt_adapter import DataHub
from .theme import Palette, mono_font

_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING)
CLOCK_WARN_SKEW_S = 1.0

# Human names (used by the calendar, compact list and timeline header).
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")
_WDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Run-state → the day-dot / indicator colour convention shown in the legend.
_DOT_COLOR = {"idle": Palette.ACCENT, "armed": Palette.ARMED, "on air": Palette.ONLINE,
              "onair": Palette.ONLINE, "missing": Palette.IDLE}


def _parse(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _to_utc(local_iso: str) -> Optional[datetime]:
    """A naive local schedule time (e.g. '2026-08-05T20:00') → an absolute UTC
    instant, interpreting it in the machine's local time zone."""
    dt = _parse(local_iso)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()   # attach local tz
    return dt.astimezone(timezone.utc)


# ── Arming a scheduled plan (worker-thread helpers) ──────────────────────────

def _arm_scheduled(fleet: Fleet, plan: m.Plan, start_utc: datetime,
                   stop_utc: datetime) -> List[tuple]:
    """Arm every item of a plan at its absolute scheduled window (fixed, not
    open-ended): on-air = start + the item's on-air offset, off-air = stop + its
    off-air offset. Worker thread. Returns [(item, SequenceRun|None, error|None)]."""
    out = []
    _offsets: dict = {}   # per-unit clock skew, fetched once per host

    def _off(host: str) -> float:
        if host not in _offsets:
            _offsets[host] = fleet.get(host).clock_offset_s()
        return _offsets[host]

    for item in plan.items:
        # Translate to the unit's clock so a skewed unit still fires at the intended
        # wall-clock window (matches single-sequence and manual-plan arming).
        skew = _off(item.hostname)
        on_air = (start_utc + timedelta(seconds=item.on_air_offset_s + skew)).isoformat()
        off_air = (stop_utc + timedelta(seconds=item.off_air_offset_s + skew)).isoformat()
        req = m.ArmSequenceRequest(
            on_air_at=on_air,
            on_air_end=off_air,
            open_ended=False,
            plan_id=plan.id,
            plan_name=plan.name,
            steps=(item.steps or None),
            step_overrides=([] if item.steps else item.overrides),
        )
        try:
            run = fleet.get(item.hostname).arm_sequence(item.sequence_id, req)
            out.append((item, run, None))
        except Exception as exc:  # noqa: BLE001 — reported per item
            out.append((item, None, str(exc)))
    return out


def _stop_runs(fleet: Fleet, runs: List[tuple]) -> List[tuple]:
    """Cancel/abort each (hostname, run_id). Worker thread."""
    out = []
    for hostname, run_id in runs:
        try:
            fleet.get(hostname).cancel_sequence_run(run_id)
            out.append((run_id, None))
        except Exception as exc:  # noqa: BLE001
            out.append((run_id, str(exc)))
    return out


def _fmt_duration(secs: int) -> str:
    total_m, _ = divmod(int(secs), 60)
    h, mm = divmod(total_m, 60)
    if h and mm:
        return f"{h}h {mm}m"
    if h:
        return f"{h}h"
    return f"{mm}m"


# ── Add / edit a scheduled plan ──────────────────────────────────────────────

class _ScheduleDialog(QDialog):
    """Pick a plan and its absolute start/stop times. Returns a ScheduledPlan via
    .result_entry on accept; Remove (result code REMOVE) when editing."""

    REMOVE = 2

    def __init__(self, plans: List[m.Plan], entry: Optional[m.ScheduledPlan] = None,
                 default_day: Optional[date] = None, hub: Optional[DataHub] = None,
                 parent=None):
        super().__init__(parent)
        self._plans = plans
        self._entry = entry
        self._hub = hub
        # This slot's own edited copy of the plan, if any. Seeded from the entry so
        # reopening an already-customized slot keeps its edits. None = follow library.
        self._plan_override: Optional[m.Plan] = (
            entry.plan.model_copy(deep=True) if entry and entry.plan else None)
        self.result_entry: Optional[m.ScheduledPlan] = None

        self.setWindowTitle("Edit scheduled plan" if entry else "Add plan to timeline")
        self.setMinimumWidth(440)
        self._build(default_day or date.today())

    def _build(self, default_day: date) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._plan = QComboBox()
        for p in self._plans:
            self._plan.addItem(p.name or p.id, p.id)
        if self._entry is not None:
            i = self._plan.findData(self._entry.plan_id)
            if i < 0:   # plan was deleted — keep a stub so editing still works
                self._plan.addItem(f"{self._entry.plan_name or self._entry.plan_id} (deleted)",
                                   self._entry.plan_id)
                i = self._plan.findData(self._entry.plan_id)
            self._plan.setCurrentIndex(i)
        form.addRow("Plan", self._plan)

        # Per-slot plan editing: edit THIS slot's copy of the plan without touching the
        # library plan or any other slot that scheduled it.
        edit_row = QHBoxLayout()
        self._edit_plan_btn = QPushButton("Edit plan contents…")
        self._edit_plan_btn.setEnabled(self._hub is not None)
        self._edit_plan_btn.clicked.connect(self._edit_plan_contents)
        edit_row.addWidget(self._edit_plan_btn)
        edit_row.addStretch(1)
        form.addRow("", edit_row)
        self._custom_lbl = QLabel("")
        self._custom_lbl.setWordWrap(True)
        self._custom_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        form.addRow("", self._custom_lbl)
        # Switching to a different plan drops any customization tied to the old one.
        self._plan.currentIndexChanged.connect(self._on_plan_changed)

        self._start = QDateTimeEdit()
        self._start.setCalendarPopup(True)
        self._start.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self._stop = QDateTimeEdit()
        self._stop.setCalendarPopup(True)
        self._stop.setDisplayFormat("yyyy-MM-dd  HH:mm")
        if self._entry is not None:
            self._start.setDateTime(QDateTime.fromString(self._entry.start, Qt.DateFormat.ISODate))
            self._stop.setDateTime(QDateTime.fromString(self._entry.stop, Qt.DateFormat.ISODate))
        else:
            qd = QDate(default_day.year, default_day.month, default_day.day)
            self._start.setDateTime(QDateTime(qd, QTime(20, 0)))
            self._stop.setDateTime(QDateTime(qd, QTime(22, 0)))
        self._start.dateTimeChanged.connect(self._revalidate)
        self._stop.dateTimeChanged.connect(self._revalidate)
        form.addRow("On-air (start)", self._start)
        form.addRow("Off-air (stop)", self._stop)
        outer.addLayout(form)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        if self._entry is not None:
            rm = QPushButton("Remove")
            rm.setStyleSheet(f"color: {Palette.CRASH};")
            self._buttons.addButton(rm, QDialogButtonBox.ButtonRole.DestructiveRole)
            rm.clicked.connect(lambda: self.done(self.REMOVE))
        self._ok = self._buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)
        self._revalidate()
        self._sync_custom_label()

    def _sync_custom_label(self) -> None:
        if self._plan_override is not None:
            self._custom_lbl.setText("✎ Customized for this slot — edits here don't "
                                     "affect the library plan or any other slot.")
            self._custom_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.ACCENT};")
        else:
            self._custom_lbl.setText("Uses the library plan. “Edit plan contents…” "
                                     "makes an independent copy just for this slot.")
            self._custom_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

    def _on_plan_changed(self) -> None:
        # Picking a different plan than the customization belongs to drops it.
        pid = self._plan.currentData()
        if self._plan_override is not None and self._plan_override.id != pid:
            self._plan_override = None
        self._sync_custom_label()
        self._revalidate()

    def _edit_plan_contents(self) -> None:
        if self._hub is None:
            return
        pid = self._plan.currentData()
        if not pid:
            return
        base = self._plan_override
        if base is None or base.id != pid:
            lib = next((p for p in self._plans if p.id == pid), None)
            if lib is None:
                QMessageBox.information(self, "Plan unavailable",
                                       "That plan no longer exists, so its contents "
                                       "can't be edited.")
                return
            base = lib.model_copy(deep=True)   # a fresh copy — never edit the library
        dlg = PlanEditorDialog(self._hub, plan=base, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_plan is not None:
            self._plan_override = dlg.result_plan
            self._sync_custom_label()

    def _revalidate(self) -> None:
        ok = self._stop.dateTime() > self._start.dateTime() and self._plan.count() > 0
        self._ok.setEnabled(ok)
        if self._plan.count() == 0:
            self._status.setText("no plans yet — create one in the Plans tab first")
        elif self._stop.dateTime() <= self._start.dateTime():
            self._status.setText("stop must be after start")
        else:
            span = self._start.dateTime().secsTo(self._stop.dateTime())
            self._status.setText(f"on air for {_fmt_duration(span)}")

    def _accept(self) -> None:
        pid = self._plan.currentData()
        if not pid:
            return
        self.result_entry = m.ScheduledPlan(
            id=self._entry.id if self._entry else new_scheduled_id(),
            plan_id=pid,
            plan_name=self._plan.currentText().replace(" (deleted)", ""),
            start=self._start.dateTime().toString(Qt.DateFormat.ISODate),
            stop=self._stop.dateTime().toString(Qt.DateFormat.ISODate),
            plan=self._plan_override,   # None = follow the library plan
        )
        self.accept()


# ── The vertical day-planner ─────────────────────────────────────────────────

class _DayPlanner(QWidget):
    """A single day, hours running top→bottom, with plan blocks placed by time and
    packed into side-by-side columns where they overlap. Each block is tinted by
    its run state and carries an Arm/Stop action button."""

    block_activated = pyqtSignal(str)   # edit — scheduled-plan id
    arm_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)

    HOUR_PX = 52
    AXIS_W = 58
    TOP_PAD = 10
    BOT_PAD = 12
    BTN_W = 54
    BTN_H = 18
    MIN_BLK_PX = 24          # floor height so 5–15 min plans stay readable/clickable

    # state -> (border, fill)
    _TINT = {
        "idle": (Palette.ACCENT, Palette.ACCENT_SOFT),
        "armed": (Palette.ARMED, Palette.ARMED_SOFT),
        "on air": (Palette.ONLINE, Palette.ONLINE_SOFT),
        "missing": (Palette.IDLE, Palette.IDLE_SOFT),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._date = date.today()
        self._blocks: List[dict] = []
        self._rects: Dict[str, QRectF] = {}
        self._btn_rects: Dict[str, Tuple[QRectF, str]] = {}   # id -> (rect, action)
        self.setMouseTracking(True)
        self.setMinimumWidth(380)

    def content_height(self) -> int:
        return self.TOP_PAD + 24 * self.HOUR_PX + self.BOT_PAD

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, self.content_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(380, self.content_height())

    # ── Data ───────────────────────────────────────────────────────────────────

    def set_day(self, d: date, blocks: List[dict]) -> None:
        """blocks: dicts with id, name, desc, start, stop, state, armable."""
        self._date = d
        self._layout(blocks)
        self.setMinimumHeight(self.content_height())
        self.updateGeometry()
        self.update()

    def _day_bounds(self) -> Tuple[datetime, datetime]:
        start = datetime.combine(self._date, time(0, 0))
        return start, start + timedelta(days=1)

    def _to_y(self, dt: datetime) -> float:
        day_start, _ = self._day_bounds()
        secs = max(0.0, min((dt - day_start).total_seconds(), 86400.0))
        return self.TOP_PAD + secs / 3600.0 * self.HOUR_PX

    def _layout(self, blocks: List[dict]) -> None:
        day_start, day_end = self._day_bounds()
        min_dur = timedelta(minutes=self.MIN_BLK_PX / self.HOUR_PX * 60)
        vis: List[dict] = []
        for b in blocks:
            s, e = b["start"], b["stop"]
            if e <= day_start or s >= day_end:
                continue
            vs, ve = max(s, day_start), min(e, day_end)
            # ve_eff = the block's *drawn* end (true end, or start+floor for very short
            # plans), so column packing matches what's painted: a clamped 5-min block that
            # would visually overlap a neighbour is placed side-by-side, not on top of it.
            ve_eff = min(day_end, max(ve, vs + min_dur))
            vis.append({
                **b, "vs": vs, "ve": ve, "ve_eff": ve_eff,
                "clip_top": s < day_start, "clip_bot": e > day_end,
            })
        vis.sort(key=lambda b: b["vs"])
        self._assign_columns(vis)
        self._blocks = vis

    @staticmethod
    def _assign_columns(vis: List[dict]) -> None:
        """Cluster blocks that overlap in time and give each a column index and the
        cluster's column count (so a block's width = 1/ncols of the lane)."""
        i, n = 0, len(vis)
        while i < n:
            cluster = [vis[i]]
            cluster_end = vis[i]["ve_eff"]
            j = i + 1
            while j < n and vis[j]["vs"] < cluster_end:
                cluster.append(vis[j])
                cluster_end = max(cluster_end, vis[j]["ve_eff"])
                j += 1
            col_ends: List[datetime] = []
            for b in cluster:
                for c, end in enumerate(col_ends):
                    if b["vs"] >= end:
                        col_ends[c] = b["ve_eff"]
                        b["col"] = c
                        break
                else:
                    b["col"] = len(col_ends)
                    col_ends.append(b["ve_eff"])
            for b in cluster:
                b["ncols"] = len(col_ends)
            i = j

    # ── Painting ───────────────────────────────────────────────────────────────

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()

        hour_font = QFont(); hour_font.setPointSize(8)
        for h in range(0, 25):
            y = self.TOP_PAD + h * self.HOUR_PX
            p.setPen(QPen(QColor(Palette.BORDER), 1))
            p.drawLine(self.AXIS_W, int(y), w, int(y))
            if h < 24:
                p.setFont(hour_font)
                p.setPen(QColor(Palette.TEXT_FAINT))
                p.drawText(0, int(y) - 6, self.AXIS_W - 8, 14,
                           int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
                           f"{h:02d}:00")

        # "now" marker when viewing today
        if self._date == date.today():
            y = int(self._to_y(datetime.now()))
            p.setPen(QPen(QColor(Palette.CRASH), 1))
            p.drawLine(self.AXIS_W, y, w, y)
            p.setBrush(QBrush(QColor(Palette.CRASH)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(self.AXIS_W - 3, y - 3, 6, 6))

        self._rects = {}
        self._btn_rects = {}
        avail = max(60, w - self.AXIS_W - 10)
        name_font = QFont(); name_font.setPointSize(10); name_font.setBold(True)
        meta_font = QFont(); meta_font.setPointSize(8)
        desc_font = QFont(); desc_font.setPointSize(9)
        for b in self._blocks:
            colw = avail / b["ncols"]
            x = self.AXIS_W + b["col"] * colw + 3
            y_top = self._to_y(b["vs"])
            y_bot = self._to_y(b["ve"])
            rect = QRectF(x, y_top, colw - 6, max(float(self.MIN_BLK_PX), y_bot - y_top))
            self._rects[b["id"]] = rect
            self._paint_block(p, b, rect, name_font, meta_font, desc_font)
        p.end()

    def _paint_block(self, p, b, rect, name_font, meta_font, desc_font) -> None:
        state = b.get("state", "idle")
        border, fill = self._TINT.get(state, self._TINT["idle"])
        p.setPen(QPen(QColor(border), 1.5))
        p.setBrush(QBrush(QColor(fill)))
        p.drawRoundedRect(rect, 7, 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(border)))
        p.drawRoundedRect(QRectF(rect.left(), rect.top() + 3, 3.5, rect.height() - 6), 1.5, 1.5)

        # Action button (top-right): Arm when idle & still armable, Stop when active.
        btn_gap = self._paint_action(p, b, rect, state, meta_font)

        inner = rect.adjusted(12, 4, -(8 + btn_gap), -4)
        pre = "…" if b["clip_top"] else ""
        post = "…" if b["clip_bot"] else ""
        trange = f"{pre}{b['start'].strftime('%H:%M')} – {b['stop'].strftime('%H:%M')}{post}"
        fm_name = QFontMetrics(name_font)
        fm_meta = QFontMetrics(meta_font)

        # Every block shows its window. When there's no room to stack, put the name
        # on the left and the time on the right of a single line.
        if rect.height() < 34:
            tw = fm_meta.horizontalAdvance(trange)
            p.setFont(meta_font)
            p.setPen(QColor(border))
            p.drawText(QRectF(inner.right() - tw, inner.top(), tw, inner.height()),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), trange)
            p.setFont(name_font)
            p.setPen(QColor(Palette.TEXT))
            name = fm_name.elidedText(b["name"], Qt.TextElideMode.ElideRight,
                                      max(10, int(inner.width() - tw - 8)))
            p.drawText(QRectF(inner.left(), inner.top(), inner.width() - tw - 8, inner.height()),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
            return

        y = inner.top()
        p.setFont(name_font)
        p.setPen(QColor(Palette.TEXT))
        name = fm_name.elidedText(b["name"], Qt.TextElideMode.ElideRight, int(inner.width()))
        p.drawText(QRectF(inner.left(), y, inner.width(), 16),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
        y += 16
        p.setFont(meta_font)
        p.setPen(QColor(border))
        label = trange if state == "idle" else f"{trange}   ·   {state}"
        p.drawText(QRectF(inner.left(), y, inner.width(), 13),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), label)
        y += 14
        # description uses the full width (the button only occupies the first row)
        if b["desc"] and rect.bottom() - y > 12:
            p.setFont(desc_font)
            p.setPen(QColor(Palette.TEXT_MUTED))
            p.drawText(QRectF(rect.left() + 12, y, rect.width() - 20, rect.bottom() - y - 2),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop |
                           Qt.TextFlag.TextWordWrap), b["desc"])

    def _paint_action(self, p, b, rect, state, meta_font) -> int:
        """Draw the block's Arm/Stop button (if any) at the top-right; register its
        hit rect. Returns the horizontal space it reserves on the first row."""
        if state == "on air":
            action, text, col = "stop", "Stop", Palette.CRASH
        elif state == "armed":
            action, text, col = "stop", "Unarm", Palette.ARMED   # cancel a pending arm
        elif state == "idle" and b.get("armable"):
            action, text, col = "arm", "Arm", Palette.ACCENT
        else:
            return 0
        if rect.width() < self.BTN_W + 24 or rect.height() < 20:
            # too small to host a button inline — skip (block can still be edited)
            return 0
        br = QRectF(rect.right() - self.BTN_W - 6, rect.top() + 5, self.BTN_W, self.BTN_H)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(col)))
        p.drawRoundedRect(br, self.BTN_H / 2, self.BTN_H / 2)
        f = QFont(meta_font); f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(Palette.SURFACE))
        p.drawText(br, int(Qt.AlignmentFlag.AlignCenter), text)
        self._btn_rects[b["id"]] = (br, action)
        return self.BTN_W + 10

    # ── Interaction ────────────────────────────────────────────────────────────

    def mouseMoveEvent(self, e):  # noqa: N802
        over = any(r.contains(e.position()) for r in self._rects.values())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        for eid, (br, action) in self._btn_rects.items():
            if br.contains(pos):
                (self.arm_requested if action == "arm" else self.stop_requested).emit(eid)
                return
        for eid, r in self._rects.items():
            if r.contains(pos):
                self.block_activated.emit(eid)
                return


# ── Sleek month calendar + compact day list ──────────────────────────────────

class _Dot(QWidget):
    """A small filled state dot; pulses when it marks a running plan."""

    def __init__(self, color: str, pulse: bool = False, diam: int = 8, parent=None):
        super().__init__(parent)
        self._color = color
        self._alpha = 1.0
        self.setFixedSize(diam, diam)
        if pulse:
            self._up = False
            self._t = QTimer(self)
            self._t.timeout.connect(self._step)
            self._t.start(650)

    def _step(self) -> None:
        self._up = not self._up
        self._alpha = 0.4 if self._up else 1.0
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QColor(self._color)
        c.setAlphaF(self._alpha)
        p.setBrush(QBrush(c))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(self.rect())


class _MonthGrid(QWidget):
    """The painted 7-column day grid. Today wears a persistent accent ring; the
    selected day is a filled cell; a dot in a cell's corner marks the day shown on
    the big timeline; each day carries up to three state-coloured plan dots. A single
    click previews a day, a double click opens it on the timeline."""

    previewed = pyqtSignal(object)   # date
    opened = pyqtSignal(object)      # date

    WEEK_H = 24
    CELL_H = 52
    GAP = 5
    _DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

    def __init__(self, parent=None):
        super().__init__(parent)
        t = date.today()
        self._year, self._month = t.year, t.month
        self._today = t
        self._selected = t
        self._timeline = t
        self._states: Dict[date, List[str]] = {}
        self._hover: Optional[date] = None
        self._ignore_release = False
        self._pending: Optional[date] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire_preview)
        self.setMouseTracking(True)
        self._apply_height()

    # ── data ──
    def month(self) -> Tuple[int, int]:
        return (self._year, self._month)

    def set_month(self, y: int, mo: int) -> None:
        self._year, self._month = y, mo
        self._apply_height()

    def set_states(self, s) -> None:
        self._states = s or {}
        self.update()

    def set_selected(self, d) -> None:
        self._selected = d
        self.update()

    def set_timeline(self, d) -> None:
        self._timeline = d
        self.update()

    def set_today(self, d) -> None:
        self._today = d
        self.update()

    def _rows(self) -> int:
        first = date(self._year, self._month, 1)
        dim = _calmod.monthrange(self._year, self._month)[1]
        return math.ceil((first.weekday() + dim) / 7)

    def _apply_height(self) -> None:
        self.setFixedHeight(self.WEEK_H + self._rows() * self.CELL_H)
        self.update()

    def _dates(self) -> List[date]:
        first = date(self._year, self._month, 1)
        start = first - timedelta(days=first.weekday())      # back to Monday
        return [start + timedelta(days=i) for i in range(self._rows() * 7)]

    def _cell_rect(self, idx: int) -> QRectF:
        cw = self.width() / 7.0
        c, r = idx % 7, idx // 7
        g = self.GAP
        return QRectF(c * cw + g / 2, self.WEEK_H + r * self.CELL_H + g / 2,
                      cw - g, self.CELL_H - g)

    def _hit(self, pos) -> Optional[date]:
        for idx, d in enumerate(self._dates()):
            if d.month == self._month and d.year == self._year and self._cell_rect(idx).contains(pos):
                return d
        return None

    # ── painting ──
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cw = self.width() / 7.0
        f = QFont(); f.setPointSize(8); f.setBold(True)
        p.setFont(f); p.setPen(QColor(Palette.TEXT_FAINT))
        for c, lbl in enumerate(self._DOW):
            p.drawText(QRectF(c * cw, 0, cw, self.WEEK_H), int(Qt.AlignmentFlag.AlignCenter), lbl)
        num_font = QFont(); num_font.setPointSize(11)
        for idx, d in enumerate(self._dates()):
            self._paint_cell(p, self._cell_rect(idx), d, num_font)
        p.end()

    def _paint_cell(self, p, rect, d, num_font) -> None:
        in_month = (d.month == self._month and d.year == self._year)
        is_sel = in_month and d == self._selected
        is_today = in_month and d == self._today
        is_tl = in_month and d == self._timeline
        if is_sel:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(Palette.ACCENT))
            p.drawRoundedRect(rect, 12, 12)
            if is_today:                                      # filled + halo = today & selected
                p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(QColor(Palette.ACCENT), 2))
                p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 13, 13)
        elif is_today:
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(QColor(Palette.ACCENT), 2))
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 11, 11)
        elif in_month and self._hover == d:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(Palette.INSET))
            p.drawRoundedRect(rect, 12, 12)
        # number
        if is_sel:
            nc, bold = Palette.SURFACE, True
        elif is_today:
            nc, bold = Palette.ACCENT_INK, True
        elif not in_month:
            nc, bold = "#B7BEC9", False
        elif d.weekday() >= 5:
            nc, bold = Palette.TEXT_MUTED, False
        else:
            nc, bold = Palette.TEXT, False
        nf = QFont(num_font); nf.setBold(bold)
        p.setFont(nf); p.setPen(QColor(nc))
        p.drawText(QRectF(rect.left(), rect.top() + 6, rect.width(), 18),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), str(d.day))
        if in_month and self._states.get(d):
            self._paint_dots(p, rect, self._states[d], is_sel)
        if is_tl:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(Palette.SURFACE if is_sel else Palette.ACCENT_INK))
            p.drawEllipse(QRectF(rect.right() - 12, rect.top() + 6, 6, 6))

    def _paint_dots(self, p, rect, states, is_sel) -> None:
        shown = states[:3]
        extra = len(states) - len(shown)
        dot, gap = 6, 3
        width = len(shown) * dot + (len(shown) - 1) * gap + (14 if extra else 0)
        x = rect.center().x() - width / 2
        y = rect.bottom() - 13
        for st in shown:
            col = "#FFFFFF" if is_sel else _DOT_COLOR.get(st, Palette.IDLE)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(col))
            p.drawEllipse(QRectF(x, y, dot, dot)); x += dot + gap
        if extra:
            f = QFont(); f.setPointSize(7); f.setBold(True); p.setFont(f)
            p.setPen(QColor("#FFFFFF" if is_sel else Palette.TEXT_FAINT))
            p.drawText(QRectF(x, y - 3, 16, 12),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), f"+{extra}")

    # ── mouse (single-click preview vs double-click open) ──
    def mouseMoveEvent(self, e):  # noqa: N802
        d = self._hit(e.position())
        if d != self._hover:
            self._hover = d; self.update()
        self.setCursor(Qt.CursorShape.PointingHandCursor if d else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, _e):  # noqa: N802
        if self._hover is not None:
            self._hover = None; self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._ignore_release:      # trailing release of a double-click
            self._ignore_release = False
            return
        d = self._hit(e.position())
        if d is not None:
            self._pending = d
            self._timer.start(220)    # wait to see if a double-click follows

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._timer.stop()
        self._ignore_release = True
        d = self._hit(e.position())
        if d is not None:
            self.opened.emit(d)

    def _fire_preview(self) -> None:
        if self._pending is not None:
            self.previewed.emit(self._pending)


class _MonthCalendar(QFrame):
    """Card wrapping the month grid with a header (month + Today + nav) and a legend
    that explains the day-dot colour convention."""

    previewed = pyqtSignal(object)
    opened = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calCard")
        self.setStyleSheet(f"#calCard {{ background:{Palette.SURFACE}; "
                           f"border:1px solid {Palette.BORDER}; border-radius:16px; }}")
        v = QVBoxLayout(self); v.setContentsMargins(18, 16, 18, 15); v.setSpacing(0)

        head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 12); head.setSpacing(6)
        self._title = QLabel(); self._title.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(self._title); head.addStretch(1)
        self._today_btn = self._nav("Today", wide=True); self._today_btn.clicked.connect(self._go_today)
        self._prev = self._nav("‹"); self._prev.clicked.connect(lambda: self._shift(-1))
        self._next = self._nav("›"); self._next.clicked.connect(lambda: self._shift(1))
        for b in (self._today_btn, self._prev, self._next):
            head.addWidget(b)
        v.addLayout(head)

        self._grid = _MonthGrid()
        self._grid.previewed.connect(self.previewed)
        self._grid.opened.connect(self.opened)
        v.addWidget(self._grid)

        v.addSpacing(12)
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{Palette.BORDER}; border:none;")
        v.addWidget(sep)
        v.addSpacing(9)
        legend = QLabel(
            f"<span style='color:{Palette.ACCENT}'>&#9679;</span> Scheduled"
            f"&nbsp;&nbsp;&nbsp;<span style='color:{Palette.ARMED}'>&#9679;</span> Armed"
            f"&nbsp;&nbsp;&nbsp;<span style='color:{Palette.ONLINE}'>&#9679;</span> On air")
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setStyleSheet(f"font-size:11px; color:{Palette.TEXT_MUTED};")
        v.addWidget(legend)
        hint = QLabel("A day’s dots show what’s scheduled and its state. Single-click a day to "
                      "preview it below; double-click to open it on the timeline. A dot in a "
                      "day’s corner marks the day shown on the timeline.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size:11px; color:{Palette.TEXT_FAINT};")
        v.addSpacing(6); v.addWidget(hint)
        self._sync_title()

    def _nav(self, text: str, wide: bool = False) -> QPushButton:
        b = QPushButton(text); b.setCursor(Qt.CursorShape.PointingHandCursor)
        size = "padding:0 12px;" if wide else "min-width:30px; max-width:30px;"
        b.setStyleSheet(
            f"QPushButton {{ height:30px; {size} border:1px solid {Palette.BORDER}; border-radius:9px;"
            f" background:{Palette.SURFACE}; color:{Palette.ACCENT_INK}; font-size:13px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{Palette.INSET}; }}")
        return b

    def _sync_title(self) -> None:
        y, mo = self._grid.month()
        self._title.setText(
            f"<span style='font-size:18px; font-weight:600; color:{Palette.TEXT}'>{_MONTH_NAMES[mo - 1]}</span>"
            f" <span style='font-size:14px; color:{Palette.TEXT_FAINT}'>{y}</span>")

    def _shift(self, delta: int) -> None:
        y, mo = self._grid.month(); mo += delta
        if mo < 1:
            mo, y = 12, y - 1
        elif mo > 12:
            mo, y = 1, y + 1
        self._grid.set_month(y, mo); self._sync_title()

    def _go_today(self) -> None:
        t = date.today()
        self._grid.set_month(t.year, t.month)
        self._grid.set_selected(t)
        self._sync_title()
        self.previewed.emit(t)          # calendar Today = calendar + preview only (not the timeline)

    # proxied API
    def set_states(self, s) -> None: self._grid.set_states(s)
    def set_selected(self, d) -> None: self._grid.set_selected(d)
    def set_timeline(self, d) -> None: self._grid.set_timeline(d)
    def set_today(self, d) -> None: self._grid.set_today(d)
    def set_month(self, y, mo) -> None: self._grid.set_month(y, mo); self._sync_title()


class _CompactDayList(QFrame):
    """The compact preview of one day's plans (follows single clicks). Each row shows a
    state dot, the plan, its window, and an Arm / Unarm / Stop action of one fixed width
    so the time stamps line up."""

    arm_requested = pyqtSignal(str)
    stop_requested = pyqtSignal(str)     # Unarm (armed) or Stop (on air)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("compactCard")
        self.setStyleSheet(f"#compactCard {{ background:{Palette.SURFACE}; "
                           f"border:1px solid {Palette.BORDER}; border-radius:16px; }}")
        v = QVBoxLayout(self); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(0)
        head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 11); head.setSpacing(8)
        self._title = QLabel(); self._title.setTextFormat(Qt.TextFormat.RichText)
        self._title.setStyleSheet("font-size:14px; font-weight:600;")
        self._sub = QLabel(); self._sub.setStyleSheet(f"font-size:11.5px; color:{Palette.TEXT_FAINT};")
        head.addWidget(self._title); head.addStretch(1); head.addWidget(self._sub)
        v.addLayout(head)
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{Palette.BORDER}; border:none;")
        v.addWidget(sep)
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea{ background:transparent; border:none; }")
        self._body = QWidget(); self._body.setStyleSheet("background:transparent;")
        self._bv = QVBoxLayout(self._body)
        self._bv.setContentsMargins(0, 11, 0, 0); self._bv.setSpacing(7)
        self._bv.addStretch(1)
        self._scroll.setWidget(self._body)
        v.addWidget(self._scroll, 1)

    def set_day(self, d, rows: List[dict]) -> None:
        self._title.setText(
            f"{d.day} {_MONTH_NAMES[d.month - 1]} "
            f"<span style='color:{Palette.TEXT_FAINT}; font-weight:500'>· {_WDAY_NAMES[d.weekday()]}</span>")
        self._sub.setText(f"{len(rows)} plan{'s' if len(rows) != 1 else ''}" if rows else "")
        while self._bv.count() > 1:      # clear rows, keep the trailing stretch
            item = self._bv.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        if not rows:
            e = QLabel("Nothing scheduled this day.\nDouble-click the day to open it on the timeline.")
            e.setAlignment(Qt.AlignmentFlag.AlignCenter); e.setWordWrap(True)
            e.setStyleSheet(f"font-size:12.5px; color:{Palette.TEXT_FAINT}; padding:18px 8px;")
            self._bv.insertWidget(0, e)
            return
        for i, r in enumerate(rows):
            self._bv.insertWidget(i, self._row(r))

    def _row(self, r: dict) -> QWidget:
        state = r["state"]
        row = QFrame(); row.setObjectName("crow")
        # Scope to #crow: a bare `QFrame` rule would also style child QLabels (QLabel is a
        # QFrame subclass), boxing the name and time.
        row.setStyleSheet(f"QFrame#crow {{ background:{Palette.SURFACE_ALT}; border:1px solid "
                          f"{Palette.BORDER}; border-radius:10px; }}")
        h = QHBoxLayout(row); h.setContentsMargins(10, 7, 8, 7); h.setSpacing(9)
        h.addWidget(_Dot(_DOT_COLOR.get(state, Palette.IDLE),
                         pulse=state in ("on air", "onair"), diam=8))
        name = QLabel(r["name"]); name.setStyleSheet("font-size:13px; font-weight:500;")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        h.addWidget(name, 1)
        t = QLabel(f"{r['start'].strftime('%H:%M')}–{r['stop'].strftime('%H:%M')}")
        t.setFont(mono_font(11, 500)); t.setStyleSheet(f"color:{Palette.TEXT_MUTED};")
        h.addWidget(t)
        h.addWidget(self._action(r))
        return row

    def _action(self, r: dict) -> QPushButton:
        state, eid = r["state"], r["id"]
        if state in ("on air", "onair"):
            b = self._btn("Stop", Palette.CRASH, "#A82F2F")
            b.clicked.connect(lambda _=False, i=eid: self.stop_requested.emit(i))
        elif state == "armed":
            b = self._btn("Unarm", Palette.ARMED, "#9C6113")
            b.clicked.connect(lambda _=False, i=eid: self.stop_requested.emit(i))
        else:
            b = self._btn("Arm", Palette.ACCENT, Palette.ACCENT_INK)
            if r.get("armable"):
                b.clicked.connect(lambda _=False, i=eid: self.arm_requested.emit(i))
            else:
                b.setEnabled(False)
        return b

    @staticmethod
    def _btn(text: str, bg: str, hover: str) -> QPushButton:
        b = QPushButton(text); b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton {{ min-width:64px; height:24px; padding:0 10px; border:none; border-radius:12px;"
            f" background:{bg}; color:#fff; font-size:10.5px; font-weight:700; }}"
            f"QPushButton:hover {{ background:{hover}; }}"
            f"QPushButton:disabled {{ background:{Palette.IDLE}; }}")
        return b


# ── The tab ──────────────────────────────────────────────────────────────────

class TimelineTab(QWidget):
    def __init__(self, hub: Optional[DataHub] = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._store = ScheduleStore()
        self._plans = PlanStore()
        self._runs_by_host: Dict[str, List[m.SequenceRun]] = {}
        self._runs_pending = False
        self._runs_sig: object = None   # last-rendered active-run signature
        # Two independent days: the calendar/compact preview (single-click) and the big
        # timeline (double-click / defaults to today).
        self._selected_day: date = date.today()
        self._timeline_day: date = date.today()
        self._build()
        if self.hub is not None:
            self.hub.task_done.connect(self._on_task_done)
            self.hub.event_received.connect(self._on_event)
            # Safety net for a missed sequence webhook: fold the poller's periodic
            # run snapshot in so a finished run clears within a poll cycle (~3s)
            # rather than waiting on the 30s timer or a manual action.
            self.hub.fast_update.connect(self._on_fast_update)
        # Time marches on: re-evaluate 'armable' (start passing) and the now-line,
        # and re-poll run state, on a slow tick.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30_000)
        self._reload()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 14)
        outer.setSpacing(16)

        # ── top bar: title + Add plan ──
        top = QHBoxLayout(); top.setSpacing(16)
        tbox = QVBoxLayout(); tbox.setSpacing(3)
        h1 = QLabel("Schedule")
        h1.setStyleSheet(f"font-size:21px; font-weight:700; color:{Palette.TEXT};")
        sub = QLabel("Single-click a day to preview it below; double-click to open its full "
                     "timeline on the right. The timeline opens on today and holds a day until "
                     "you open another.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"font-size:12.5px; color:{Palette.TEXT_MUTED};")
        tbox.addWidget(h1); tbox.addWidget(sub)
        top.addLayout(tbox, 1)
        self._add_btn = QPushButton("＋  Add plan")
        self._add_btn.setObjectName("primary")
        self._add_btn.clicked.connect(self._on_add)
        abox = QVBoxLayout(); abox.addStretch(1); abox.addWidget(self._add_btn)
        top.addLayout(abox)
        outer.addLayout(top)

        # ── split: [calendar + compact list] | [big timeline] ──
        split = QHBoxLayout(); split.setSpacing(20)

        self._cal = _MonthCalendar()
        self._cal.previewed.connect(self._on_preview)
        self._cal.opened.connect(self._on_open)
        self._compact = _CompactDayList()
        self._compact.arm_requested.connect(self._on_arm)
        self._compact.stop_requested.connect(self._on_stop)
        left = QVBoxLayout(); left.setSpacing(16); left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(self._cal)
        left.addWidget(self._compact, 1)
        left_w = QWidget(); left_w.setFixedWidth(392); left_w.setLayout(left)
        split.addWidget(left_w)

        self._planner = _DayPlanner()
        self._planner.block_activated.connect(self._on_block)
        self._planner.arm_requested.connect(self._on_arm)
        self._planner.stop_requested.connect(self._on_stop)
        split.addWidget(self._build_timeline_card(), 1)
        outer.addLayout(split, 1)

        # ── status line (arm/stop progress + totals) ──
        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size:11px; color:{Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

    def _build_timeline_card(self) -> QWidget:
        """The right pane: a header naming the day on the timeline (with a Today badge /
        Back-to-today) above the scrollable day-planner."""
        card = QFrame(); card.setObjectName("tlCard")
        card.setStyleSheet(f"#tlCard {{ background:{Palette.SURFACE}; "
                           f"border:1px solid {Palette.BORDER}; border-radius:16px; }}")
        cv = QVBoxLayout(card); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(0)

        head = QWidget()
        hh = QHBoxLayout(head); hh.setContentsMargins(20, 16, 18, 14); hh.setSpacing(12)
        hbox = QVBoxLayout(); hbox.setSpacing(3)
        eb = QLabel("ON THE TIMELINE")
        eb.setStyleSheet(f"font-size:10px; font-weight:700; letter-spacing:1px; color:{Palette.TEXT_FAINT};")
        drow = QHBoxLayout(); drow.setSpacing(9)
        self._tl_date = QLabel("—")
        self._tl_date.setStyleSheet(f"font-size:19px; font-weight:600; color:{Palette.TEXT};")
        self._tl_badge = QLabel("TODAY")
        self._tl_badge.setStyleSheet(f"font-size:10px; font-weight:700; color:#fff; "
                                     f"background:{Palette.ACCENT}; border-radius:8px; padding:2px 8px;")
        drow.addWidget(self._tl_date); drow.addWidget(self._tl_badge); drow.addStretch(1)
        self._tl_sub = QLabel("")
        self._tl_sub.setStyleSheet(f"font-size:12.5px; color:{Palette.TEXT_MUTED};")
        hbox.addWidget(eb); hbox.addLayout(drow); hbox.addWidget(self._tl_sub)
        hh.addLayout(hbox, 1)
        self._tl_back = QPushButton("Back to today")
        self._tl_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tl_back.setStyleSheet(
            f"QPushButton {{ height:30px; padding:0 13px; border:1px solid {Palette.BORDER};"
            f" border-radius:9px; background:{Palette.SURFACE}; color:{Palette.ACCENT_INK};"
            f" font-size:12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{Palette.ACCENT_SOFT}; border-color:{Palette.ACCENT_SOFT}; }}")
        self._tl_back.clicked.connect(self._go_timeline_today)
        hh.addWidget(self._tl_back, 0, Qt.AlignmentFlag.AlignTop)
        cv.addWidget(head)
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{Palette.BORDER}; border:none;")
        cv.addWidget(sep)

        self._tl_scroll = QScrollArea(); self._tl_scroll.setWidgetResizable(True)
        self._tl_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._tl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tl_scroll.setStyleSheet("QScrollArea{ background:transparent; border:none; }")
        self._tl_scroll.setWidget(self._planner)
        cv.addWidget(self._tl_scroll, 1)
        return card

    # ── Shown / reload ─────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._reload()
        self._refresh_runs()

    def _tick(self) -> None:
        self._refresh_runs()
        self._refresh_state_views()   # 'armable' + the now-line depend on the wall clock

    def _reload(self) -> None:
        self._store.load()
        self._plans.load()
        self._refresh_calendar()
        self._refresh_compact()
        self._refresh_planner(scroll=True)

    def _refresh_state_views(self) -> None:
        """Repaint the dot/action states everywhere without disturbing the timeline scroll."""
        self._refresh_calendar()
        self._refresh_compact()
        self._refresh_planner(scroll=False)

    def _refresh_runs(self) -> None:
        if self.hub is None or self._runs_pending or len(self.hub.fleet) == 0:
            return
        self._runs_pending = True
        self.hub.run_async("tl_runs", lambda: self.hub.fleet.list_runs_all())

    def _selected_date(self) -> date:
        return self._selected_day

    def _plan_for(self, entry: m.ScheduledPlan) -> Optional[m.Plan]:
        """This slot's effective plan: its own edited copy if it has one, else the
        library plan it was seeded from (None if that's since been deleted)."""
        return entry.plan or self._plans.get(entry.plan_id)

    def _resolve(self, entry: m.ScheduledPlan) -> Tuple[str, str]:
        plan = self._plan_for(entry)
        if plan is not None:
            return plan.name or plan.id, plan.description
        return (entry.plan_name or "(unknown plan)"), "plan no longer exists"

    # ── Calendar highlighting ──────────────────────────────────────────────────

    def _refresh_calendar(self) -> None:
        """Recompute each day's plan states (for the dots) and push the today/selected/
        timeline markers to the calendar."""
        states: Dict[date, List[str]] = {}
        for entry in self._store.entries():
            s, e = _parse(entry.start), _parse(entry.stop)
            if s is None or e is None:
                continue
            st = self._entry_state(entry)
            d, last = s.date(), e.date()
            while d <= last:
                states.setdefault(d, []).append(st)
                d += timedelta(days=1)
        self._cal.set_states(states)
        self._cal.set_today(date.today())
        self._cal.set_selected(self._selected_day)
        self._cal.set_timeline(self._timeline_day)

    # ── Day view ────────────────────────────────────────────────────────────────

    def _entries_on(self, d: date) -> List[dict]:
        """Rows for the plans that touch day `d`, sorted by start."""
        day_start = datetime.combine(d, time(0, 0))
        day_end = day_start + timedelta(days=1)
        now = datetime.now()
        rows = []
        for entry in self._store.entries():
            s, e = _parse(entry.start), _parse(entry.stop)
            if s is None or e is None or e <= day_start or s >= day_end:
                continue
            name, desc = self._resolve(entry)
            rows.append({
                "id": entry.id, "name": name, "desc": desc, "start": s, "stop": e,
                "state": self._entry_state(entry),
                "armable": self._plan_for(entry) is not None and now < s,
            })
        rows.sort(key=lambda r: r["start"])
        return rows

    def _refresh_compact(self) -> None:
        self._compact.set_day(self._selected_day, self._entries_on(self._selected_day))

    def _refresh_planner(self, scroll: bool = False) -> None:
        d = self._timeline_day
        blocks = self._entries_on(d)
        self._planner.set_day(d, blocks)

        is_today = (d == date.today())
        self._tl_date.setText(f"{_WDAY_NAMES[d.weekday()]}, {d.day} {_MONTH_NAMES[d.month - 1]} {d.year}")
        self._tl_badge.setVisible(is_today)
        self._tl_back.setVisible(not is_today)
        self._tl_sub.setText(
            f"{len(blocks)} plan{'s' if len(blocks) != 1 else ''}" if blocks else "No plans scheduled")
        total = len(self._store.entries())
        self._status.setText(f"{total} plan(s) scheduled in total" if total
                             else "No plans scheduled yet. Click “Add plan” to place one.")
        self._runs_sig = self._runs_signature()   # view now matches this run picture

        if scroll:
            if is_today:
                anchor = self._planner._to_y(datetime.now())
            elif blocks:
                anchor = min(self._planner._to_y(b["start"]) for b in blocks)
            else:
                anchor = self._planner._to_y(datetime.combine(d, time(8, 0)))
            QTimer.singleShot(0, lambda a=anchor: self._tl_scroll.verticalScrollBar()
                              .setValue(max(0, int(a) - 90)))

    # ── Preview / open / today ───────────────────────────────────────────────────

    def _on_preview(self, d: date) -> None:
        """Single click: move only the calendar selection + compact preview."""
        self._selected_day = d
        self._cal.set_selected(d)
        self._refresh_compact()

    def _on_open(self, d: date) -> None:
        """Double click: load the day onto the big timeline (and preview it too)."""
        self._selected_day = d
        self._timeline_day = d
        self._cal.set_selected(d)
        self._cal.set_timeline(d)
        self._refresh_compact()
        self._refresh_planner(scroll=True)

    def _go_timeline_today(self) -> None:
        """Back-to-today: reset only the big timeline (the calendar keeps its day)."""
        self._timeline_day = date.today()
        self._cal.set_timeline(self._timeline_day)
        self._refresh_planner(scroll=True)

    # ── Run-state matching ──────────────────────────────────────────────────────

    def _entry_runs(self, entry: m.ScheduledPlan) -> List[tuple]:
        """[(hostname, run), …] of active runs belonging to this scheduled entry —
        same plan, and armed at a time inside this entry's window."""
        start_utc, stop_utc = _to_utc(entry.start), _to_utc(entry.stop)
        if start_utc is None or stop_utc is None:
            return []
        lo = start_utc - timedelta(seconds=5)
        hi = stop_utc + timedelta(seconds=5)
        out = []
        for host, runs in self._runs_by_host.items():
            for r in runs:
                if r.plan_id != entry.plan_id or r.state not in _ACTIVE:
                    continue
                oa = _parse(r.on_air_at)
                if oa is None:
                    continue
                if oa.tzinfo is None:
                    oa = oa.replace(tzinfo=timezone.utc)
                oa = oa.astimezone(timezone.utc)
                if lo <= oa <= hi:
                    out.append((host, r))
        return out

    def _entry_state(self, entry: m.ScheduledPlan) -> str:
        if self._plan_for(entry) is None:
            return "missing"
        runs = self._entry_runs(entry)
        if not runs:
            return "idle"
        if any(r.state == m.SequenceState.RUNNING and r.on_air_actual for _h, r in runs):
            return "on air"
        return "armed"

    # ── Add / edit / remove ─────────────────────────────────────────────────────

    def _on_add(self) -> None:
        plans = self._plans.plans()
        if not plans:
            QMessageBox.information(
                self, "No plans", "There are no plans yet. Create one in the Plans tab first.")
            return
        dlg = _ScheduleDialog(plans, default_day=self._selected_date(),
                              hub=self.hub, parent=self.window())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_entry is not None:
            self._store.upsert(dlg.result_entry)
            self._sync_units()
            self._jump_to(dlg.result_entry)
            self._reload()

    def _on_block(self, entry_id: str) -> None:
        entry = self._store.get(entry_id)
        if entry is None:
            return
        dlg = _ScheduleDialog(self._plans.plans(), entry=entry,
                              hub=self.hub, parent=self.window())
        r = dlg.exec()
        if r == _ScheduleDialog.REMOVE:
            self._store.delete(entry_id)
            self._sync_units()
            self._reload()
        elif r == QDialog.DialogCode.Accepted and dlg.result_entry is not None:
            self._store.upsert(dlg.result_entry)
            self._sync_units()
            self._jump_to(dlg.result_entry)
            self._reload()

    def _sync_units(self) -> None:
        """Replicate the just-edited schedule (and plans) out to every unit so
        their copies stay identical to the PC. No-op when running head-less."""
        if self.hub is not None:
            self.hub.sync_state_to_units()

    def _jump_to(self, entry: m.ScheduledPlan) -> None:
        """After an add/edit, focus the plan's day on the calendar, compact list and the
        big timeline (the subsequent _reload repaints them)."""
        s = _parse(entry.start)
        if s is not None:
            d = s.date()
            self._selected_day = d
            self._timeline_day = d
            self._cal.set_month(d.year, d.month)

    # ── Arm / stop ──────────────────────────────────────────────────────────────

    def _on_arm(self, entry_id: str) -> None:
        if getattr(self, "_arm_busy", False):
            return                       # a preflight is already in flight — no double-arm
        entry = self._store.get(entry_id)
        if entry is None or self.hub is None:
            return
        plan = self._plan_for(entry)
        if plan is None or not plan.items:
            QMessageBox.warning(self, "Cannot arm", "This plan no longer exists or has no sequences.")
            return
        if datetime.now() >= (_parse(entry.start) or datetime.now()):
            QMessageBox.information(self, "Window started",
                                   "This plan's start time has passed. Arming mid-window "
                                   "isn't supported yet.")
            return
        missing = [i for i in plan.items if i.hostname not in self.hub.fleet]
        if missing:
            names = ", ".join(i.unit_label or i.hostname for i in missing)
            QMessageBox.warning(self, "Cannot arm", f"These units are not in the fleet: {names}")
            return
        hostnames = sorted({i.hostname for i in plan.items})
        self._arm_busy = True
        self._status.setText(f"pre-flight for {plan.name}…")
        self.hub.run_async(f"tl_preflight:{entry.id}",
                           lambda: self.hub.fleet.clock_skew(hostnames))

    def _finish_preflight(self, entry: m.ScheduledPlan, result) -> None:
        plan = self._plan_for(entry)
        if plan is None:
            return
        max_skew = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        start_utc, stop_utc = _to_utc(entry.start), _to_utc(entry.stop)
        s, e = _parse(entry.start), _parse(entry.stop)
        skew_note = ""
        if max_skew is not None and max_skew > CLOCK_WARN_SKEW_S:
            skew_note = (f"\n\n⚠ Unit clocks differ by {max_skew:.1f}s — a shared on-air time "
                         f"depends on synced clocks; units may differ by that much.")
        n_units = len({i.hostname for i in plan.items})
        if QMessageBox.question(
            self, "Arm plan",
            f"Arm “{plan.name}” on {n_units} unit(s)?\n\n"
            f"On air {s.strftime('%H:%M')} → {e.strftime('%H:%M')} "
            f"({s.strftime('%a %d %b')})."
            f"{skew_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            self._status.setText("arm cancelled")
            return
        self._status.setText(f"arming {plan.name}…")
        self.hub.run_async(
            f"tl_arm:{entry.id}",
            lambda: _arm_scheduled(self.hub.fleet, plan, start_utc, stop_utc))

    def _on_stop(self, entry_id: str) -> None:
        entry = self._store.get(entry_id)
        if entry is None or self.hub is None:
            return
        runs = [(h, r.id) for h, r in self._entry_runs(entry)]
        if not runs:
            self._refresh_runs()
            return
        self._status.setText(f"stopping {entry.plan_name or 'plan'}…")
        self.hub.run_async(f"tl_stop:{entry.id}",
                           lambda: _stop_runs(self.hub.fleet, runs))

    # ── Live events + result routing ────────────────────────────────────────────

    def _on_event(self, ev) -> None:
        if isinstance(ev, m.SequenceWebhook):
            self._refresh_runs()

    def _runs_signature(self) -> frozenset:
        """A cheap fingerprint of the active-run picture — redraw the day only when
        run state actually changes, not on every poll tick."""
        return frozenset(
            (host, r.id, r.state, bool(getattr(r, "on_air_actual", None)))
            for host, rs in self._runs_by_host.items() for r in rs)

    def _on_fast_update(self, snap) -> None:
        runs = getattr(snap, "runs", None)
        if not isinstance(runs, dict) or not runs:
            return
        # Merge per host — a scoped refresh carries only one unit's runs.
        for host, val in runs.items():
            self._runs_by_host[host] = val if isinstance(val, list) else []
        if self._runs_signature() != self._runs_sig and self.isVisible():
            self._refresh_state_views()   # recomputes and stores the signature

    def _on_task_done(self, label: str, result) -> None:
        if label == "tl_runs":
            self._runs_pending = False
            by_host: Dict[str, List[m.SequenceRun]] = {}
            if isinstance(result, dict):
                for host, val in result.items():
                    by_host[host] = val if isinstance(val, list) else []
            self._runs_by_host = by_host
            self._refresh_state_views()
            return
        if ":" not in label or not label.startswith("tl_"):
            return
        op, entry_id = label.split(":", 1)
        entry = self._store.get(entry_id)
        if op == "tl_preflight":
            self._arm_busy = False       # preflight done; the modal dialog guards the rest
            if entry is not None:
                self._finish_preflight(entry, result)
        elif op == "tl_arm":
            self._report_arm(result)
            self._refresh_runs()
        elif op == "tl_stop":
            if isinstance(result, list):
                bad = [(rid, e) for rid, e in result if e is not None]
                if bad:
                    QMessageBox.warning(self, "Stop — some runs failed",
                                        "\n".join(f"• {rid}: {e}" for rid, e in bad))
                    self._status.setText("stop: some runs failed")
                else:
                    self._status.setText("stopped")
            self._refresh_runs()

    def _report_arm(self, result) -> None:
        if isinstance(result, Exception) or not isinstance(result, list):
            self._status.setText("arm failed")
            QMessageBox.warning(self, "Arm failed", f"{result}")
            return
        ok = [it for it, run, err in result if err is None]
        bad = [(it, err) for it, run, err in result if err is not None]
        if bad:
            lines = "\n".join(f"• {it.unit_label or it.hostname} / {it.sequence_name}: {err}"
                              for it, err in bad)
            QMessageBox.warning(self, "Arm — partial",
                                f"Armed {len(ok)} of {len(result)} sequence(s).\n\nFailed:\n{lines}")
            self._status.setText("arm: some units failed")
        else:
            self._status.setText(f"armed {len(ok)} sequence(s)")
