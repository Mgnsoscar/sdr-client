"""Schedule tab — "Arm all (N)": arm every not-yet-started, idle plan of the shown day in
one go, with ONE confirmation, chronologically, a refused plan never stopping the rest.

Pairs with sdr-agent 1.27.3, whose arm guards admit a later, non-overlapping window while
an earlier scheduled plan is already on air (the owner's four-plans-a-day case). The client
side is agent-agnostic: an older agent simply refuses the later arm and the refusal is
reported per plan.
"""
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from state.plan_store import PlanStore
from state.schedule_store import ScheduleStore
import ui.timeline_tab as tt

_app = QApplication.instance() or QApplication(sys.argv)


# ── fakes ──────────────────────────────────────────────────────────────────────

class _Client:
    """A unit: records every arm request; refuses the sequence ids in `refuse` the way an
    agent does (an exception with its reason); reports what it armed as runs."""

    def __init__(self, host, skew=0.0, refuse=()):
        self.hostname = host
        self.skew = skew
        self.refuse = set(refuse)
        self.captured = []
        self.runs = []

    def clock_offset_s(self):
        return self.skew

    def arm_sequence(self, seq_id, req):
        self.captured.append((seq_id, req))
        if seq_id in self.refuse:
            raise RuntimeError("cannot arm: on-air window overlaps run run_1 already on this unit")
        run = m.SequenceRun(id=f"run_{len(self.runs) + 1}", sequence_id=seq_id,
                            sequence_name=seq_id, state=m.SequenceState.ARMED,
                            on_air_at=req.on_air_at, on_air_end=req.on_air_end,
                            plan_id=req.plan_id, plan_name=req.plan_name)
        self.runs.append(run)
        return run


class _Fleet:
    def __init__(self, clients):
        self._c = {c.hostname: c for c in clients}
        self.calls = []

    def __contains__(self, host):
        return host in self._c

    def __len__(self):
        return len(self._c)

    def get(self, host):
        return self._c[host]

    def clock_skew(self, hosts):
        self.calls.append(("clock_skew", tuple(hosts)))
        return ({h: 0.0 for h in hosts}, 0.0)

    def sequences_all(self, hosts):
        self.calls.append(("sequences_all", tuple(hosts)))
        return {h: [] for h in hosts}

    def list_runs_all(self):
        self.calls.append(("list_runs_all",))
        return {h: list(c.runs) for h, c in self._c.items()}


class _Hub(QObject):
    """Runs the worker synchronously and delivers its result like the real hub."""
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)
    fast_update = pyqtSignal(object)

    def __init__(self, fleet):
        super().__init__()
        self.fleet = fleet
        self.labels = []

    def run_async(self, label, fn):
        self.labels.append(label)
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


TOMORROW = date.today() + timedelta(days=1)


def _local(d, hh):
    return datetime.combine(d, time(hh, 0)).isoformat()


def _plan(pid, name, host="u", seq="s1"):
    return m.Plan(id=pid, name=name, items=[
        m.PlanItem(hostname=host, unit_label=host.upper(), sequence_id=seq, sequence_name=seq)])


def _entry(eid, pid, d, start_h, stop_h):
    return m.ScheduledPlan(id=eid, plan_id=pid, start=_local(d, start_h), stop=_local(d, stop_h))


@pytest.fixture
def boxes(monkeypatch):
    """Answer every dialog: Yes to a question; record the texts."""
    rec = {"question": [], "warning": [], "information": []}

    def _q(*a, **k):
        rec["question"].append(a[2])
        return tt.QMessageBox.StandardButton.Yes

    monkeypatch.setattr(tt.QMessageBox, "question", staticmethod(_q))
    monkeypatch.setattr(tt.QMessageBox, "warning", staticmethod(lambda *a, **k: rec["warning"].append(a[2])))
    monkeypatch.setattr(tt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: rec["information"].append(a[2])))
    return rec


