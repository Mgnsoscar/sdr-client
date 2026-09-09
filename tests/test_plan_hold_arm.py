"""The Hold step in PLANS (docs/sequence-hold-step.md §6–§7, §10–§12).

This is the scope expansion from "plans always compile the Hold out" to "a single-unit,
operator-present plan honours the Hold, while multi-unit plans and the unattended schedule still
compile it out". Covers:

  - the version-aware runtime gate (hold_runtime_supported): sequence-hold is advertised from
    1.16.0 (data model only, arm refused) but the runtime needs 1.17.0.
  - authoring: a plan-local sequence round-trips a Hold (timeline editor + PlanItem/PlanBar), and
    the plan-item dialog no longer disables `+ Hold`.
  - eligibility (_hold_aware_plan_item): single-unit + hold + capable → hold-aware; otherwise None.
  - _arm_plan(hold_aware=True) sends the Hold VERBATIM (open-ended, max_hold_s); the default path
    still collapses a multi-unit plan and never sets hold_aware.
  - the plan run row relabels Arm→Proceed while HOLDING and surfaces Hold-now / Edit….
"""
import os
from datetime import datetime, timezone

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui import plan_editor as pe
from ui import plans_tab as pt
from ui import timeline_model as tlm

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _hold_steps(hold_off=120.0):
    return [
        m.SequenceStep(anchor="start", offset_s=-10, action=m.StepAction.START, task_name="tx"),
        m.SequenceStep(anchor="start", offset_s=hold_off, action=m.StepAction.HOLD, task_name=""),
        m.SequenceStep(anchor="hold", offset_s=30, action=m.StepAction.TUNE, task_name="tx",
                       params={"power": -40}),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx"),
    ]


