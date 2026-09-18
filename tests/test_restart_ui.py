"""RF-fault RECOVERY Phase 2 — client Restart button + wire (docs/rf-fault-recovery.md §7).

The agent (>= 1.29.0, capability sequence-restart) recovers a faulted run via
POST /sequence-runs/{id}/restart with a resync/replay mode. These pin the CLIENT: the model +
HTTP wrapper, the capability gate, the Restart button visibility (only on a faulted run with both
capabilities), the resync/replay choice routing, and the plan-row fault pill + Restart.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox as _QMB

from api import Fleet
from api import models as m
from api.client import AgentClient
import ui.timeline_model as tlm
import ui.sequences_panel as sp
import ui.plans_tab as pt

_app = QApplication.instance() or QApplication([])


# ── model + client wrapper + gate ────────────────────────────────────────────

def test_restart_request_model_defaults_resync():
    assert m.RestartRunRequest().mode == "resync"
    assert m.RestartRunRequest(mode="replay").mode == "replay"


def test_client_restart_posts_to_the_run(monkeypatch):
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    seen = {}

    def fake_request(method, path, json=None, **k):
        seen.update(method=method, path=path, json=json)
        return {"id": "r7", "sequence_id": "s1", "sequence_name": "n",
                "on_air_at": "2030-01-01T00:00:00+00:00", "state": "running", "fault": ""}

    monkeypatch.setattr(c, "_request", fake_request)
    run = c.restart_sequence_run("r7", m.RestartRunRequest(mode="replay"))
    assert seen["method"] == "POST" and seen["path"] == "/sequence-runs/r7/restart"
    assert seen["json"]["mode"] == "replay"
    assert run.state == m.SequenceState.RUNNING and run.fault == ""


def test_sequence_restart_supported_gate():
    class C:
        def __init__(self, caps): self._c = caps
        def supports(self, cap): return cap in self._c
    assert tlm.sequence_restart_supported(C(["sequence-restart"])) is True
    assert tlm.sequence_restart_supported(C([])) is False


# ── test doubles ─────────────────────────────────────────────────────────────

class _Client:
    def __init__(self, caps=("task-rf-health", "sequence-restart")):
        self._caps = set(caps)
        self.restarted = []

    def supports(self, cap):
        return cap in self._caps

    def restart_sequence_run(self, run_id, req):
        self.restarted.append((run_id, req.mode))
        return m.SequenceRun(id=run_id, sequence_id="s1", sequence_name="n",
                             on_air_at="2030-01-01T00:00:00+00:00",
                             state=m.SequenceState.RUNNING, fault="")


class _Fleet:
    def __init__(self, client): self._c = client
    def get(self, _host): return self._c


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)
    fast_update = pyqtSignal(object)       # PlansTab folds the poller snapshot in

    def __init__(self, client):
        super().__init__()
        self.fleet = _Fleet(client)
        self.calls = []
        self._last_fn = None

    def run_async(self, label, fn):
        self.calls.append(label)
        self._last_fn = fn


class _FakeBox:
    """A QMessageBox stand-in whose clickedButton() is controlled by `picked`."""
    picked = "resync"
    Icon = _QMB.Icon                       # the handler reads QMessageBox.Icon.* / ButtonRole.* /
    ButtonRole = _QMB.ButtonRole           # StandardButton.* off the (patched) class symbol
    StandardButton = _QMB.StandardButton

    def __init__(self, *a, **k):
        self._buttons = {}

    def setIcon(self, *a): pass
    def setWindowTitle(self, *a): pass
    def setText(self, *a): pass
    def setInformativeText(self, *a): pass
    def setDefaultButton(self, *a): pass
    def exec(self): return 0

    def addButton(self, *a, **k):
        label = a[0] if a and isinstance(a[0], str) else "cancel"
        btn = object()
        self._buttons[str(label).lower()] = btn
        return btn

    def clickedButton(self):
        return self._buttons.get(_FakeBox.picked)


def _faulted_run(fault="tx: vmcircbuf"):
    return m.SequenceRun(id="run-1", sequence_id="s", sequence_name="sweep",
                         state=m.SequenceState.RUNNING, on_air_at="2030-01-01T00:00:00+00:00",
                         plan_id="p", fault=fault, fault_task="tx")


# ── SequenceRow visibility + pill ────────────────────────────────────────────

def _seq_kw():
    return dict(on_start=lambda s: None, on_stop=lambda s: None, on_edit=lambda s: None,
                on_delete=lambda s: None, on_log=lambda s: None, can_run=True, can_edit=False)


def test_sequence_row_restart_visible_only_on_a_faulted_run_with_caps():
    seq = m.Sequence(id="s", name="sweep", steps=[])
    # faulted + restart_ok → visible + red pill.
    row = sp._SequenceRow(seq, _faulted_run(), on_restart=lambda s: None, restart_ok=True,
                          **_seq_kw())
    assert row._restart.isVisibleTo(row) is True and row._pill.text() == "RF FAULT"
    # faulted but no capability → hidden (pill still red — the pill never gates on the cap).
    row2 = sp._SequenceRow(seq, _faulted_run(), on_restart=lambda s: None, restart_ok=False,
                           **_seq_kw())
    assert row2._restart.isVisibleTo(row2) is False and row2._pill.text() == "RF FAULT"
    # healthy run → hidden, ordinary pill.
    ok = m.SequenceRun(id="r", sequence_id="s", sequence_name="sweep",
                       state=m.SequenceState.RUNNING, on_air_at="2030-01-01T00:00:00+00:00")
    row3 = sp._SequenceRow(seq, ok, on_restart=lambda s: None, restart_ok=True, **_seq_kw())
    assert row3._restart.isVisibleTo(row3) is False and row3._pill.text() == "RUNNING"


# ── SequencesPanel._on_restart routing (resync / replay / cancel) ────────────

def _seq_panel(client):
    p = sp.SequencesPanel("u", _Hub(client), can_edit=False, can_run=True)
    p._runs = [_faulted_run()]
    return p


def test_sequence_on_restart_routes_mode(monkeypatch):
    monkeypatch.setattr(sp, "QMessageBox", _FakeBox)
    seq = m.Sequence(id="s", name="sweep", steps=[])

    for picked, expect in (("resync", "resync"), ("replay", "replay")):
        client = _Client()
        p = _seq_panel(client)
        _FakeBox.picked = picked
        p._on_restart(seq)
        assert any(c.startswith("seq_restart:u:s") for c in p.hub.calls)
        p.hub._last_fn()                                    # run the queued op
        assert client.restarted == [("run-1", expect)]

    # Cancel → nothing fired.
    client = _Client()
    p = _seq_panel(client)
    _FakeBox.picked = "cancel"
    p._on_restart(seq)
    assert not any(c.startswith("seq_restart") for c in p.hub.calls)
    assert client.restarted == []


# ── PlanRow fault pill + Restart, and PlansTab._fault_run_for ─────────────────

def _plan_kw():
    return dict(on_arm=lambda p: None, on_stop=lambda p: None, on_edit=lambda p: None,
                on_delete=lambda p: None, on_log=lambda p: None)


def test_plan_row_restart_and_fault_pill():
    plan = m.Plan(id="p", name="daily", items=[])
    row = pt._PlanRow(plan, [_faulted_run()], 1, 0, on_restart=lambda p: None, can_restart=True,
                      **_plan_kw())
    assert row._restart.isVisibleTo(row) is True and row._pill.text() == "RF FAULT"
    # A fault with no capability: pill red, button hidden.
    row2 = pt._PlanRow(plan, [_faulted_run()], 1, 0, on_restart=lambda p: None, can_restart=False,
                       **_plan_kw())
    assert row2._restart.isVisibleTo(row2) is False and row2._pill.text() == "RF FAULT"
    # Healthy: ordinary pill, hidden button.
    ok = m.SequenceRun(id="r", sequence_id="s", sequence_name="x", plan_id="p",
                       state=m.SequenceState.RUNNING, on_air_at="2030-01-01T00:00:00+00:00")
    row3 = pt._PlanRow(plan, [ok], 1, 0, on_restart=lambda p: None, can_restart=True, **_plan_kw())
    assert row3._restart.isVisibleTo(row3) is False and row3._pill.text() != "RF FAULT"


def test_plans_tab_fault_run_for_finds_the_faulted_run():
    hub = _Hub(_Client())
    tab = pt.PlansTab(Fleet(), hub)
    plan = m.Plan(id="p", name="daily", items=[])
    tab._runs_by_host = {"u1": [_faulted_run()]}
    host, run = tab._fault_run_for(plan)
    assert host == "u1" and run.id == "run-1"
    # No fault → (None, None).
    tab._runs_by_host = {"u1": [m.SequenceRun(id="r", sequence_id="s", sequence_name="x",
                                              plan_id="p", state=m.SequenceState.RUNNING,
                                              on_air_at="2030-01-01T00:00:00+00:00")]}
    assert tab._fault_run_for(plan) == (None, None)
