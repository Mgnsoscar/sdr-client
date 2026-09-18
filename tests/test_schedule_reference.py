"""Schedule tab — reference (note) entries.

A reference is an external test the team does NOT transmit for, placed on the timeline
only for situational awareness — it carries just a name, a description and a time slot,
is visually distinct (a dashed violet block / "REFERENCE" tag), and is never armable.
"""
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QDialog

from api import models as m
from state.plan_store import PlanStore
from state.schedule_store import ScheduleStore
import ui.timeline_tab as tt

_app = QApplication.instance() or QApplication(sys.argv)

TOMORROW = date.today() + timedelta(days=1)


def _local(d, hh):
    return datetime.combine(d, time(hh, 0)).isoformat()


def _ref(eid="r1", name="Vendor A jammer", desc="external sweep", d=TOMORROW, start_h=10, stop_h=11):
    return m.ScheduledPlan(id=eid, plan_name=name, description=desc, reference=True,
                           start=_local(d, start_h), stop=_local(d, stop_h))


def _plan(pid="p1", name="Morning", host="u", seq="s1"):
    return m.Plan(id=pid, name=name, items=[
        m.PlanItem(hostname=host, unit_label=host.upper(), sequence_id=seq, sequence_name=seq)])


def _entry(eid, pid, d=TOMORROW, start_h=12, stop_h=13):
    return m.ScheduledPlan(id=eid, plan_id=pid, start=_local(d, start_h), stop=_local(d, stop_h))


# ── The model ────────────────────────────────────────────────────────────────

def test_reference_validates_without_a_plan_id():
    e = _ref()
    assert e.reference and e.plan_id == "" and e.plan is None
    assert e.plan_name == "Vendor A jammer" and e.description == "external sweep"
    # A round-trip through JSON keeps the reference fields.
    e2 = m.ScheduledPlan.model_validate(e.model_dump())
    assert e2.reference and e2.description == "external sweep"


def test_an_ordinary_scheduled_plan_is_not_a_reference():
    e = _entry("e1", "p1")
    assert e.reference is False and e.description == ""


# ── The dialog ───────────────────────────────────────────────────────────────

def test_reference_dialog_builds_a_reference_entry():
    dlg = tt._ReferenceDialog(default_day=TOMORROW)
    dlg._name.setText("  Vendor B ranging  ")
    dlg._desc.setPlainText("their L1 test")
    dlg._revalidate()
    assert dlg._ok.isEnabled()
    dlg._accept()
    r = dlg.result_entry
    assert r is not None and r.reference and r.plan_id == ""
    assert r.plan_name == "Vendor B ranging"          # trimmed
    assert r.description == "their L1 test"


def test_reference_dialog_requires_a_name_and_a_positive_window():
    dlg = tt._ReferenceDialog(default_day=TOMORROW)
    dlg._name.setText("")
    dlg._revalidate()
    assert not dlg._ok.isEnabled() and "name" in dlg._status.text()
    dlg._name.setText("X")
    # stop before start
    dlg._stop.setDateTime(dlg._start.dateTime().addSecs(-3600))
    dlg._revalidate()
    assert not dlg._ok.isEnabled() and "after" in dlg._status.text()


def test_reference_dialog_edits_an_existing_entry():
    dlg = tt._ReferenceDialog(entry=_ref(name="old", desc="was"))
    assert dlg._name.text() == "old" and dlg._desc.toPlainText() == "was"
    dlg._name.setText("new name")
    dlg._accept()
    assert dlg.result_entry.plan_name == "new name" and dlg.result_entry.id == "r1"


# ── TimelineTab wiring ───────────────────────────────────────────────────────

@pytest.fixture
def make_tab(tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "ScheduleStore", lambda: ScheduleStore(tmp_path / "schedule.json"))
    monkeypatch.setattr(tt, "PlanStore", lambda: PlanStore(tmp_path / "plans.json"))

    def _make(plans=(), entries=(), day=TOMORROW):
        tab = tt.TimelineTab(None)
        for p in plans:
            tab._plans.upsert(p)
        for e in entries:
            tab._store.upsert(e)
        tab._timeline_day = day
        tab._selected_day = day
        tab._reload()
        return tab

    return _make


