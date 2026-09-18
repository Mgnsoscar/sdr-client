"""Schedule day-planner: vertical zoom (Ctrl+scroll or the header +/− buttons).

The `_DayPlanner` draws a day top→bottom at `HOUR_PX` pixels per hour; zoom changes that
scale (clamped), re-lays out at the new scale, and — when hosted in a scroll area — keeps the
time under the cursor (or the viewport centre) pinned so the zoom feels anchored.
"""
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

import ui.timeline_tab as tt

_app = QApplication.instance() or QApplication(sys.argv)

_DAY = date(2026, 9, 18)


def _planner_with_a_block():
    p = tt._DayPlanner()
    p.set_day(_DAY, [{
        "id": "b1", "name": "Plan A", "desc": "", "state": "idle", "armable": True,
        "start": datetime.combine(_DAY, time(8, 0)),
        "stop": datetime.combine(_DAY, time(9, 0)),
    }])
    return p


# ── fake scroll host (real viewport height + a scrollbar that records setValue) ──

class _FakeSB:
    def __init__(self):
        self._v = 0
    def value(self):
        return self._v
    def setValue(self, v):
        self._v = int(v)


class _FakeVP:
    def __init__(self, h):
        self._h = h
    def height(self):
        return self._h


class _FakeScroll:
    def __init__(self, vp_h=400, scroll=0):
        self._sb = _FakeSB(); self._sb.setValue(scroll); self._vp = _FakeVP(vp_h)
    def verticalScrollBar(self):
        return self._sb
    def viewport(self):
        return self._vp


def _wheel(dy, y, ctrl=True):
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    return QWheelEvent(QPointF(30.0, float(y)), QPointF(30.0, float(y)),
                       QPoint(0, 0), QPoint(0, dy),
                       Qt.MouseButton.NoButton, mods,
                       Qt.ScrollPhase.NoScrollPhase, False)


# ── scale + content height ───────────────────────────────────────────────────────

def test_content_height_and_to_y_scale_with_the_zoom():
    p = _planner_with_a_block()
    base = p.HOUR_PX
    y8_base = p._to_y(datetime.combine(_DAY, time(8, 0)))
    h_base = p.content_height()

    p.set_hour_px(base * 2)
    assert p.HOUR_PX == base * 2
    # 24 h axis doubles (minus the fixed top/bottom pads scale linearly with the hour span)
    assert p.content_height() > h_base
    assert p.content_height() - tt._DayPlanner.TOP_PAD - tt._DayPlanner.BOT_PAD == 24 * base * 2
    # a time sits twice as far down (the top pad aside)
    y8_2x = p._to_y(datetime.combine(_DAY, time(8, 0)))
    assert abs((y8_2x - p.TOP_PAD) - 2 * (y8_base - p.TOP_PAD)) < 0.5


def test_set_hour_px_clamps_to_the_min_and_max():
    p = _planner_with_a_block()
    p.set_hour_px(9999)
    assert p.HOUR_PX == tt._DayPlanner.HOUR_PX_MAX
    p.set_hour_px(1)
    assert p.HOUR_PX == tt._DayPlanner.HOUR_PX_MIN


def test_zoom_by_multiplies_the_scale():
    p = _planner_with_a_block()
    base = p.HOUR_PX
    p.zoom_by(1.25)
    assert abs(p.HOUR_PX - base * 1.25) < 1e-6
    p.zoom_by(0.8)
    assert abs(p.HOUR_PX - base) < 1e-3          # 1.25 * 0.8 == 1.0


def test_from_y_inverts_to_y():
    p = _planner_with_a_block()
    for hh in (0, 6, 12, 18, 23):
        dt = datetime.combine(_DAY, time(hh, 0))
        assert abs((p._from_y(p._to_y(dt)) - dt).total_seconds()) < 1.0


def test_zoom_survives_a_day_refresh():
    p = _planner_with_a_block()
    p.set_hour_px(120)
    # a later set_day (a refresh) re-lays out but must not reset the zoom
    p.set_day(_DAY, [])
    assert p.HOUR_PX == 120


# ── anchoring (keep a time pinned under the cursor / viewport centre) ─────────────

def test_zoom_keeps_the_anchor_time_at_a_fixed_viewport_y():
    p = _planner_with_a_block()
    scroll = _FakeScroll(vp_h=400, scroll=300)
    p.set_scroll_area(scroll)
    keep_dt = datetime.combine(_DAY, time(10, 0))
    vp_y = 150.0                                  # keep 10:00 at 150 px down the viewport
    p.set_hour_px(p.HOUR_PX * 1.7, keep_dt=keep_dt, keep_vp_y=vp_y)
    # 10:00's new content-y minus the scroll value == the requested viewport y
    assert abs((p._to_y(keep_dt) - scroll.verticalScrollBar().value()) - vp_y) < 1.0


def test_default_anchor_is_the_viewport_centre():
    p = _planner_with_a_block()
    scroll = _FakeScroll(vp_h=400, scroll=500)
    p.set_scroll_area(scroll)
    centre_dt = p._from_y(scroll.verticalScrollBar().value() + 200)   # 200 == vp_h/2
    p.zoom_by(1.5)                                 # no explicit anchor → viewport centre
    assert abs((p._to_y(centre_dt) - scroll.verticalScrollBar().value()) - 200) < 1.0


# ── wheel gesture ────────────────────────────────────────────────────────────────

def test_ctrl_wheel_zooms_in_and_out():
    p = _planner_with_a_block()
    p.set_scroll_area(_FakeScroll())
    base = p.HOUR_PX
    e_in = _wheel(dy=120, y=100, ctrl=True)
    p.wheelEvent(e_in)
    assert p.HOUR_PX > base and e_in.isAccepted()
    up = p.HOUR_PX
    p.wheelEvent(_wheel(dy=-120, y=100, ctrl=True))
    assert p.HOUR_PX < up


def test_plain_wheel_does_not_zoom():
    p = _planner_with_a_block()
    p.set_scroll_area(_FakeScroll())
    base = p.HOUR_PX
    p.wheelEvent(_wheel(dy=120, y=100, ctrl=False))
    assert p.HOUR_PX == base          # no zoom — the parent scroll area handles the scroll
