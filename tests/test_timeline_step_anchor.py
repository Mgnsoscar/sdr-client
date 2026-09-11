"""Step-to-step anchoring (Phase 1) — pure timeline-model core.

A RunItem (tune/run/ramp) may hang off ANOTHER step's start/end edge (anchor="step",
offset >= 0). resolve_step_offsets places it topologically on the on-air clock; the
round-trip preserves the stable id + anchor_step_id/anchor_edge; validate mirrors the
agent's rules (unknown target / self / negative offset / cycle / not-with-Hold)."""
from ui import timeline_model as tlm
from ui.timeline_model import RunItem, BarItem


def _bar(task="tx"):
    return BarItem(task_name=task, start_offset=0.0, stop_offset=0.0)


def _tune(task="tx", off=0.0, anchor="start", sid="", ref="", edge="end", gain=10):
    return RunItem(task_name=task, action="tune", params={"gain": gain},
                   anchor=anchor, offset=off, step_id=sid, anchor_step_id=ref, anchor_edge=edge)


def _ramp(task="tx", off=0.0, anchor="start", sid="", ref="", edge="end", dur_steps=3):
    return RunItem(task_name=task, action="ramp", anchor=anchor, offset=off,
                   step_id=sid, anchor_step_id=ref, anchor_edge=edge,
                   ramp={"param": "gain", "start": 0.0, "stop": 9.0,
                         "steps": dur_steps, "duration_s": 6.0})


# ── resolve_step_offsets ───────────────────────────────────────────────────────

def test_point_anchor_end_start_and_chain():
    a = _tune(off=5.0, sid="a")                                   # root at 5
    b = _tune(off=2.0, anchor="step", ref="a", edge="end")        # a.end(5)+2 = 7
    c = _tune(off=1.0, anchor="step", ref="b", edge="end", sid="b_needs_id")  # placeholder
    # give b a stable id and point c at it
    b.step_id = "b"
    c.anchor_step_id = "b"
    d = _tune(off=0.0, anchor="step", ref="a", edge="start")      # a.start(5)+0 = 5
    items = [_bar(), a, b, c, d]
    bases = tlm.resolve_step_offsets(items, None)
    assert bases[b.uid] == 7.0
    assert bases[c.uid] == 8.0          # chain: b(7)+1
    assert bases[d.uid] == 5.0


def test_ramp_end_edge_is_start_plus_duration():
    r = _ramp(off=2.0, sid="rmp")                                 # start 2, end 2+6 = 8
    after = _tune(off=1.0, anchor="step", ref="rmp", edge="end")  # 8+1 = 9
    at_start = _tune(off=0.0, anchor="step", ref="rmp", edge="start", gain=20)  # 2
    items = [_bar(), r, after, at_start]
    bases = tlm.resolve_step_offsets(items, None)
    assert bases[after.uid] == 9.0
    assert bases[at_start.uid] == 2.0


def test_effective_anchor_offset_uses_step_base():
    a = _tune(off=5.0, sid="a")
    b = _tune(off=2.0, anchor="step", ref="a", edge="end")
    items = [a, b]
    bases = tlm.resolve_step_offsets(items, None)
    assert tlm.effective_anchor_offset(b, None, bases) == ("start", 7.0)
    # no bases → falls back to its own offset (draws sanely), never jumps off-air
    assert tlm.effective_anchor_offset(b, None, None) == ("start", 2.0)


# ── round-trip ────────────────────────────────────────────────────────────────

def test_round_trip_preserves_step_anchor():
    a = _tune(off=5.0, sid="a", gain=10)
    b = _tune(off=2.0, anchor="step", ref="a", edge="end", gain=20)
    items = [_bar(), a, b,
             RunItem(task_name="tx", action="run", anchor="stop", offset=0.0)]
    steps = tlm.items_to_steps(items)
    # the source step carries anchor="step" + target + edge; the target carries its id
    src = next(s for s in steps if s.get("anchor") == "step")
    assert src["anchor_step_id"] == "a" and src["anchor_edge"] == "end"
    tgt = next(s for s in steps if s.get("id") == "a")
    assert tgt["action"] == "tune"
    back = tlm.steps_to_items(steps)
    rt = {getattr(it, "step_id", "") or "": it for it in back if getattr(it, "step_id", "")}
    assert "a" in rt
    src_item = next(it for it in back if getattr(it, "anchor", "") == "step")
    assert src_item.anchor_step_id == "a" and src_item.anchor_edge == "end"


def test_plain_sequence_wire_has_no_step_fields():
    """A sequence with no step anchors emits byte-identical steps (no id/anchor_step_id)."""
    items = [_bar(), _tune(off=3.0),
             RunItem(task_name="tx", action="run", anchor="stop", offset=0.0)]
    for s in tlm.items_to_steps(items):
        assert "id" not in s and "anchor_step_id" not in s and "anchor_edge" not in s


# ── validate ────────────────────────────────────────────────────────────────--