def test_resolve_and_state_for_a_reference(make_tab):
    tab = make_tab(entries=[_ref()])
    e = tab._store.get("r1")
    assert tab._resolve(e) == ("Vendor A jammer", "external sweep")
    assert tab._entry_state(e) == "reference"


def test_a_reference_is_never_armable(make_tab):
    # A future reference (start in the future) must still be non-armable.
    day = date.today() + timedelta(days=2)
    tab = make_tab(entries=[_ref(d=day)], day=day)
    row = next(r for r in tab._entries_on(day) if r["id"] == "r1")
    assert row["reference"] is True and row["armable"] is False and row["state"] == "reference"
    assert tab._armable_entries(day) == []


def test_arm_all_ignores_references(make_tab):
    plans = [_plan("p1", "Morning", seq="s1")]
    entries = [_entry("e1", "p1", TOMORROW, 14, 15), _ref("r1", d=TOMORROW, start_h=10, stop_h=11)]
    tab = make_tab(plans=plans, entries=entries)
    # Only the real plan is armable — the reference is skipped.
    assert [e.id for e in tab._armable_entries(TOMORROW)] == ["e1"]
    assert tab._tl_arm_all.text() == "Arm all (1)"


def test_arm_on_a_reference_is_a_noop(make_tab):
    tab = make_tab(entries=[_ref()])
    tab._on_arm("r1")          # must not raise and must not try to arm (hub is None anyway)


def test_add_reference_works_without_any_plans(make_tab, monkeypatch):
    captured = {}

    class _FakeDlg:
        def __init__(self, *a, **k):
            captured["built"] = True
            self.result_entry = _ref("added", name="Note", desc="d")

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(tt, "_ReferenceDialog", _FakeDlg)
    tab = make_tab()                       # no plans at all
    tab._on_add_reference()
    assert captured.get("built")
    assert tab._store.get("added") is not None and tab._store.get("added").reference


def test_clicking_a_reference_block_opens_the_reference_dialog(make_tab, monkeypatch):
    seen = {}

    class _FakeRefDlg:
        REMOVE = tt._ReferenceDialog.REMOVE

        def __init__(self, entry=None, **k):
            seen["entry"] = entry
            self.result_entry = _ref("r1", name="edited")

        def exec(self):
            return QDialog.DialogCode.Accepted

    class _FakeSchedDlg:
        def __init__(self, *a, **k):
            seen["sched_opened"] = True

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(tt, "_ReferenceDialog", _FakeRefDlg)
    monkeypatch.setattr(tt, "_ScheduleDialog", _FakeSchedDlg)
    tab = make_tab(entries=[_ref()])
    tab._on_block("r1")
    assert seen.get("entry") is not None and "sched_opened" not in seen   # ref path, not the plan dialog
    assert tab._store.get("r1").plan_name == "edited"


# ── The day-planner painting ─────────────────────────────────────────────────

def _paint(planner):
    planner.resize(640, max(planner.content_height(), 10))
    pm = QPixmap(640, max(planner.content_height(), 10))
    pm.fill(Qt.GlobalColor.white)
    planner.render(pm)          # drives paintEvent → fills _rects / _btn_rects


def test_a_reference_block_has_no_arm_hit_rect():
    planner = tt._DayPlanner()
    planner.set_day(TOMORROW, [
        {"id": "r1", "name": "Vendor A", "desc": "x", "state": "reference", "reference": True,
         "armable": False,
         "start": datetime.combine(TOMORROW, time(10, 0)),
         "stop": datetime.combine(TOMORROW, time(11, 0))},
        {"id": "p1", "name": "Ours", "desc": "", "state": "idle", "reference": False,
         "armable": True,
         "start": datetime.combine(TOMORROW, time(12, 0)),
         "stop": datetime.combine(TOMORROW, time(13, 0))},
    ])
    _paint(planner)
    # The reference registers no actionable button; the armable plan registers an Arm rect.
    assert "r1" not in planner._btn_rects
    assert planner._btn_rects.get("p1", (None, None))[1] == "arm"
    # Both blocks are still clickable to edit.
    assert "r1" in planner._rects and "p1" in planner._rects
