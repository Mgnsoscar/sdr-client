"""
Per-slot scheduled-plan editing: a scheduled slot can carry its own copy of the plan,
edited without touching the library plan or any other slot that scheduled it.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog

from api import models as m
from state.schedule_store import ScheduleStore


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


def _plan(pid, name, desc=""):
    return m.Plan(id=pid, name=name, description=desc,
                  items=[m.PlanItem(hostname="unit_1", sequence_id="seq1")])


# ── model / store ───────────────────────────────────────────────────────────

def test_embedded_plan_round_trips(tmp_path):
    store = ScheduleStore(tmp_path / "schedule.json")
    e = m.ScheduledPlan(id="s1", plan_id="p1", start="2026-01-01T20:00:00",
                        stop="2026-01-01T22:00:00", plan=_plan("p1", "Custom", "EDITED"))
    store.upsert(e)
    back = ScheduleStore(tmp_path / "schedule.json").get("s1")
    assert back is not None and back.plan is not None
    assert back.plan.description == "EDITED"


def test_pre_existing_entry_has_no_embedded_plan(tmp_path):
    p = tmp_path / "schedule.json"
    p.write_text('{"schedule":[{"id":"s1","plan_id":"p1","start":"x","stop":"y"}]}')
    e = ScheduleStore(p).get("s1")
    assert e is not None and e.plan is None   # follows the library plan


# ── the schedule dialog: independence ───────────────────────────────────────

class _FakePlanEditor:
    """Stand-in for PlanEditorDialog: records the plan it was handed and returns an
    edited deep copy, so we can assert the library plan is never mutated."""
    seen = {}

    def __init__(self, hub, plan=None, parent=None):
        _FakePlanEditor.seen["plan"] = plan
        edited = plan.model_copy(deep=True)
        edited.description = "EDITED-IN-SLOT"
        self.result_plan = edited

    def exec(self):
        return QDialog.DialogCode.Accepted


def _dialog(monkeypatch, plans, entry=None):
    import ui.timeline_tab as tt
    monkeypatch.setattr(tt, "PlanEditorDialog", _FakePlanEditor)
    return tt._ScheduleDialog(plans, entry=entry, hub=object())


def test_editing_a_slot_copies_and_leaves_library_untouched(app, monkeypatch):
    p1, p2 = _plan("p1", "Alpha"), _plan("p2", "Beta")
    dlg = _dialog(monkeypatch, [p1, p2])          # add-mode, plan p1 selected
    assert dlg._plan_override is None

    dlg._edit_plan_contents()
    # the editor was handed a COPY, not the library object
    assert _FakePlanEditor.seen["plan"] is not p1
    assert dlg._plan_override is not None
    assert dlg._plan_override.id == "p1"
    assert dlg._plan_override.description == "EDITED-IN-SLOT"
    assert p1.description == ""                    # library plan untouched

    dlg._accept()
    assert dlg.result_entry is not None
    assert dlg.result_entry.plan_id == "p1"
    assert dlg.result_entry.plan is not None
    assert dlg.result_entry.plan.description == "EDITED-IN-SLOT"


def test_two_slots_same_plan_are_independent(app, monkeypatch):
    p1 = _plan("p1", "Alpha")
    # Slot A: customize it.
    a = _dialog(monkeypatch, [p1])
    a._edit_plan_contents()
    a._accept()
    # Slot B: same plan, left as-is → no embedded copy, follows the library.
    b = _dialog(monkeypatch, [p1])
    b._accept()
    assert a.result_entry.plan is not None and a.result_entry.plan.description == "EDITED-IN-SLOT"
    assert b.result_entry.plan is None
    assert p1.description == ""                    # library still pristine


def test_switching_plan_drops_the_customization(app, monkeypatch):
    p1, p2 = _plan("p1", "Alpha"), _plan("p2", "Beta")
    dlg = _dialog(monkeypatch, [p1, p2])
    dlg._edit_plan_contents()
    assert dlg._plan_override is not None
    dlg._plan.setCurrentIndex(1)                   # switch to p2 → fires _on_plan_changed
    assert dlg._plan_override is None
    dlg._accept()
    assert dlg.result_entry.plan_id == "p2"
    assert dlg.result_entry.plan is None


def test_reopening_a_customized_slot_keeps_its_edit(app, monkeypatch):
    p1 = _plan("p1", "Alpha")
    entry = m.ScheduledPlan(id="s1", plan_id="p1", start="2026-01-01T20:00:00",
                            stop="2026-01-01T22:00:00", plan=_plan("p1", "Alpha", "PREV"))
    dlg = _dialog(monkeypatch, [p1], entry=entry)
    assert dlg._plan_override is not None and dlg._plan_override.description == "PREV"
    # editing the entry's copy must not mutate the passed-in entry object
    assert entry.plan.description == "PREV"
    dlg._accept()
    assert dlg.result_entry.plan.description == "PREV"
