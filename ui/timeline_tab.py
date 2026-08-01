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
can be built days in advance. Selecting a plan block edits or removes it.

Persistence is the local ScheduleStore (schedule.json); plan names/descriptions
are resolved live from the PlanStore. Execution — actually arming a plan when its
time arrives — is a later step; this tab is the schedule + view.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QDate, QDateTime, QSize, Qt, QRectF, QTime, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import models as m
from state import PlanStore, ScheduleStore, new_scheduled_id
from .qt_adapter import DataHub
from .theme import Palette


def _parse(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


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
                 default_day: Optional[date] = None, parent=None):
        super().__init__(parent)
        self._plans = plans
        self._entry = entry
        self.result_entry: Optional[m.ScheduledPlan] = None

        self.setWindowTitle("Edit scheduled plan" if entry else "Add plan to timeline")
        self.setMinimumWidth(420)
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
        )
        self.accept()


# ── The vertical day-planner ─────────────────────────────────────────────────

class _DayPlanner(QWidget):
    """A single day, hours running top→bottom, with plan blocks placed by time and
    packed into side-by-side columns where they overlap."""

    block_activated = pyqtSignal(str)   # scheduled-plan id

    HOUR_PX = 52
    AXIS_W = 58
    TOP_PAD = 10
    BOT_PAD = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self._date = date.today()
        self._blocks: List[dict] = []
        self._rects: Dict[str, QRectF] = {}
        self.setMouseTracking(True)
        self.setMinimumWidth(380)

    def content_height(self) -> int:
        return self.TOP_PAD + 24 * self.HOUR_PX + self.BOT_PAD

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, self.content_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(380, self.content_height())

    # ── Data ───────────────────────────────────────────────────────────────────

    def set_day(self, d: date, blocks: List[Tuple[str, str, str, datetime, datetime]]) -> None:
        """blocks: (entry_id, name, description, start_dt, stop_dt)."""
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

    def _layout(self, blocks) -> None:
        day_start, day_end = self._day_bounds()
        vis: List[dict] = []
        for (eid, name, desc, s, e) in blocks:
            if e <= day_start or s >= day_end:
                continue
            vis.append({
                "id": eid, "name": name, "desc": desc, "start": s, "stop": e,
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
        p.setPen(QPen(QColor(Palette.ACCENT), 1.5))
        p.setBrush(QBrush(QColor(Palette.ACCENT_SOFT)))
        p.drawRoundedRect(rect, 7, 7)
        # left accent stripe
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(Palette.ACCENT)))
        p.drawRoundedRect(QRectF(rect.left(), rect.top() + 3, 3.5, rect.height() - 6), 1.5, 1.5)

        inner = rect.adjusted(12, 5, -8, -4)
        y = inner.top()
        p.setFont(name_font)
        p.setPen(QColor(Palette.TEXT))
        fm = QFontMetrics(name_font)
        name = fm.elidedText(b["name"], Qt.TextElideMode.ElideRight, int(inner.width()))
        p.drawText(QRectF(inner.left(), y, inner.width(), 16),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
        y += 16
        if rect.height() >= 40:
            pre = "…" if b["clip_top"] else ""
            post = "…" if b["clip_bot"] else ""
            trange = f"{pre}{b['start'].strftime('%H:%M')} – {b['stop'].strftime('%H:%M')}{post}"
            p.setFont(meta_font)
            p.setPen(QColor(Palette.ACCENT))
            p.drawText(QRectF(inner.left(), y, inner.width(), 13),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), trange)
            y += 14
        if b["desc"] and rect.bottom() - y > 14:
            p.setFont(desc_font)
            p.setPen(QColor(Palette.TEXT_MUTED))
            p.drawText(QRectF(inner.left(), y, inner.width(), rect.bottom() - y - 2),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop |
                           Qt.TextFlag.TextWordWrap), b["desc"])

    # ── Interaction ────────────────────────────────────────────────────────────

    def mouseMoveEvent(self, e):  # noqa: N802
        over = any(r.contains(e.position()) for r in self._rects.values())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        for eid, r in self._rects.items():
            if r.contains(e.position()):
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
        self._build()
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

    def _reload(self) -> None:
        self._store.load()
        self._plans.load()
        self._refresh_calendar()
        self._refresh_day()

    def _selected_date(self) -> date:
        qd = self._cal.selectedDate()
        return date(qd.year(), qd.month(), qd.day())

    def _resolve(self, entry: m.ScheduledPlan) -> Tuple[str, str]:
        plan = self._plans.get(entry.plan_id)
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
        blocks = []
        for entry in self._store.entries():
            s, e = _parse(entry.start), _parse(entry.stop)
            if s is None or e is None or e <= day_start or s >= day_end:
                continue
            name, desc = self._resolve(entry)
            blocks.append((entry.id, name, desc, s, e))
        self._planner.set_day(d, blocks)

        total = len(self._store.entries())
        n_today = len(blocks)
        self._status.setText(
            f"{n_today} plan(s) on this day · {total} scheduled in total" if total
            else "No plans scheduled yet. Click “Add plan…” to place one.")

    def _on_date_changed(self) -> None:
        self._refresh_day()

    # ── Add / edit / remove ─────────────────────────────────────────────────────

    def _on_add(self) -> None:
        plans = self._plans.plans()
        if not plans:
            QMessageBox.information(
                self, "No plans", "There are no plans yet. Create one in the Plans tab first.")
            return
        dlg = _ScheduleDialog(plans, default_day=self._selected_date(), parent=self.window())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_entry is not None:
            self._store.upsert(dlg.result_entry)
            self._jump_to(dlg.result_entry)
            self._reload()

    def _on_block(self, entry_id: str) -> None:
        entry = self._store.get(entry_id)
        if entry is None:
            return
        dlg = _ScheduleDialog(self._plans.plans(), entry=entry, parent=self.window())
        r = dlg.exec()
        if r == _ScheduleDialog.REMOVE:
            self._store.delete(entry_id)
            self._reload()
        elif r == QDialog.DialogCode.Accepted and dlg.result_entry is not None:
            self._store.upsert(dlg.result_entry)
            self._jump_to(dlg.result_entry)
            self._reload()

    def _jump_to(self, entry: m.ScheduledPlan) -> None:
        s = _parse(entry.start)
        if s is not None:
            self._cal.setSelectedDate(QDate(s.year, s.month, s.day))
