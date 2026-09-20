"""ui/plan_graph.py — the plan editor's timing graph (pure, no Qt).

Every plan item edge and step edge resolves to (clock, seconds) on the plan's on-air / off-air
clock; cross-item anchors chain; loops / missing targets resolve to None with a named fault;
compile_plan turns it all into absolute instants and rewrites cross-item step anchors to plain
on-air offsets of their own sequence.
"""
from datetime import datetime, timedelta, timezone

import pytest

from api import models as m
from ui import plan_graph as pg


def _steps(task="tx", ramp_id="up", start_id="st"):
    return [
        m.SequenceStep(id=start_id, anchor="start", offset_s=-1, action="start", task_name=task,
                       args=["--rf", "off"], replace_args=True),
        m.SequenceStep(id=start_id + "~stop", anchor="stop", offset_s=1, action="stop", task_name=task),
        m.SequenceStep(anchor="start", offset_s=0, action="tune", task_name=task, params={"rf": "on"}),
        m.SequenceStep(id=ramp_id, anchor="start", offset_s=10, action="ramp", task_name=task,
                       ramp=m.RampSpec(param="power", start=-90, stop=-50, steps=4, duration_s=150)),
        m.SequenceStep(anchor="stop", offset_s=0, action="tune", task_name=task, params={"rf": "off"}),
    ]


def _item(iid, host="u1", **kw):
    base = dict(id=iid, hostname=host, unit_label=host, sequence_id="s", sequence_name=iid,
                steps=_steps(ramp_id=iid + "-up", start_id=iid + "-st"))
    base.update(kw)
    return m.PlanItem(**base)


T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_plan_anchored_defaults_reproduce_the_old_offsets():
    it = _item("a", on_air_offset_s=20.0, off_air_offset_s=-40.0)
    g = pg.PlanGraph([it])
    assert g.item_timing("a") == pg.ItemTiming(("on", 20.0), ("off", -40.0))
    assert not pg.plan_uses_anchors([it])


def test_item_edges_chain_through_another_item_and_a_fixed_length():
    a = _item("a", off_air_anchor="item", off_air_anchor_item="", off_air_anchor_edge="on",
              off_air_offset_s=450.0)                       # fixed length: off = own on + 450
    b = _item("b", on_air_anchor="item", on_air_anchor_item="a", on_air_anchor_edge="off",
              on_air_offset_s=30.0)                          # 30 s after a's off-air
    g = pg.PlanGraph([a, b])
    assert g.item_timing("a") == pg.ItemTiming(("on", 0.0), ("on", 450.0))
    assert g.item_timing("b").on == ("on", 480.0)
    assert g.item_timing("b").off == ("off", 0.0)
    assert pg.plan_uses_anchors([a, b])


def test_step_edges_and_cross_item_step_anchors_resolve():
    a = _item("a")
    b = _item("b", host="u2", on_air_anchor="step", on_air_anchor_item="a", on_air_anchor_step="a-up",
              on_air_anchor_edge="end", on_air_offset_s=60.0)
    # b's down ramp hangs off a's up-ramp END + 120 (cross-item step → step)
    b.steps.append(m.SequenceStep(anchor="step", anchor_item="a", anchor_step_id="a-up", anchor_edge="end",
                                  offset_s=120.0, action="ramp", task_name="tx",
                                  ramp=m.RampSpec(param="power", start=-50, stop=-90, steps=4, duration_s=150)))
    g = pg.PlanGraph([a, b])
    assert g.step_edge_by_id("a", "a-up", "start") == ("on", 10.0)
    assert g.step_edge_by_id("a", "a-up", "end") == ("on", 160.0)          # start + duration
    assert g.item_timing("b").on == ("on", 220.0)                            # 60 after the ramp end
    assert g.step_edge("b", len(b.steps) - 1, "start") == ("on", 280.0)     # 120 after the ramp end
    assert g.step_edge("b", len(b.steps) - 1, "end") == ("on", 430.0)
    # a blank step id addresses the item's own window edge
    assert g.step_edge_by_id("b", "", "start") == ("on", 220.0)
    assert g.step_edge_by_id("b", "", "end") == ("off", 0.0)
    # the stop-anchored RF-off tune of b lives on the off clock
    assert g.step_edge("b", 4, "start") == ("off", 0.0)


def test_a_loop_and_a_missing_target_are_faults_not_crashes():
    a = _item("a", on_air_anchor="item", on_air_anchor_item="b", on_air_anchor_edge="on")
    b = _item("b", on_air_anchor="item", on_air_anchor_item="a", on_air_anchor_edge="on")
    g = pg.PlanGraph([a, b])
    assert g.item_edge("a", "on") is None and g.item_edge("b", "on") is None
    assert "loop" in (g.describe_fault("a") or "")
    c = _item("c", on_air_anchor="item", on_air_anchor_item="zz", on_air_anchor_edge="on")
    g2 = pg.PlanGraph([c])
    assert "no longer in the plan" in (g2.describe_fault("c") or "")
    assert g2.first_fault()
    # a step anchored to a missing step in another item
    d = _item("d")
    d.steps.append(m.SequenceStep(anchor="step", anchor_item="e", anchor_step_id="nope", anchor_edge="end",
                                  offset_s=1, action="tune", task_name="tx", params={"x": 1}))
    e = _item("e")
    g3 = pg.PlanGraph([d, e])
    assert g3.step_edge("d", len(d.steps) - 1, "start") is None
    assert "anchors to something that can't be timed" in (g3.describe_fault("d") or "")


