"""All three arm paths — single sequence, manual plan, scheduled plan — must translate
the operator's laptop-UTC on-air time to each unit's clock, so a clock-skewed unit
(a Pi with no NTP) still goes on air at the intended wall-clock instant."""
from datetime import datetime, timedelta, timezone

from api import models as m
from ui.sequences_panel import _arm_at
from ui.plans_tab import _arm_plan
from ui.timeline_tab import _arm_scheduled


def _iso(dt):
    return dt.isoformat()


class _Client:
    """A unit whose clock is `skew` seconds ahead of this PC."""
    def __init__(self, skew, host="u"):
        self.skew = skew
        self.hostname = host
        self.captured = []

    def clock_offset_s(self):
        return self.skew

    def arm_sequence(self, seq_id, req):
        self.captured.append(req)
        return req                       # tests only inspect the request we sent


class _Fleet:
    def __init__(self, clients):
        self._c = clients

    def get(self, host):
        return self._c[host]


def test_single_sequence_arm_translates_skew():
    c = _Client(skew=30.0)
    t0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _arm_at(c, m.Sequence(id="s", name="s", steps=[]), t0, duration_s=None)
    sent = datetime.fromisoformat(c.captured[0].on_air_at)
    assert sent == t0 + timedelta(seconds=30)      # unit +30s → send +30s


def test_manual_plan_arm_translates_skew_per_unit():
    fleet = _Fleet({"a": _Client(skew=10.0, host="a"),
                    "b": _Client(skew=-5.0, host="b")})
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", on_air_offset_s=0),
        m.PlanItem(hostname="b", sequence_id="s2", on_air_offset_s=60),
    ])
    t0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _arm_plan(fleet, plan, t0, duration_s=None)
    a = datetime.fromisoformat(fleet.get("a").captured[0].on_air_at)
    b = datetime.fromisoformat(fleet.get("b").captured[0].on_air_at)
    assert a == t0 + timedelta(seconds=10)             # unit a +10s
    assert b == t0 + timedelta(seconds=60 - 5)         # offset 60 + unit b −5s


def test_scheduled_plan_arm_translates_skew_on_both_edges():
    fleet = _Fleet({"a": _Client(skew=20.0, host="a")})
    plan = m.Plan(id="p", name="p", items=[
        m.PlanItem(hostname="a", sequence_id="s1", on_air_offset_s=0, off_air_offset_s=0)])
    start = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    stop = start + timedelta(minutes=10)
    _arm_scheduled(fleet, plan, start, stop)
    req = fleet.get("a").captured[0]
    assert datetime.fromisoformat(req.on_air_at) == start + timedelta(seconds=20)
    assert datetime.fromisoformat(req.on_air_end) == stop + timedelta(seconds=20)
