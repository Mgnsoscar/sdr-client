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

from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QDate, QDateTime, QSize, Qt, QRectF, QTimer, QTime, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from state import PlanStore, ScheduleStore, new_scheduled_id
from .plan_editor import PlanEditorDialog
from .qt_adapter import DataHub
from .theme import Palette

_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING)
CLOCK_WARN_SKEW_S = 1.0


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
    for item in plan.items:
        on_air = (start_utc + timedelta(seconds=item.on_air_offset_s)).isoformat()
        off_air = (stop_utc + timedelta(seconds=item.off_air_offset_s)).isoformat()
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
    BTN_W = 46
    BTN_H = 18

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
        vis: List[dict] = []
        for b in blocks:
            s, e = b["start"], b["stop"]
            if e <= day_start or s >= day_end:
                continue
            vis.append({
                **b,
                "vs": max(s, day_start), "ve": min(e, day_end),
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
            cluster_end = vis[i]["ve"]
            j = i + 1
            while j < n and vis[j]["vs"] < cluster_end:
                cluster.append(vis[j])
                cluster_end = max(cluster_end, vis[j]["ve"])
                j += 1
            col_ends: List[datetime] = []
            for b in cluster:
                for c, end in enumerate(col_ends):
                    if b["vs"] >= end:
                        col_ends[c] = b["ve"]
                        b["col"] = c
                        break
                else:
                    b["col"] = len(col_ends)
                    col_ends.append(b["ve"])
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
            rect = QRectF(x, y_top + 1, colw - 6, max(24.0, y_bot - y_top - 2))
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
        if state in ("armed", "on air"):
            action, text, col = "stop", "Stop", Palette.CRASH
        elif state == "idle" and b.get("armable"):
            action, text, col = "arm", "Arm", Palette.ACCENT
        else:
            return 0
        if rect.width() < self.BTN_W + 40 or rect.height() < 22:
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


# ── The tab ──────────────────────────────────────────────────────────────────

class TimelineTab(QWidget):
    def __init__(self, hub: Optional[DataHub] = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._store = ScheduleStore()
        self._plans = PlanStore()
        self._marked: List[QDate] = []
        self._runs_by_host: Dict[str, List[m.SequenceRun]] = {}
        self._runs_pending = False
        self._build()
        if self.hub is not None:
            self.hub.task_done.connect(self._on_task_done)
            self.hub.event_received.connect(self._on_event)
        # Time marches on: re-evaluate 'armable' (start passing) and the now-line,
        # and re-poll run state, on a slow tick.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30_000)
        self._reload()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        body = QHBoxLayout()
        body.setSpacing(14)

        # Left: calendar + add.
        left = QVBoxLayout()
        left.setSpacing(8)
        self._cal = QCalendarWidget()
        self._cal.setGridVisible(True)
        self._cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self._cal.setFixedWidth(300)
        self._cal.selectionChanged.connect(self._on_date_changed)
        left.addWidget(self._cal)

        legend = QLabel("Highlighted dates have plans scheduled.")
        legend.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        legend.setWordWrap(True)
        left.addWidget(legend)

        self._add_btn = QPushButton("Add plan…")
        self._add_btn.setObjectName("primary")
        self._add_btn.clicked.connect(self._on_add)
        left.addWidget(self._add_btn)
        left.addStretch(1)
        body.addLayout(left)

        # Right: day header + scrollable day-planner.
        right = QVBoxLayout()
        right.setSpacing(6)
        self._day_lbl = QLabel("")
        self._day_lbl.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {Palette.TEXT};")
        right.addWidget(self._day_lbl)

        self._planner = _DayPlanner()
        self._planner.block_activated.connect(self._on_block)
        self._planner.arm_requested.connect(self._on_arm)
        self._planner.stop_requested.connect(self._on_stop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._planner)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        right.addWidget(scroll, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        right.addWidget(self._status)
        body.addLayout(right, stretch=1)

        outer.addLayout(body)

    # ── Shown / reload ─────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._reload()
        self._refresh_runs()

    def _tick(self) -> None:
        self._refresh_runs()
        self._refresh_day()   # 'armable' + the now-line depend on the wall clock

    def _reload(self) -> None:
        self._store.load()
        self._plans.load()
        self._refresh_calendar()
        self._refresh_day()

    def _refresh_runs(self) -> None:
        if self.hub is None or self._runs_pending or len(self.hub.fleet) == 0:
            return
        self._runs_pending = True
        self.hub.run_async("tl_runs", lambda: self.hub.fleet.list_runs_all())

    def _selected_date(self) -> date:
        qd = self._cal.selectedDate()
        return date(qd.year(), qd.month(), qd.day())

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
        default = QTextCharFormat()
        for qd in self._marked:
            self._cal.setDateTextFormat(qd, default)
        self._marked = []

        fmt = QTextCharFormat()
        fmt.setBackground(QColor(Palette.ACCENT_SOFT))
        fmt.setForeground(QColor(Palette.ACCENT))
        f = fmt.font(); f.setBold(True); fmt.setFont(f)
        seen: set = set()
        for entry in self._store.entries():
            s, e = _parse(entry.start), _parse(entry.stop)
            if s is None or e is None:
                continue
            d, last = s.date(), e.date()
            while d <= last:
                key = (d.year, d.month, d.day)
                if key not in seen:
                    qd = QDate(d.year, d.month, d.day)
                    self._cal.setDateTextFormat(qd, fmt)
                    self._marked.append(qd)
                    seen.add(key)
                d += timedelta(days=1)

    # ── Day view ────────────────────────────────────────────────────────────────

    def _refresh_day(self) -> None:
        d = self._selected_date()
        self._day_lbl.setText(d.strftime("%A, %d %B %Y"))
        day_start = datetime.combine(d, time(0, 0))
        day_end = day_start + timedelta(days=1)
        now = datetime.now()
        blocks = []
        for entry in self._store.entries():
            s, e = _parse(entry.start), _parse(entry.stop)
            if s is None or e is None or e <= day_start or s >= day_end:
                continue
            name, desc = self._resolve(entry)
            blocks.append({
                "id": entry.id, "name": name, "desc": desc, "start": s, "stop": e,
                "state": self._entry_state(entry),
                "armable": self._plan_for(entry) is not None and now < s,
            })
        self._planner.set_day(d, blocks)

        total = len(self._store.entries())
        self._status.setText(
            f"{len(blocks)} plan(s) on this day · {total} scheduled in total" if total
            else "No plans scheduled yet. Click “Add plan…” to place one.")

    def _on_date_changed(self) -> None:
        self._refresh_day()

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
        s = _parse(entry.start)
        if s is not None:
            self._cal.setSelectedDate(QDate(s.year, s.month, s.day))

    # ── Arm / stop ──────────────────────────────────────────────────────────────

    def _on_arm(self, entry_id: str) -> None:
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

    def _on_task_done(self, label: str, result) -> None:
        if label == "tl_runs":
            self._runs_pending = False
            by_host: Dict[str, List[m.SequenceRun]] = {}
            if isinstance(result, dict):
                for host, val in result.items():
                    by_host[host] = val if isinstance(val, list) else []
            self._runs_by_host = by_host
            self._refresh_day()
            return
        if ":" not in label or not label.startswith("tl_"):
            return
        op, entry_id = label.split(":", 1)
        entry = self._store.get(entry_id)
        if op == "tl_preflight":
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