def test_extents_round_up_to_whole_minutes_with_room():
    a = _item("a", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=450.0)
    g = pg.PlanGraph([a])
    fwd, bwd = g.extents()
    assert fwd == 540.0            # 451 (stop tune at off+1 → on clock 451) + 30 → next minute
    assert bwd == 60.0             # nothing on the off clock → the 30 s pad rounds to a minute


def test_earliest_on_clock_is_the_items_warm_up():
    a = _item("a", on_air_offset_s=100.0)                 # its START step fires 1 s early
    g = pg.PlanGraph([a])
    assert g.earliest_on_clock_s("a") == 99.0
    b = _item("b", on_air_anchor="plan", on_air_anchor_edge="off", on_air_offset_s=-300.0)
    assert pg.PlanGraph([b]).earliest_on_clock_s("b") is None       # nothing on the on clock


def test_pack_lanes_and_channel_conflicts():
    lanes = pg.pack_lanes([("a", 0, 100), ("b", 50, 150), ("c", 120, 200)])
    assert lanes == {"a": 0, "b": 1, "c": 0}
    spans = {"a": (0.0, 100.0), "b": (50.0, 150.0), "c": (50.0, 150.0)}
    assert pg.channel_conflicts(spans, {"a": "u1", "b": "u1", "c": "u2"}) == {"a", "b"}
    assert pg.channel_conflicts({"a": (0.0, 100.0), "b": (100.0, 200.0)}, {"a": "u", "b": "u"}) == set()


def test_compile_rewrites_cross_item_step_anchors_to_on_air_offsets():
    a = _item("a")
    b = _item("b", host="u2", on_air_anchor="step", on_air_anchor_item="a", on_air_anchor_step="a-up",
              on_air_anchor_edge="end", on_air_offset_s=60.0)
    b.steps.append(m.SequenceStep(anchor="step", anchor_item="a", anchor_step_id="a-up", anchor_edge="end",
                                  offset_s=120.0, action="tune", task_name="tx", params={"x": 1}))
    b.steps.append(m.SequenceStep(id="b-t2", anchor="step", anchor_step_id="b-up", anchor_edge="end",
                                  offset_s=5.0, action="tune", task_name="tx", params={"y": 1}))
    t_end = T0 + timedelta(seconds=900)
    ca, cb = pg.compile_plan([a, b], T0, t_end)
    assert ca.on_air_at == T0 and ca.off_air_at == t_end
    assert cb.on_air_at == T0 + timedelta(seconds=220)
    cross = cb.steps[-2]
    assert cross.anchor == "start" and cross.anchor_item == "" and cross.anchor_step_id == ""
    assert cross.offset_s == pytest.approx(280.0 - 220.0)          # relative to b's own on-air
    same = cb.steps[-1]
    assert same.anchor == "step" and same.anchor_step_id == "b-up"   # same-sequence: the agent's job
    assert [s.anchor for s in ca.steps] == [s.anchor for s in a.steps]


def test_compile_open_ended_keeps_a_fixed_length_and_refuses_an_off_clock_on_air():
    a = _item("a", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=450.0)
    (ca,) = pg.compile_plan([a], T0, None)
    assert ca.off_air_at == T0 + timedelta(seconds=450)
    b = _item("b")
    (cb,) = pg.compile_plan([b], T0, None)
    assert cb.off_air_at is None                                 # floats with an open-ended arm
    c = _item("c", on_air_anchor="plan", on_air_anchor_edge="off", on_air_offset_s=-120.0)
    with pytest.raises(pg.PlanResolveError):
        pg.compile_plan([c], T0, None)
    assert pg.compile_plan([c], T0, T0 + timedelta(seconds=600))[0].on_air_at == T0 + timedelta(seconds=480)


def test_compile_raises_on_a_loop_and_keeps_steps_less_items():
    a = _item("a", on_air_anchor="item", on_air_anchor_item="b", on_air_anchor_edge="on")
    b = _item("b", on_air_anchor="item", on_air_anchor_item="a", on_air_anchor_edge="on")
    with pytest.raises(pg.PlanResolveError):
        pg.compile_plan([a, b], T0, None)
    legacy = m.PlanItem(hostname="u", sequence_id="s", on_air_offset_s=15.0)     # no id, no steps
    (cl,) = pg.compile_plan([legacy], T0, None)
    assert cl.steps is None and cl.on_air_at == T0 + timedelta(seconds=15)


def test_ensure_item_ids_and_models_round_trip_the_anchor_fields():
    items = [m.PlanItem(hostname="u", sequence_id="s"), m.PlanItem(id="keep", hostname="u", sequence_id="s")]
    pg.ensure_item_ids(items)
    assert items[0].id.startswith(pg.ITEM_ID_PREFIX) and items[1].id == "keep"
    it = _item("a", on_air_anchor="step", on_air_anchor_item="b", on_air_anchor_step="x",
               on_air_anchor_edge="end", expanded=False)
    it.steps[2] = it.steps[2].model_copy(update={"anchor": "step", "anchor_item": "b", "anchor_step_id": "x"})
    back = m.PlanItem(**it.model_dump(mode="json"))
    assert back == it
    # a plan without the new fields (an older client / unit replica) reads as plain plan anchors
    old = m.PlanItem(**{"hostname": "u", "sequence_id": "s", "on_air_offset_s": 5.0})
    assert (old.on_air_anchor, old.off_air_anchor, old.id, old.expanded) == ("plan", "plan", "", True)