@pytest.fixture
def make_tab(tmp_path, monkeypatch):
    """A TimelineTab on tmp-path stores, wired to a synchronous fake hub, showing TOMORROW
    with the given plans + entries."""
    monkeypatch.setattr(tt, "ScheduleStore", lambda: ScheduleStore(tmp_path / "schedule.json"))
    monkeypatch.setattr(tt, "PlanStore", lambda: PlanStore(tmp_path / "plans.json"))

    def _make(plans, entries, clients=None, day=TOMORROW):
        fleet = _Fleet(clients or [_Client("u")])
        hub = _Hub(fleet)
        tab = tt.TimelineTab(hub)
        for p in plans:
            tab._plans.upsert(p)
        for e in entries:
            tab._store.upsert(e)
        tab._timeline_day = day
        tab._reload()
        return tab, hub, fleet

    return _make


def _two_plans():
    plans = [_plan("p1", "Morning", seq="s1"), _plan("p2", "Afternoon", seq="s2")]
    entries = [_entry("e2", "p2", TOMORROW, 12, 13), _entry("e1", "p1", TOMORROW, 10, 11)]
    return plans, entries


# ── the worker ─────────────────────────────────────────────────────────────────

def test_arm_scheduled_many_arms_in_start_order_and_continues_past_a_refusal():
    u = _Client("u", refuse={"s2"})
    x = _Client("x")
    x.clock_offset_s = lambda: (_ for _ in ()).throw(ConnectionError("unit x unreachable"))
    fleet = _Fleet([u, x])
    t0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    p1, p2 = _plan("p1", "Morning", seq="s1"), _plan("p2", "Afternoon", seq="s2")
    p3 = _plan("p3", "Night", host="x", seq="s3")
    e1, e2, e3 = (m.ScheduledPlan(id=f"e{i}", plan_id=f"p{i}", start="x", stop="y") for i in (1, 2, 3))
    jobs = [(e2, p2, t0 + timedelta(hours=2), t0 + timedelta(hours=3)),   # out of order on purpose
            (e3, p3, t0 + timedelta(hours=4), t0 + timedelta(hours=5)),
            (e1, p1, t0, t0 + timedelta(hours=1))]
    out = tt._arm_scheduled_many(fleet, jobs)
    assert [en.id for en, _p, _r in out] == ["e1", "e2", "e3"]          # chronological
    assert [s for s, _req in u.captured] == ["s1", "s2"]                 # the refusal didn't stop s2's attempt…
    r1, r2, r3 = (r for _e, _p, r in out)
    assert r1[0][2] is None and r1[0][1] is not None                     # p1 armed
    assert "overlaps run" in r2[0][2] and r2[0][1] is None               # p2 refused, reason kept
    assert isinstance(r3, str) and "unreachable" in r3                   # a whole-plan failure is a string
    assert datetime.fromisoformat(u.captured[0][1].on_air_at) < datetime.fromisoformat(u.captured[1][1].on_air_at)


# ── the button ────────────────────────────────────────────────────────────────

def test_arm_all_counts_only_idle_not_yet_started_entries_with_a_plan(make_tab):
    plans, entries = _two_plans()
    plans.append(_plan("p3", "Evening", seq="s3"))
    entries.append(_entry("e3", "p3", TOMORROW, 14, 15))                 # will be ARMED already
    entries.append(_entry("e4", "gone", TOMORROW, 16, 17))               # its plan no longer exists
    tab, hub, fleet = make_tab(plans, entries)
    assert tab._tl_arm_all.text() == "Arm all (3)" and tab._tl_arm_all.isEnabled()
    # e3 gains an active run → it's armed, not idle → left alone by Arm all.
    on_air = tt._to_utc(_local(TOMORROW, 14)).isoformat()
    tab._runs_by_host = {"u": [m.SequenceRun(id="rx", sequence_id="s3", sequence_name="s3",
                                             state=m.SequenceState.ARMED, on_air_at=on_air, plan_id="p3")]}
    tab._refresh_planner(scroll=False)
    assert tab._tl_arm_all.text() == "Arm all (2)"
    assert [e.id for e in tab._armable_entries(TOMORROW)] == ["e1", "e2"]   # start order
    # A day whose plans have already started (yesterday) has nothing to arm.
    yesterday = date.today() - timedelta(days=1)
    tab._store.upsert(_entry("e0", "p1", yesterday, 10, 11))
    tab._timeline_day = yesterday
    tab._refresh_planner(scroll=False)
    assert tab._tl_arm_all.text() == "Arm all" and not tab._tl_arm_all.isEnabled()