def _plain_steps():
    return [
        m.SequenceStep(anchor="start", offset_s=0, action=m.StepAction.START, task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx"),
    ]


def _item(host="a", steps=None, seq_id="s1"):
    return m.PlanItem(hostname=host, sequence_id=seq_id, sequence_name="n", steps=steps or [])


def _resolved(*step_lists):
    return {i: s for i, s in enumerate(step_lists)}


# ── item 4: the version-aware runtime gate ────────────────────────────────────

class _Cli:
    def __init__(self, caps=("sequence-hold",), ver="1.19.0"):
        self._caps = set(caps)
        self.agent_version = ver

    def supports(self, cap):
        return cap in self._caps


def test_hold_runtime_supported_gates_on_capability_and_version():
    ok = tlm.hold_runtime_supported
    assert ok(_Cli(ver="1.19.0")) is True
    assert ok(_Cli(ver="1.17.0")) is True
    assert ok(_Cli(ver="1.16.0")) is False       # capability advertised, runtime NOT present
    assert ok(_Cli(caps=(), ver="1.19.0")) is False
    assert ok(_Cli(ver="")) is True              # unknown version → the capability is authoritative
    assert ok(_Cli(ver="2.0.0")) is True


# ── item 1: authoring + round-trip ────────────────────────────────────────────

def test_plan_local_timeline_round_trips_a_hold():
    # The plan-item dialog embeds the same TimelineEditor; a Hold survives set_steps → steps().
    from ui.timeline_editor import TimelineEditor
    te = TimelineEditor()
    te.set_steps(_hold_steps(120.0))
    out = te.steps()
    assert m.has_hold(out)
    wb = [s for s in out if m._step_action(s) == m.StepAction.TUNE.value]
    assert wb and wb[0].anchor == "hold" and wb[0].offset_s == 30.0
    # a Hold-free list round-trips with no marker
    te.set_steps(_plain_steps())
    assert not m.has_hold(te.steps())


def test_plan_item_bar_round_trip_preserves_a_hold():
    it = _item(steps=_hold_steps(150.0))
    back = pe._item_from_bar(pe._bar_from_item(it))
    assert m.has_hold(back.steps)
    assert any(s.anchor == "hold" for s in back.steps)


def test_plan_item_dialog_enables_hold_authoring():
    # plan_editor no longer calls set_hold_authoring(False): the ARM path now decides whether a
    # Hold pauses, so authoring one in a plan-local sequence is meaningful.
    from tests.test_step_editor_power_units import FakeHub
    dlg = pe.PlanItemDialog(FakeHub(), {}, parent=None)
    try:
        assert dlg._timeline._hold_authoring is True
        assert dlg._timeline._add_hold.isVisibleTo(dlg._timeline) is True
    finally:
        dlg.deleteLater()


# ── item 2 + §10: single-unit hold-aware eligibility ──────────────────────────

def test_single_unit_hold_on_a_capable_agent_is_eligible():
    plan = m.Plan(id="p", name="p", items=[_item(steps=_hold_steps())])
    got = pt._hold_aware_plan_item(plan, _resolved(_hold_steps()), lambda h: True)
    assert got is plan.items[0]


def test_hold_not_eligible_when_agent_lacks_the_runtime():
    plan = m.Plan(id="p", name="p", items=[_item(steps=_hold_steps())])
    assert pt._hold_aware_plan_item(plan, _resolved(_hold_steps()), lambda h: False) is None


def test_hold_not_eligible_for_a_multi_unit_plan():
    # Cross-unit Hold synchronisation is deferred (§10): a plan with >1 item compiles the Hold out.
    plan = m.Plan(id="p", name="p", items=[
        _item(host="a", steps=_hold_steps()), _item(host="b", steps=_hold_steps())])
    got = pt._hold_aware_plan_item(plan, _resolved(_hold_steps(), _hold_steps()), lambda h: True)
    assert got is None


def test_hold_free_single_unit_plan_is_not_hold_aware():
    plan = m.Plan(id="p", name="p", items=[_item(steps=_plain_steps())])
    assert pt._hold_aware_plan_item(plan, _resolved(_plain_steps()), lambda h: True) is None


# ── item 2: the arm request ───────────────────────────────────────────────────

class _ArmClient:
    def __init__(self, skew=0.0, stored=None):
        self.skew = skew
        self.captured = []
        self._stored = stored

    def clock_offset_s(self):
        return self.skew

    def get_sequence(self, seq_id):
        return self._stored

    def arm_sequence(self, seq_id, req):
        self.captured.append(req)
        return req


class _ArmFleet:
    def __init__(self, client):
        self._c = client

    def get(self, _host):
        return self._c


def test_arm_plan_hold_aware_sends_the_hold_verbatim_and_flags():
    c = _ArmClient()
    plan = m.Plan(id="p", name="p", items=[_item(steps=_hold_steps(120.0))])
    pt._arm_plan(_ArmFleet(c), plan, datetime(2030, 1, 1, tzinfo=timezone.utc), None,
                 hold_aware=True, max_hold_s=600.0)
    req = c.captured[0]
    assert req.hold_aware is True and req.open_ended is True
    assert req.on_air_duration_s is None and req.max_hold_s == 600.0
    assert req.plan_id == "p"
    assert m.has_hold(req.steps)                          # Hold intact — NOT collapsed
    assert any(s.anchor == "hold" and s.offset_s == 30.0 for s in req.steps)


def test_arm_plan_hold_aware_bakes_overrides_into_a_stored_hold_no_step_overrides():
    # A steps-less item referencing a stored Hold-bearing sequence with legacy overrides: the
    # hold-aware arm must bake the overrides into inline steps (Hold intact) and send NO
    # step_overrides, since the agent refuses step_overrides alongside a Hold.
    stored = m.Sequence(id="s1", name="stored", steps=[
        m.SequenceStep(anchor="start", offset_s=-10, action=m.StepAction.START, task_name="tx",
                       args=["--power", "-40"]),
        m.SequenceStep(anchor="start", offset_s=120, action=m.StepAction.HOLD, task_name=""),
        m.SequenceStep(anchor="hold", offset_s=30, action=m.StepAction.TUNE, task_name="tx",
                       params={"power": -40}),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx")])
    ov = [m.StepOverride(index=0, args=["--power", "-55"], replace_args=True)]
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", sequence_name="n", steps=[], overrides=ov)])
    c = _ArmClient(stored=stored)
    pt._arm_plan(_ArmFleet(c), plan, datetime(2030, 1, 1, tzinfo=timezone.utc), None,
                 hold_aware=True, max_hold_s=600.0)
    req = c.captured[0]
    assert req.hold_aware is True and req.step_overrides == []
    assert m.has_hold(req.steps)                          # Hold intact
    start = next(s for s in req.steps if s.action == m.StepAction.START)
    assert start.args == ["--power", "-55"]               # the override was baked in


def test_arm_plan_default_multi_unit_still_collapses_the_hold():
    c = _ArmClient()
    plan = m.Plan(id="p", name="p", items=[
        _item(host="a", steps=_hold_steps()), _item(host="b", steps=_hold_steps())])
    pt._arm_plan(_ArmFleet(c), plan, datetime(2030, 1, 1, tzinfo=timezone.utc), 600.0)
    assert len(c.captured) == 2
    for req in c.captured:
        assert req.hold_aware is False and not m.has_hold(req.steps)
        assert any(s.anchor == "start" and s.offset_s == 150.0 for s in req.steps)  # window B re-anchored


# ── item 2: the plan run row (Arm→Proceed while HOLDING; Hold-now / Edit…) ─────

def _row(holding=False, can_ff=False, can_edit_wb=False):
    plan = m.Plan(id="p", name="p", items=[_item(steps=_hold_steps())])
    calls = {}
    row = pt._PlanRow(
        plan, [], 0, 0,
        on_arm=lambda p: calls.__setitem__("arm", p),
        on_stop=lambda p: None, on_edit=lambda p: calls.__setitem__("edit", p),
        on_delete=lambda p: None, on_log=lambda p: None,
        holding=holding, can_ff=can_ff, can_edit_wb=can_edit_wb,
        on_proceed=lambda p: calls.__setitem__("proceed", p),
        on_hold_now=lambda p: calls.__setitem__("ff", p),
        on_edit_wb=lambda p: calls.__setitem__("wb", p))
    return row, calls, plan


def test_plan_row_arm_button_when_idle():
    row, calls, plan = _row(holding=False)
    assert row._arm.text() == "Arm"
    row._arm.click()
    assert calls.get("arm") is plan and "proceed" not in calls


def test_plan_row_relabels_arm_to_proceed_while_holding():
    row, calls, plan = _row(holding=True)
    assert row._arm.text() == "Proceed" and row._arm.isEnabled()
    row._arm.click()
    assert calls.get("proceed") is plan and "arm" not in calls
    # Hold-only controls are hidden unless their run state + capability say otherwise.
    assert row._hold_now.isVisibleTo(row) is False
    # There is exactly ONE Edit button, never a separate "Edit…". Without edit-while-holding
    # support it still opens the plan editor.
    assert not hasattr(row, "_edit_wb")
    assert row._edit.text() == "Edit"
    row._edit.click()
    assert calls.get("edit") is plan and "wb" not in calls


def test_plan_row_shows_hold_now_and_edit_when_flagged():
    row, calls, plan = _row(holding=True, can_ff=True, can_edit_wb=True)
    assert row._hold_now.isVisibleTo(row) is True
    # No second edit button: the single Edit button drives edit-while-holding here.
    assert not hasattr(row, "_edit_wb")
    assert row._edit.isVisibleTo(row) is True
    row._hold_now.click()
    row._edit.click()
    assert calls.get("ff") is plan and calls.get("wb") is plan and "edit" not in calls


def test_plan_row_hold_now_can_show_without_holding():
    # A RUNNING hold-aware run (not yet held) offers Hold-now while the pill is still "running".
    row, calls, plan = _row(holding=False, can_ff=True)
    assert row._arm.text() == "Arm"
    assert row._hold_now.isVisibleTo(row) is True
    row._hold_now.click()
    assert calls.get("ff") is plan


def _edit_row(on_air_n=0, pending_n=0, holding=False):
    plan = m.Plan(id="p", name="p", items=[_item(steps=_hold_steps())])
    return pt._PlanRow(
        plan, [], on_air_n, pending_n,
        on_arm=lambda p: None, on_stop=lambda p: None, on_edit=lambda p: None,
        on_delete=lambda p: None, on_log=lambda p: None,
        holding=holding, on_proceed=lambda p: None, on_hold_now=lambda p: None,
        on_edit_wb=lambda p: None)


def test_plan_row_edit_enabled_only_when_holding_or_idle():
    # Edit the definition only when idle, or window B while HOLDING — never mid-run.
    assert _edit_row()._edit.isEnabled() is True               # idle
    assert _edit_row(holding=True)._edit.isEnabled() is True   # holding (edit window B)
    assert _edit_row(on_air_n=1)._edit.isEnabled() is False    # on air → disabled
    assert _edit_row(pending_n=1)._edit.isEnabled() is False   # armed → disabled
