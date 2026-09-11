"""UI routing for the run/task conflict guards (offscreen Qt):
- Tasks tab: stopping a task routes to the 3-way dialog only when a run owns it.
- Sequences: arming pre-checks running tasks, then either arms or offers stop-and-arm.
- Plans: the batch task-stop helper reports per-task results.
The dialogs themselves (QMessageBox) are standard Qt; these pin the branching."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui.unit_detail import _TaskRow, _TasksPanel
import ui.sequences_panel as sp
import ui.plans_tab as pt

_app = QApplication.instance() or QApplication([])


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)

    def __init__(self, client=None):
        super().__init__()
        self.calls = []
        self.fleet = SimpleNamespace(get=lambda h: client)

    def run_async(self, label, fn):
        self.calls.append(label)        # record but DON'T run fn (no real I/O in tests)


def _status(name, state=m.ProcessState.RUNNING):
    return m.ProcessStatus(name=name, description="", state=state)


def _run(state, tasks, sequence_name="Seq"):
    return SimpleNamespace(state=state, id="r1", sequence_id="s1",
                           sequence_name=sequence_name, plan_id="", plan_name="",
                           steps=[SimpleNamespace(task_name=t) for t in tasks])


# ── Feature 2: Tasks tab — stop a task that a run owns ───────────────────────────

def test_task_stop_only_prompts_when_a_run_owns_it(monkeypatch):
    hub = _Hub()

    def make(provider):
        row = _TaskRow("u", _status("tx"), hub, runs_provider=provider)
        rec = []
        monkeypatch.setattr(row, "_do_stop_task", lambda: rec.append("stop"))
        monkeypatch.setattr(row, "_confirm_stop_owned", lambda o: rec.append(("confirm", len(o))))
        return row, rec

    # No active run using it → stop straight away.
    row, rec = make(lambda: [])
    row._on_stop()
    assert rec == ["stop"]

    # A COMPLETED run using it is not active → still stop straight away.
    row, rec = make(lambda: [_run(m.SequenceState.COMPLETED, ["tx"])])
    row._on_stop()
    assert rec == ["stop"]

    # An active (running) run using it → the 3-way confirm.
    row, rec = make(lambda: [_run(m.SequenceState.RUNNING, ["tx"])])
    row._on_stop()
    assert rec == [("confirm", 1)]


def test_panel_feeds_active_runs_to_rows():
    p = _TasksPanel("u", _Hub())
    p.update_tasks([_status("tx")])
    runs = [_run(m.SequenceState.RUNNING, ["tx"])]
    p.update_runs(runs)
    assert p._rows["tx"]._runs_provider() == runs


# ── Feature 1: Sequences — arm pre-check ────────────────────────────────────────

def _seq():
    return SimpleNamespace(id="sid", name="S",
                           steps=[SimpleNamespace(task_name="tx", action="start", anchor="start")])


def _seq_panel():
    return sp.SequencesPanel("u", _Hub(), can_edit=False, can_run=True)


def test_on_start_fires_precheck(monkeypatch):
    p = _seq_panel()
    monkeypatch.setattr(sp, "_hold_offset_of", lambda seq: None)
    seq = _seq()
    p._on_start(seq)
    assert p._pending_arm is seq
    assert any(l.startswith("seq_precheck:u:") for l in p.hub.calls)


def test_precheck_clear_arms_conflict_offers_stop(monkeypatch):
    p = _seq_panel()
    seq = _seq()
    routed = []
    monkeypatch.setattr(p, "_arm_flow", lambda s: routed.append(("arm", s)))
    monkeypatch.setattr(p, "_offer_stop_and_arm", lambda s, c: routed.append(("offer", c)))

    # No conflicts → arm straight through.
    p._pending_arm = seq
    p._on_task_done("seq_precheck:u:sid", [])
    assert routed == [("arm", seq)]

    # Conflicts → offer stop-and-arm (not arm).
    routed.clear()
    p._pending_arm = seq
    p._on_task_done("seq_precheck:u:sid", ["tx"])
    assert routed == [("offer", ["tx"])]

    # After stopping, arm.
    routed.clear()
    p._pending_arm = seq
    p._on_task_done("seq_stoptasks:u:sid", [object()])
    assert routed == [("arm", seq)]


# ── Feature 1: Plans — batch task stop helper ───────────────────────────────────

def test_stop_tasks_on_hosts_reports_per_task():
    stopped = []

    class _Client:
        def __init__(self, fail=None):
            self._fail = fail or set()

        def stop_task(self, name):
            if name in self._fail:
                raise RuntimeError(f"nope {name}")
            stopped.append(name)

    clients = {"h1": _Client(), "h2": _Client(fail={"b"})}
    fleet = SimpleNamespace(get=lambda h: clients[h])
    out = pt._stop_tasks_on_hosts(fleet, {"h1": ["a"], "h2": ["b", "c"]})
    assert ("h1", "a", None) in out
    assert ("h2", "c", None) in out
    bad = [(h, t) for h, t, e in out if e is not None]
    assert bad == [("h2", "b")]
    assert set(stopped) == {"a", "c"}