def test_arm_all_arms_every_idle_plan_of_the_day_with_one_confirm(make_tab, boxes):
    plans, entries = _two_plans()
    tab, hub, fleet = make_tab(plans, entries)
    tab._tl_arm_all.click()
    # One preflight over the day's units, ONE confirmation naming both plans in start order…
    assert hub.labels[:2] == [f"tl_preflight_all:{TOMORROW.isoformat()}", f"tl_arm_all:{TOMORROW.isoformat()}"]
    assert ("clock_skew", ("u",)) in fleet.calls and ("sequences_all", ("u",)) in fleet.calls
    assert len(boxes["question"]) == 1 and not boxes["warning"]
    q = boxes["question"][0]
    assert "Arm 2 plan(s)" in q and q.index("10:00 → 11:00   Morning") < q.index("12:00 → 13:00   Afternoon")
    # …then both armed chronologically at their own absolute windows.
    u = fleet.get("u")
    assert [s for s, _r in u.captured] == ["s1", "s2"]
    reqs = [r for _s, r in u.captured]
    assert [r.plan_id for r in reqs] == ["p1", "p2"] and all(r.open_ended is False for r in reqs)
    assert datetime.fromisoformat(reqs[0].on_air_at) == tt._to_utc(_local(TOMORROW, 10))
    assert datetime.fromisoformat(reqs[1].on_air_end) == tt._to_utc(_local(TOMORROW, 13))
    # The run picture was refreshed: both blocks now read armed and the button has nothing left.
    assert "tl_runs" in hub.labels
    assert tab._entry_state(tab._store.get("e1")) == "armed"
    assert tab._entry_state(tab._store.get("e2")) == "armed"
    assert tab._tl_arm_all.text() == "Arm all" and not tab._tl_arm_all.isEnabled()


def test_a_refused_plan_is_reported_and_the_others_still_arm(make_tab, boxes):
    plans, entries = _two_plans()
    tab, hub, fleet = make_tab(plans, entries, clients=[_Client("u", refuse={"s2"})])
    tab._tl_arm_all.click()
    assert len(boxes["warning"]) == 1
    w = boxes["warning"][0]
    assert "Armed 1 of 2 plan(s)" in w and "Afternoon" in w and "overlaps run" in w
    assert "Morning" not in w.split("Failed:")[1]
    assert tab._entry_state(tab._store.get("e1")) == "armed"
    assert tab._entry_state(tab._store.get("e2")) == "idle"
    assert tab._tl_arm_all.text() == "Arm all (1)"                       # the refused one can be retried


def test_an_entry_whose_unit_is_not_in_the_fleet_is_skipped_not_fatal(make_tab, boxes):
    plans, entries = _two_plans()
    plans[1] = _plan("p2", "Afternoon", host="ghost", seq="s2")
    tab, hub, fleet = make_tab(plans, entries)
    tab._tl_arm_all.click()
    q = boxes["question"][0]
    assert "Arm 1 plan(s)" in q and "Skipped" in q and "Afternoon" in q and "GHOST" in q
    assert [s for s, _r in fleet.get("u").captured] == ["s1"]
    assert not boxes["warning"]


def test_arm_all_with_nothing_armable_just_says_so(make_tab, boxes):
    tab, hub, fleet = make_tab([], [])
    assert not tab._tl_arm_all.isEnabled()
    tab._on_arm_all()
    assert boxes["information"] and "already started or is armed" in boxes["information"][0]
    assert not hub.labels[1:] if hub.labels else True                    # no preflight was started
