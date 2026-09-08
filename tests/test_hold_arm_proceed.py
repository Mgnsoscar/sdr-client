"""Phase 2 of the Hold step (docs/sequence-hold-step.md §6–§7): arming a Hold-bearing
sequence from the Library pauses at the Hold, Proceed reuses the ArmDialog to resume, and
the scheduled/plan path compiles the Hold OUT (the unattended no-op).

Covers:
  - model helpers: has_hold / collapse_hold (drop the marker, re-anchor window B to start).
  - the scheduled (_arm_scheduled) and plan (_arm_plan) arm paths send the COLLAPSED steps
    and never set hold_aware.
  - the interactive arm (_arm_at, hold_aware) is open-ended + carries max_hold_s; _proceed_run
    posts the resume instant.
  - the api client's proceed_sequence_run() POSTs ProceedRequest to the run's /proceed.
  - the ArmDialog serves both arm (max-hold deadman) and Proceed (relabelled, live status).
  - the capability gate blocks a hold-aware arm on an agent lacking `sequence-hold`.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from api import models as m
from api.client import AgentClient
from ui import sequences_panel as sp
from ui.arm_dialog import ArmDialog

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _hold_seq(hold_off=120.0):
    return m.Sequence(id="s1", name="LoL test", steps=[
        m.SequenceStep(anchor="start", offset_s=-10, action=m.StepAction.START, task_name="tx"),
        m.SequenceStep(anchor="start", offset_s=hold_off, action=m.StepAction.HOLD, task_name=""),
        m.SequenceStep(anchor="hold", offset_s=30, action=m.StepAction.TUNE, task_name="tx",
                       params={"power": -40}),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx"),
    ])


# ── model helpers ────────────────────────────────────────────────────────────

def test_has_hold_and_collapse_hold():
    seq = _hold_seq(120.0)
    assert m.has_hold(seq.steps) is True
    out = m.collapse_hold(seq.steps)
    assert m.has_hold(out) is False                         # marker dropped
    # the window-B tune (anchor="hold" +30) becomes start-anchored at 120+30 = 150
    wb = [s for s in out if s.action == m.StepAction.TUNE][0]
    assert wb.anchor == "start" and wb.offset_s == 150.0 and wb.params == {"power": -40}
    # the start/stop steps are untouched
    assert [(s.action.value, s.anchor, s.offset_s) for s in out] == [
        ("start", "start", -10.0), ("tune", "start", 150.0), ("stop", "stop", 0.0)]


def test_collapse_hold_is_a_noop_without_a_hold():
    steps = [m.SequenceStep(anchor="start", offset_s=0, action=m.StepAction.START, task_name="tx"),
             m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx")]
    out = m.collapse_hold(steps)
    assert out == steps and all(a is b for a, b in zip(out, steps))    # same objects, untouched


def test_hold_offset_of():
    assert sp._hold_offset_of(_hold_seq(200.0)) == 200.0
    plain = m.Sequence(id="s", name="s", steps=[
        m.SequenceStep(anchor="start", offset_s=0, action=m.StepAction.START, task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx")])
    assert sp._hold_offset_of(plain) is None


# ── the arm helpers (fake clients, like test_arm_clock_skew) ─────────────────

class _Client:
    def __init__(self, skew=0.0, supports=("sequence-hold",), stored=None):
        self.skew = skew
        self._caps = set(supports)
        self.captured = []
        self.proceeded = []
        self._stored = stored          # the unit's STORED sequence (for get_sequence)

    def clock_offset_s(self):
        return self.skew

    def supports(self, cap):
        return cap in self._caps

    def get_sequence(self, seq_id):
        return self._stored

    def arm_sequence(self, seq_id, req):
        self.captured.append(req)
        return req

    def proceed_sequence_run(self, run_id, req):
        self.proceeded.append((run_id, req))
        return m.SequenceRun(id=run_id, sequence_id="s1", sequence_name="n",
                             on_air_at="2030-01-01T00:00:00+00:00", state=m.SequenceState.RUNNING)


class _Fleet:
    def __init__(self, client):
        self._c = client

    def get(self, _host):
        return self._c


def test_interactive_hold_arm_is_open_ended_with_max_hold():
    c = _Client()
    t0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    sp._arm_at(c, _hold_seq(), t0, None, hold_aware=True, max_hold_s=600.0)
    req = c.captured[0]
    assert req.hold_aware is True and req.open_ended is True
    assert req.on_air_duration_s is None and req.max_hold_s == 600.0


def test_proceed_run_posts_translated_instant():
    c = _Client(skew=15.0)
    resume = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    sp._proceed_run(c, "run-7", resume)
    run_id, req = c.proceeded[0]
    assert run_id == "run-7"
    assert datetime.fromisoformat(req.proceed_at) == resume + timedelta(seconds=15)
    assert req.steps is None                                  # edit-while-holding is Phase 3


def test_scheduled_arm_collapses_the_hold_and_never_sets_hold_aware():
    from ui.timeline_tab import _arm_scheduled
    c = _Client()
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", steps=_hold_seq().steps)])
    start = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _arm_scheduled(_Fleet(c), plan, start, start + timedelta(minutes=10))
    req = c.captured[0]
    assert req.hold_aware is False
    assert not m.has_hold(req.steps)                          # compiled out
    assert any(s.anchor == "start" and s.offset_s == 150.0 for s in req.steps)


def test_manual_plan_arm_collapses_the_hold():
    from ui.plans_tab import _arm_plan
    c = _Client()
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", steps=_hold_seq().steps)])
    _arm_plan(_Fleet(c), plan, datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc), duration_s=600.0)
    req = c.captured[0]
    assert req.hold_aware is False and not m.has_hold(req.steps)


def test_arm_collapses_a_STORED_sequence_hold_with_no_plan_local_copy():
    # An item with NO plan-local copy (steps=[]) references the unit's stored sequence. If that
    # stored sequence holds a Hold, the arm must still compile it out (fetch + collapse) — else a
    # Hold-bearing arm without hold_aware would be refused, contradicting the "runs straight
    # through" promise. Both arm paths.
    from ui.plans_tab import _arm_plan
    from ui.timeline_tab import _arm_scheduled
    stored = m.Sequence(id="s1", name="stored", steps=_hold_seq().steps)
    plan = m.Plan(id="p", name="p", items=[m.PlanItem(hostname="a", sequence_id="s1", steps=[])])

    c1 = _Client(stored=stored)
    _arm_plan(_Fleet(c1), plan, datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc), duration_s=600.0)
    req1 = c1.captured[0]
    assert req1.hold_aware is False and req1.steps is not None and not m.has_hold(req1.steps)
    assert req1.step_overrides == []                          # explicit steps → no legacy overrides

    c2 = _Client(stored=stored)
    start = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _arm_scheduled(_Fleet(c2), plan, start, start + timedelta(minutes=10))
    req2 = c2.captured[0]
    assert req2.hold_aware is False and req2.steps is not None and not m.has_hold(req2.steps)


def test_arm_of_a_stored_HOLD_FREE_sequence_falls_back_unchanged():
    # A steps-less item whose stored sequence has NO Hold must behave exactly as before: send no
    # inline steps (use the stored sequence) and keep any legacy overrides.
    from ui.plans_tab import _arm_plan
    plain = m.Sequence(id="s1", name="plain", steps=[
        m.SequenceStep(anchor="start", offset_s=0, action=m.StepAction.START, task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0, action=m.StepAction.STOP, task_name="tx")])
    ov = [m.StepOverride(index=0, args=["--x", "1"])]
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", steps=[], overrides=ov)])
    c = _Client(stored=plain)
    _arm_plan(_Fleet(c), plan, datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc), duration_s=600.0)
    req = c.captured[0]
    assert req.steps is None and req.step_overrides == ov     # unchanged fallback


# ── the api client wrapper ───────────────────────────────────────────────────

def test_client_proceed_posts_to_the_run(monkeypatch):
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    seen = {}

    def fake_request(method, path, json=None, **k):
        seen.update(method=method, path=path, json=json)
        return {"id": "r9", "sequence_id": "s1", "sequence_name": "n",
                "on_air_at": "2030-01-01T00:00:00+00:00", "state": "running"}

    monkeypatch.setattr(c, "_request", fake_request)
    run = c.proceed_sequence_run("r9", m.ProceedRequest(proceed_at="2030-01-01T00:05:00+00:00"))
    assert seen["method"] == "POST" and seen["path"] == "/sequence-runs/r9/proceed"
    assert seen["json"]["proceed_at"] == "2030-01-01T00:05:00+00:00"
    assert run.state == m.SequenceState.RUNNING


def test_client_hold_now_posts_to_the_run(monkeypatch):
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    seen = {}

    def fake_request(method, path, json=None, **k):
        seen.update(method=method, path=path)
        return {"id": "r5", "sequence_id": "s1", "sequence_name": "n",
                "on_air_at": "2030-01-01T00:00:00+00:00", "state": "holding"}

    monkeypatch.setattr(c, "_request", fake_request)
    run = c.hold_now_sequence_run("r5")
    assert seen["method"] == "POST" and seen["path"] == "/sequence-runs/r5/hold-now"
    assert run.state == m.SequenceState.HOLDING


# ── the ArmDialog serves both arm and Proceed ────────────────────────────────

def test_arm_dialog_hold_variant_shows_max_hold_and_hides_stop():
    d = ArmDialog("Arm", 10, 60, 0, show_stop=False, max_hold_default_s=1800.0)
    assert d._stop_section.isVisibleTo(d) is False
    assert d.max_hold_s() == 1800.0
    d._maxhold_on.setChecked(False)                          # unchecked = unlimited
    d._sync_maxhold()
    assert d.max_hold_s() == 0.0


def test_arm_dialog_proceed_variant_relabels_and_shows_live_status():
    ticks = {"n": 0}

    def status():
        ticks["n"] += 1
        return "elapsed 3:20 · held 1:05 · auto-stops in 28:55"

    d = ArmDialog("Proceed", 0, 60, 0, accept_label="Proceed", title="Proceed",
                  show_stop=False, status_provider=status)
    d._render()
    assert d.windowTitle() == "Proceed"
    assert d._status_line.isVisibleTo(d) is True
    assert "auto-stops in 28:55" in d._status_line.text()
    assert ticks["n"] >= 1
    assert d.max_hold_s() == 0.0                             # no deadman field on Proceed


# ── the SequencesPanel capability gate + Proceed button ──────────────────────

class _PanelHub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)

    def __init__(self, client):
        super().__init__()
        self.fleet = _Fleet(client)
        self.calls = []
        self._last_fn = None

    def run_async(self, label, fn):
        self.calls.append(label)
        self._last_fn = fn


def _panel(client):
    p = sp.SequencesPanel("u", _PanelHub(client), can_edit=False, can_run=True)
    return p


def test_hold_arm_blocked_when_agent_lacks_the_capability(monkeypatch):
    warned = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: warned.update(shown=True) or QMessageBox.StandardButton.Ok)
    p = _panel(_Client(supports=()))                         # no sequence-hold
    p._arm_hold_aware(_hold_seq(), 120.0)
    assert warned.get("shown") is True
    assert p.hub.calls == []                                 # nothing armed


def test_hold_arm_proceeds_when_supported(monkeypatch):
    # A fake ArmDialog that accepts immediately, so the arm request is actually built.
    class _FakeArm:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def on_air_at(self):
            return datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        def max_hold_s(self):
            return 900.0

    monkeypatch.setattr(sp, "ArmDialog", _FakeArm)
    client = _Client(supports=("sequence-hold",))
    p = _panel(client)
    p._arm_hold_aware(_hold_seq(), 120.0)
    assert p.hub.calls and p.hub.calls[0].startswith("seq_arm:")
    p.hub._last_fn()                                         # run the queued arm
    req = client.captured[0]
    assert req.hold_aware is True and req.open_ended is True and req.max_hold_s == 900.0


def test_running_hold_aware_row_shows_hold_now(monkeypatch):
    from datetime import datetime, timezone
    seq = _hold_seq()
    running = m.SequenceRun(id="r", sequence_id="s1", sequence_name="LoL test",
                            state=m.SequenceState.RUNNING,
                            on_air_at="2030-01-01T00:00:00+00:00",
                            hold_aware=True, held_actual=None)
    ff = {"n": 0}
    row = sp._SequenceRow(seq, running, on_start=lambda s: None, on_stop=lambda s: None,
                          on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                          can_run=True, can_edit=False,
                          on_hold_now=lambda s: ff.__setitem__("n", ff["n"] + 1), hold_now_ok=True)
    assert row._hold_now.isVisibleTo(row) is True and row._hold_now.isEnabled() is True
    row._hold_now.click()
    assert ff["n"] == 1

    # Once HOLDING (held_actual set), there's nothing to fast-forward → hidden.
    held = running.model_copy(update={"state": m.SequenceState.HOLDING,
                                      "held_actual": "2030-01-01T00:02:00+00:00"})
    row2 = sp._SequenceRow(seq, held, on_start=lambda s: None, on_stop=lambda s: None,
                           on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                           can_run=True, can_edit=False, on_proceed=lambda s: None,
                           on_hold_now=lambda s: None, hold_now_ok=True)
    assert row2._hold_now.isVisibleTo(row2) is False

    # Agent lacks the capability → hidden even while the run-up is in progress.
    row3 = sp._SequenceRow(seq, running, on_start=lambda s: None, on_stop=lambda s: None,
                           on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                           can_run=True, can_edit=False, on_hold_now=lambda s: None,
                           hold_now_ok=False)
    assert row3._hold_now.isVisibleTo(row3) is False


def test_holding_run_row_shows_proceed_button():
    run = m.SequenceRun(id="r1", sequence_id="s1", sequence_name="LoL test",
                        state=m.SequenceState.HOLDING,
                        on_air_at="2030-01-01T00:00:00+00:00",
                        held_actual="2030-01-01T00:02:00+00:00", max_hold_s=1800.0)
    proceeds = {"n": 0}
    row = sp._SequenceRow(_hold_seq(), run, on_start=lambda s: None, on_stop=lambda s: None,
                          on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                          can_run=True, can_edit=False,
                          on_proceed=lambda s: proceeds.__setitem__("n", proceeds["n"] + 1))
    assert row._start.text() == "Proceed" and row._start.isEnabled() is True
    assert row._stop.isEnabled() is True                     # holding is still "active"
    row._start.click()
    assert proceeds["n"] == 1


# ── Edit-while-holding (Phase 3c, §6.4) ──────────────────────────────────────

def test_edit_while_holding_row_shows_edit_only_when_holding_and_supported():
    held = m.SequenceRun(id="r", sequence_id="s1", sequence_name="LoL test",
                         state=m.SequenceState.HOLDING, on_air_at="2030-01-01T00:00:00+00:00",
                         hold_aware=True, held_actual="2030-01-01T00:02:00+00:00")
    edits = {"n": 0}
    row = sp._SequenceRow(_hold_seq(), held, on_start=lambda s: None, on_stop=lambda s: None,
                          on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                          can_run=True, can_edit=False, on_proceed=lambda s: None,
                          on_edit_wb=lambda s: edits.__setitem__("n", edits["n"] + 1),
                          edit_wb_ok=True)
    assert row._edit_wb.isVisibleTo(row) is True
    row._edit_wb.click()
    assert edits["n"] == 1

    # Hidden without the capability, and on a run that isn't holding.
    row2 = sp._SequenceRow(_hold_seq(), held, on_start=lambda s: None, on_stop=lambda s: None,
                           on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                           can_run=True, can_edit=False, on_proceed=lambda s: None,
                           on_edit_wb=lambda s: None, edit_wb_ok=False)
    assert row2._edit_wb.isVisibleTo(row2) is False
    running = held.model_copy(update={"state": m.SequenceState.RUNNING, "held_actual": None})
    row3 = sp._SequenceRow(_hold_seq(), running, on_start=lambda s: None, on_stop=lambda s: None,
                           on_edit=lambda s: None, on_delete=lambda s: None, on_log=lambda s: None,
                           can_run=True, can_edit=False, on_edit_wb=lambda s: None, edit_wb_ok=True)
    assert row3._edit_wb.isVisibleTo(row3) is False


def test_proceed_run_carries_edited_steps():
    c = _Client()
    edited = _hold_seq().steps                               # the edited full step list
    sp._proceed_run(c, "run-9", datetime(2030, 1, 1, tzinfo=timezone.utc), steps=edited)
    _run_id, req = c.proceeded[0]
    assert req.steps is not None and len(req.steps) == len(edited)
    # And no edit → steps stays None (the agent uses the window B stored at arm).
    sp._proceed_run(c, "run-9", datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert c.proceeded[1][1].steps is None


def test_hold_edit_dialog_returns_the_edited_steps():
    from ui.hold_edit_dialog import HoldEditDialog

    class _EditHub(QObject):
        task_done = pyqtSignal(str, object)

        def __init__(self):
            super().__init__()
            client = type("C", (), {
                "list_tasks": lambda self_: [type("T", (), {"name": "tx"})()],
                "get_tasks_yaml": lambda self_: "tasks:\n  - name: tx\n    command: [python3, tx.py]\n",
                "get_calibration": lambda self_: {"unit_type": "broadcaster", "valid": True,
                                                  "signals": {}},
            })()
            self.fleet = type("F", (), {"get": lambda self_, h: client})()

        def run_async(self, label, fn):
            try:
                res = fn()
            except Exception as exc:                         # noqa: BLE001
                res = exc
            self.task_done.emit(label, res)

    dlg = HoldEditDialog(_EditHub(), "unit", _hold_seq())
    _app.processEvents()
    # Retarget the window-B tune, then accept → the edited full step list comes back.
    steps = dlg._timeline.steps()
    assert any(s.action == m.StepAction.HOLD for s in steps)   # the Hold survives the round-trip
    dlg._accept()
    assert dlg.result_steps is not None
    assert any(s.action == m.StepAction.HOLD for s in dlg.result_steps)
    assert any(s.anchor == "hold" for s in dlg.result_steps)   # window B present