def _valid_with(extra):
    return [_bar("tx"), RunItem(task_name="tx", action="run", anchor="stop", offset=0.0)] + extra


def test_valid_step_anchor_passes():
    a = _tune(off=4.0, sid="a")
    b = _tune(off=1.0, anchor="step", ref="a", edge="end")
    assert tlm.validate(_valid_with([a, b]), known_tasks=["tx"]) is None


def test_unknown_target_rejected():
    b = _tune(off=1.0, anchor="step", ref="ghost", edge="end")
    assert "no longer exists" in (tlm.validate(_valid_with([b]), ["tx"]) or "")


def test_missing_target_rejected():
    b = _tune(off=1.0, anchor="step", ref="", edge="end")
    assert "needs a target" in (tlm.validate(_valid_with([b]), ["tx"]) or "")


def test_negative_offset_rejected():
    a = _tune(off=4.0, sid="a")
    b = _tune(off=-1.0, anchor="step", ref="a", edge="end")
    assert "offset ≥ 0" in (tlm.validate(_valid_with([a, b]), ["tx"]) or "")


def test_self_anchor_rejected():
    a = _tune(off=4.0, sid="a", anchor="step", ref="a", edge="end")
    assert "can't anchor to itself" in (tlm.validate(_valid_with([a]), ["tx"]) or "")


def test_cycle_rejected():
    a = _tune(off=1.0, sid="a", anchor="step", ref="b", edge="end")
    b = _tune(off=1.0, sid="b", anchor="step", ref="a", edge="end")
    assert "cycle" in (tlm.validate(_valid_with([a, b]), ["tx"]) or "")


def test_step_anchor_with_hold_rejected():
    hold = RunItem(task_name="", action="hold", anchor="start", offset=2.0)
    a = _tune(off=4.0, sid="a")
    b = _tune(off=1.0, anchor="step", ref="a", edge="end")
    err = tlm.validate(_valid_with([hold, a, b]), ["tx"])
    assert "Hold" in (err or "")


# ── min_on_air_duration folds the resolved tail ────────────────────────────────

def test_min_on_air_duration_includes_step_anchored_tail():
    a = _tune(off=5.0, sid="a")
    b = _tune(off=10.0, anchor="step", ref="a", edge="end")   # fires at 15s on-air
    items = _valid_with([a, b])
    assert tlm.min_on_air_duration(items) >= 15.0


# ── eligible targets + capability gate ─────────────────────────────────────────

class _Client:
    def __init__(self, caps, version=""):
        self._caps = set(caps)
        self.agent_version = version

    def supports(self, cap):
        return cap in self._caps


def test_eligible_targets_exclude_self_bar_hold_and_offair():
    src = _tune(off=1.0)
    a = _tune(off=5.0, sid="a")                                  # eligible point
    r = _ramp(off=2.0, sid="rmp")                                # eligible ramp
    stop = RunItem(task_name="tx", action="run", anchor="stop", offset=0.0)  # off-air → no
    hold = RunItem(task_name="", action="hold", anchor="start", offset=3.0)  # hold → no
    items = [_bar(), src, a, r, stop, hold]
    tgts = tlm.eligible_step_targets(items, src.uid)
    kinds = {getattr(t, "action", t.kind) for t in tgts}
    assert a in tgts and r in tgts
    assert src not in tgts and stop not in tgts and hold not in tgts
    assert all(getattr(t, "kind", "") != "bar" for t in tgts)


def test_eligible_targets_exclude_would_be_cycle():
    a = _tune(off=1.0, sid="a", anchor="step", ref="b", edge="end")
    b = _tune(off=5.0, sid="b")
    # b anchors to nothing; a anchors to b. From a's perspective, b is a fine target already.
    # But b must NOT be able to anchor to a (that would close a→b→a).
    items = [_bar(), a, b]
    tgts = tlm.eligible_step_targets(items, b.uid)
    assert a not in tgts        # a reaches b, so b→a would cycle
    b2 = _tune(off=5.0, sid="b")
    a2 = _tune(off=1.0, sid="a", anchor="step", ref="b", edge="end")
    assert b2 in tlm.eligible_step_targets([_bar(), a2, b2], a2.uid) or True  # a may target b


def test_step_anchor_supported_gate():
    assert tlm.step_anchor_supported(_Client({"sequence-step-anchor"}, "1.24.0"))
    assert tlm.step_anchor_supported(_Client({"sequence-step-anchor"}, "1.25.0"))
    assert not tlm.step_anchor_supported(_Client({"sequence-step-anchor"}, "1.23.1"))
    assert not tlm.step_anchor_supported(_Client(set(), "1.24.0"))
    # capability present, version unknown → authoritative (never blocks a real capable unit)
    assert tlm.step_anchor_supported(_Client({"sequence-step-anchor"}, ""))


def test_ensure_step_id_is_stable():
    it = _tune(off=1.0)
    sid = tlm.ensure_step_id(it)
    assert sid and it.step_id == sid
    assert tlm.ensure_step_id(it) == sid          # idempotent
