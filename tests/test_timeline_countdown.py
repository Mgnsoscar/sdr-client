"""Schedule day-planner: the now-line countdown pill.

On today, a small pill rides the right end of the red now-line and counts down — to the
next scheduled plan's on-air (a hollow ring · "STARTS IN"), or, while a plan is on air, to
its off-air (a filled dot · "ENDS IN"). It carries no task name (the block the line sits on
names it) and skips reference (note) windows, which are never transmitted.
"""
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

import ui.timeline_tab as tt

_app = QApplication.instance() or QApplication(sys.argv)

TODAY = date.today()


def _blk(bid, state, start, stop, *, ref=False):
    return {"id": bid, "name": bid, "desc": "", "state": state, "reference": ref,
            "armable": False, "start": start, "stop": stop}


def _at(hh, mm=0, ss=0):
    return datetime.combine(TODAY, time(hh, mm, ss))


def _planner(blocks):
    p = tt._DayPlanner()
    p.set_day(TODAY, blocks)
    return p


# ── target selection ─────────────────────────────────────────────────────────

def test_counts_down_to_a_running_plans_off_air():
    p = _planner([_blk("p1", "on air", _at(9, 0), _at(10, 0))])
    mode, remaining = p._countdown_target(_at(9, 20))
    assert mode == "onair"
    assert abs(remaining - timedelta(minutes=40)) < timedelta(seconds=1)


def test_counts_down_to_the_next_plans_on_air_in_a_gap():
    p = _planner([_blk("p1", "idle", _at(8, 0), _at(8, 30)),
                  _blk("p2", "armed", _at(11, 0), _at(11, 30))])
    mode, remaining = p._countdown_target(_at(9, 45))
    assert mode == "next"
    assert abs(remaining - timedelta(hours=1, minutes=15)) < timedelta(seconds=1)


def test_reference_windows_are_skipped_as_a_target():
    # A reference sits between now and the next real plan; the countdown steps over it.
    p = _planner([_blk("r1", "reference", _at(10, 0), _at(10, 30), ref=True),
                  _blk("p1", "idle", _at(12, 0), _at(12, 30))])
    mode, remaining = p._countdown_target(_at(9, 30))
    assert mode == "next"
    assert abs(remaining - timedelta(hours=2, minutes=30)) < timedelta(seconds=1)


def test_inside_a_reference_window_still_counts_to_the_next_plan():
    # A reference is never "on air" — even while now is inside it, the pill targets the next plan.
    p = _planner([_blk("r1", "reference", _at(9, 0), _at(10, 0), ref=True),
                  _blk("p1", "idle", _at(11, 0), _at(11, 30))])
    mode, _ = p._countdown_target(_at(9, 30))
    assert mode == "next"


def test_a_reference_only_day_has_no_target():
    p = _planner([_blk("r1", "reference", _at(9, 0), _at(10, 0), ref=True)])
    assert p._countdown_target(_at(8, 0)) is None
    assert p._countdown_target(_at(9, 30)) is None      # inside it, still nothing to count to


def test_no_target_once_the_last_plan_has_passed():
    p = _planner([_blk("p1", "idle", _at(8, 0), _at(8, 30))])
    assert p._countdown_target(_at(9, 0)) is None


def test_picks_the_soonest_of_several():
    p = _planner([_blk("p1", "idle", _at(12, 0), _at(12, 30)),
                  _blk("p2", "idle", _at(10, 30), _at(11, 0)),      # the soonest next
                  _blk("p3", "idle", _at(15, 0), _at(15, 30))])
    mode, remaining = p._countdown_target(_at(9, 0))
    assert mode == "next"
    assert abs(remaining - timedelta(hours=1, minutes=30)) < timedelta(seconds=1)
    # Two overlapping on-air plans → the one ending soonest wins.
    q = _planner([_blk("a", "on air", _at(9, 0), _at(11, 0)),
                  _blk("b", "on air", _at(9, 0), _at(10, 0))])
    mode, remaining = q._countdown_target(_at(9, 30))
    assert mode == "onair" and abs(remaining - timedelta(minutes=30)) < timedelta(seconds=1)


# ── formatting ───────────────────────────────────────────────────────────────

def test_fmt_countdown():
    f = tt._DayPlanner._fmt_countdown
    assert f(0) == "00:00"
    assert f(59) == "00:59"
    assert f(60) == "01:00"
    assert f(14 * 60 + 32) == "14:32"
    assert f(3600) == "1:00:00"
    assert f(3661) == "1:01:01"
    assert f(-5) == "00:00"          # clamps negatives


# ── the per-second tick ──────────────────────────────────────────────────────

def test_second_timer_runs_on_today_and_stops_off_it():
    p = _planner([])
    assert p._sec_timer.isActive()                        # today → ticking
    p.set_day(TODAY + timedelta(days=1), [])
    assert not p._sec_timer.isActive()                    # another day → stopped
    p.set_day(TODAY, [])
    assert p._sec_timer.isActive()                        # back to today → ticking again


# ── paint smoke ──────────────────────────────────────────────────────────────

def _render(p):
    p.resize(720, max(p.content_height(), 10))
    pm = QPixmap(720, max(p.content_height(), 10))
    pm.fill(Qt.GlobalColor.white)
    p.render(pm)                                          # drives paintEvent → _paint_countdown


def test_painting_today_with_a_countdown_does_not_raise():
    now = datetime.now()
    p = _planner([_blk("p1", "on air", now - timedelta(minutes=5), now + timedelta(minutes=25))])
    _render(p)                                            # on-air pill path
    q = _planner([_blk("p2", "idle", now + timedelta(minutes=30), now + timedelta(minutes=60))])
    _render(q)                                            # next pill path
    r = _planner([])                                      # nothing to count → no pill
    _render(r)
    assert r._countdown_target(now) is None
