"""RF-fault RECOVERY Phase 3 — client UNATTENDED auto-restart (docs/rf-fault-recovery.md §11/§14d).

The agent (>= 1.30.0, capability sequence-auto-restart) auto-fires the Phase-2 restart for a faulted
run armed with restart_policy="auto" (budget-limited), and trips loudly when the breaker is exhausted.
These pin the CLIENT half: the model fields (skew-safe defaults), the capability gate, the arm-time
policy resolution (auto downgraded when unsupported), the fault/recovery pill decision, the sequence
editor's recovery combo + save gate, the plan item's inherit-or-override resolution, and that all
three arm paths (sequence / plan / schedule) carry the policy.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
import ui.timeline_model as tlm
import ui.sequences_panel as sp
import ui.plans_tab as pt
import ui.timeline_tab as tt
from ui.sequence_editor import SequenceEditorDialog, _RECOVERY_CHOICES
from api.fleet import LIBRARY_HOST

_app = QApplication.instance() or QApplication([])


# ── model round-trip + skew-safe defaults ────────────────────────────────────

def test_models_default_to_manual_and_round_trip():
    # A sequence / arm / run from a pre-Phase-3 agent (no fields) deserializes to "manual".
    assert m.Sequence(id="s", name="n", steps=[]).recovery_policy == "manual"
    assert m.Sequence(id="s", name="n", steps=[]).recovery_mode == "resync"
    assert m.CreateSequenceRequest(name="n", steps=[]).recovery_policy == "manual"
    arm = m.ArmSequenceRequest(on_air_at="2030-01-01T00:00:00+00:00")
    assert arm.restart_policy == "manual" and arm.restart_mode == "resync"
    run = m.SequenceRun(id="r", sequence_id="s", sequence_name="n",
                        on_air_at="2030-01-01T00:00:00+00:00")
    assert run.restart_policy == "manual" and run.auto_restart_count == 0
    assert run.auto_restart_task == "" and run.auto_restart_healthy_since == ""
    # PlanItem's override is blank = inherit.
    assert m.PlanItem(hostname="u", sequence_id="s").recovery_policy == ""
    # Explicit values round-trip through JSON.
    seq = m.Sequence(**m.Sequence(id="s", name="n", steps=[], recovery_policy="auto",
                                  recovery_mode="replay").model_dump())
    assert seq.recovery_policy == "auto" and seq.recovery_mode == "replay"


# ── capability gate ──────────────────────────────────────────────────────────

class _Caps:
    def __init__(self, caps): self._c = set(caps)
    def supports(self, cap): return cap in self._c


def test_auto_restart_capability_gate():
    assert tlm.sequence_auto_restart_supported(_Caps(["sequence-auto-restart"])) is True
    assert tlm.sequence_auto_restart_supported(_Caps([])) is False
    # A client that raises is treated as unsupported (never crashes the gate).
    class _Boom:
        def supports(self, cap): raise RuntimeError("x")
    assert tlm.sequence_auto_restart_supported(_Boom()) is False


# ── arm-time policy resolution (downgrade when the unit can't act) ────────────

def test_resolve_arm_recovery_downgrades_auto_when_unsupported():
    sup = _Caps(["sequence-auto-restart"])
    assert tlm.resolve_arm_recovery(sup, "auto", "replay") == ("auto", "replay")
    # Auto on an unsupported unit → manual (never over-claims), mode carried for round-trip.
    unsup = _Caps([])
    assert tlm.resolve_arm_recovery(unsup, "auto", "replay") == ("manual", "replay")
    # A manual policy passes through regardless of capability.
    assert tlm.resolve_arm_recovery(unsup, "manual", "resync") == ("manual", "resync")
    # Blank inputs coerce to the manual/resync default.
    assert tlm.resolve_arm_recovery(sup, "", "") == ("manual", "resync")


# ── the fault / recovery pill decision ───────────────────────────────────────

def _run(**kw):
    base = dict(id="r", sequence_id="s", sequence_name="n",
                on_air_at="2030-01-01T00:00:00+00:00", state=m.SequenceState.RUNNING)
    base.update(kw)
    return m.SequenceRun(**base)


def test_fault_pill_decision():
    # A plain (manual-policy) fault → red RF FAULT.
    label, kind, tip = tlm.fault_pill(_run(fault="tx: vmcircbuf"))
    assert label == "RF FAULT" and kind == "rf_fault" and "vmcircbuf" in tip
    # An auto-policy fault that spent attempts → red, tooltip names the attempt count (no
    # over-claim of finality — the client can't know the agent's budget).
    label, kind, tip = tlm.fault_pill(
        _run(fault="tx: vmcircbuf", restart_policy="auto", auto_restart_count=2))
    assert label == "RF FAULT" and kind == "rf_fault" and "2 auto-restart attempt" in tip
    assert "gave up" not in tip
    # Recovered (no fault, count>0) → amber AUTO-RESTART ×n.
    label, kind, tip = tlm.fault_pill(_run(restart_policy="auto", auto_restart_count=3))
    assert label == "AUTO-RESTART ×3" and kind == "auto_restart" and "3" in tip
    # Healthy, never restarted → nothing to flag.
    assert tlm.fault_pill(_run()) is None


def test_auto_restart_status_is_amber_in_theme():
    from ui.theme import status_color, Palette
    assert status_color("auto_restart") == (Palette.ARMED, Palette.ARMED_SOFT)


# ── test doubles ─────────────────────────────────────────────────────────────

class _Client:
    def __init__(self, caps=("sequence-auto-restart",), seq=None):
        self._caps = set(caps)
        self._seq = seq
        self.skew = 0.0
        self.armed = []
        self.created = []

    def supports(self, cap): return cap in self._caps
    def clock_offset_s(self): return self.skew
    def get_sequence(self, _sid): return self._seq

    def arm_sequence(self, seq_id, req):
        self.armed.append((seq_id, req))
        return _run(id="run-armed", plan_id=req.plan_id)

    def create_sequence(self, req):
        self.created.append(req)
        return m.Sequence(id="s2", name=req.name, steps=req.steps,
                          recovery_policy=req.recovery_policy, recovery_mode=req.recovery_mode)


class _Fleet:
    def __init__(self, client): self._c = client
    def get(self, _host): return self._c


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)
    fast_update = pyqtSignal(object)

    def __init__(self, client):
        super().__init__()
        self.fleet = _Fleet(client)
        self.calls = []
        self._last_fn = None

    def run_async(self, label, fn):
        self.calls.append(label)
        self._last_fn = fn


# ── _arm_at carries the resolved policy (sequences panel / library arm) ───────

def test_arm_at_sends_resolved_policy():
    from datetime import datetime, timezone
    seq = m.Sequence(id="s", name="n", steps=[], recovery_policy="auto", recovery_mode="replay")
    c = _Client(caps=["sequence-auto-restart"])
    sp._arm_at(c, seq, datetime(2030, 1, 1, tzinfo=timezone.utc), duration_s=60.0)
    _sid, req = c.armed[-1]
    assert req.restart_policy == "auto" and req.restart_mode == "replay"
    # Same sequence to a unit that can't auto-restart → downgraded to manual.
    c2 = _Client(caps=[])
    sp._arm_at(c2, seq, datetime(2030, 1, 1, tzinfo=timezone.utc), duration_s=60.0)
    assert c2.armed[-1][1].restart_policy == "manual"


# ── _item_recovery: item override, else inherit the stored sequence ──────────

def test_item_recovery_inherits_or_overrides():
    stored = m.Sequence(id="s", name="n", steps=[], recovery_policy="auto", recovery_mode="resync")
    client = _Client(caps=["sequence-auto-restart"], seq=stored)
    fleet = _Fleet(client)
    # Blank item → inherit the stored sequence's authored policy.
    item = m.PlanItem(hostname="u", sequence_id="s")
    assert pt._item_recovery(fleet, client, item) == ("auto", "resync")
    # Item override wins over the stored sequence (no fetch needed).
    over = m.PlanItem(hostname="u", sequence_id="s", recovery_policy="manual")
    assert pt._item_recovery(fleet, client, over) == ("manual", "resync")
    # Inherited auto on a unit that can't act → downgraded to manual.
    unsup = _Client(caps=[], seq=stored)
    assert pt._item_recovery(_Fleet(unsup), unsup, item) == ("manual", "resync")


# ── all three plan/schedule arm paths carry the policy ───────────────────────

def _plan_with_auto_seq(client):
    """A plan whose item inherits an auto sequence, armable through the fake fleet."""
    return m.Plan(id="p", name="daily",
                  items=[m.PlanItem(hostname="u", sequence_id="s")])


def test_arm_plan_and_schedule_carry_policy():
    from datetime import datetime, timezone
    stored = m.Sequence(id="s", name="n", steps=[
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="tx"),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="tx"),
    ], recovery_policy="auto", recovery_mode="resync")
    client = _Client(caps=["sequence-auto-restart"], seq=stored)
    fleet = _Fleet(client)
    plan = _plan_with_auto_seq(client)
    t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
    # Direct plan arm.
    pt._arm_plan(fleet, plan, t0, duration_s=60.0)
    assert client.armed[-1][1].restart_policy == "auto"
    # Scheduled arm (the primary unattended surface).
    client.armed.clear()
    tt._arm_scheduled(fleet, plan, t0, datetime(2030, 1, 1, 1, tzinfo=timezone.utc))
    assert client.armed[-1][1].restart_policy == "auto"


# ── the sequence editor's recovery combo + save gate ─────────────────────────

def _seq_editor(hostname, client, sequence=None):
    return SequenceEditorDialog(_Hub(client), hostname, sequence=sequence)


def test_recovery_choices_cover_manual_and_both_auto_modes():
    pols = {(pol, mode) for _label, pol, mode in _RECOVERY_CHOICES}
    assert ("manual", "resync") in pols
    assert ("auto", "resync") in pols
    assert ("auto", "replay") in pols


def test_sequence_editor_loads_and_saves_recovery():
    seq = m.Sequence(id="s", name="sweep", steps=[], recovery_policy="auto", recovery_mode="replay")
    dlg = _seq_editor(LIBRARY_HOST, _Client(), sequence=seq)
    try:
        # Loaded: the combo reflects the stored auto/replay choice.
        assert dlg._recovery_choice() == ("auto", "replay")
        # Switch to operator-restart and read it back.
        dlg._set_recovery("manual", "resync")
        assert dlg._recovery_choice() == ("manual", "resync")
    finally:
        dlg.deleteLater()


def test_sequence_editor_auto_gate_blocks_unsupported_unit():
    seq = m.Sequence(id="s", name="sweep", steps=[], recovery_policy="auto", recovery_mode="resync")
    # On the Library (a definition, not a real unit) an auto policy is never blocked.
    lib = _seq_editor(LIBRARY_HOST, _Client(caps=[]), sequence=seq)
    try:
        assert lib._auto_restart_block() is None
    finally:
        lib.deleteLater()
    # On a real unit whose agent lacks the capability → blocked with a clear message.
    unsup = _seq_editor("u", _Client(caps=[]), sequence=seq)
    try:
        assert unsup._recovery_choice() == ("auto", "resync")
        msg = unsup._auto_restart_block()
        assert msg is not None and "auto-restart" in msg
    finally:
        unsup.deleteLater()
    # On a supporting unit → allowed.
    sup = _seq_editor("u", _Client(caps=["sequence-auto-restart"]), sequence=seq)
    try:
        assert sup._auto_restart_block() is None
    finally:
        sup.deleteLater()
    # A manual policy is never blocked, even on an unsupported unit.
    seq_manual = m.Sequence(id="s", name="sweep", steps=[], recovery_policy="manual")
    man = _seq_editor("u", _Client(caps=[]), sequence=seq_manual)
    try:
        assert man._auto_restart_block() is None
    finally:
        man.deleteLater()


# ── the row pills reflect the recovery state ─────────────────────────────────

def _seq_kw():
    return dict(on_start=lambda s: None, on_stop=lambda s: None, on_edit=lambda s: None,
                on_delete=lambda s: None, on_log=lambda s: None, can_run=True, can_edit=False)


def test_sequence_row_shows_auto_restart_pill():
    seq = m.Sequence(id="s", name="sweep", steps=[])
    # Recovered (healthy, count>0) → amber AUTO-RESTART pill.
    recovered = _run(restart_policy="auto", auto_restart_count=2, plan_id="p")
    row = sp._SequenceRow(seq, recovered, **_seq_kw())
    assert row._pill.text() == "AUTO-RESTART ×2"
    # A tripped auto fault → red RF FAULT (an operator Restart is still offered elsewhere).
    tripped = _run(fault="tx: vmcircbuf", restart_policy="auto", auto_restart_count=2, plan_id="p")
    row2 = sp._SequenceRow(seq, tripped, on_restart=lambda s: None, restart_ok=True, **_seq_kw())
    assert row2._pill.text() == "RF FAULT" and row2._restart.isVisibleTo(row2) is True


def _plan_kw():
    return dict(on_arm=lambda p: None, on_stop=lambda p: None, on_edit=lambda p: None,
                on_delete=lambda p: None, on_log=lambda p: None)


def test_plan_row_shows_auto_restart_pill():
    plan = m.Plan(id="p", name="daily", items=[])
    recovered = _run(restart_policy="auto", auto_restart_count=1, plan_id="p")
    row = pt._PlanRow(plan, [recovered], 1, 0, **_plan_kw())
    assert row._pill.text() == "AUTO-RESTART ×1"


# ── the authored policy survives the round-trip (persistence — the feature-defeating gaps) ──

def test_library_client_round_trips_recovery_policy():
    """The offline LibraryClient must persist recovery_policy/recovery_mode — dropping them here
    would lose the authored policy for the primary plan-authoring surface (a plan item inheriting
    from a library sequence would resolve to 'manual')."""
    import tempfile
    from state.library_client import LibraryClient
    from state.library_store import LibraryStore
    with tempfile.TemporaryDirectory() as d:
        store = LibraryStore(f"{d}/lib.json")
        store.upsert_task(m.TaskConfig(name="tx", command=["python3", "tx.py"]))
        client = LibraryClient(store)
        req = m.CreateSequenceRequest(name="sweep", steps=[
            m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="tx"),
            m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="tx"),
        ], recovery_policy="auto", recovery_mode="replay")
        seq = client.create_sequence(req)
        assert seq.recovery_policy == "auto" and seq.recovery_mode == "replay"
        # And it survives a fetch back (the inherit path reads it from the stored sequence).
        back = client.get_sequence(seq.id)
        assert back.recovery_policy == "auto" and back.recovery_mode == "replay"
        # An update preserves it too.
        upd = client.update_sequence(seq.id, m.CreateSequenceRequest(
            name="sweep", steps=req.steps, recovery_policy="auto", recovery_mode="resync"))
        assert upd.recovery_policy == "auto" and upd.recovery_mode == "resync"


def test_sequence_editor_on_save_copies_recovery_policy_onto_the_request(monkeypatch):
    """The editor's _on_save must copy the chosen recovery policy onto the CreateSequenceRequest —
    else the authored auto policy never reaches the store."""
    dlg = _seq_editor(LIBRARY_HOST, _Client(), sequence=None)
    try:
        dlg._name.setText("sweep")
        dlg._set_recovery("auto", "replay")
        monkeypatch.setattr(dlg, "_current_error", lambda: None)   # bypass step validation
        dlg._on_save()
        dlg.hub._last_fn()                                         # run the queued create
        req = dlg.hub.fleet.get(LIBRARY_HOST).created[-1]
        assert req.recovery_policy == "auto" and req.recovery_mode == "replay"
    finally:
        dlg.deleteLater()
