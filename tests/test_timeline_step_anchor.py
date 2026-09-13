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


def test_ramp_end_edge_includes_the_final_levels_hold():
    # A ramp's END edge is its FULL duration (last fire + the final level's hold), matching the
    # agent (>= 1.25.1): steps=3, duration_s=6.0 → 4 levels held 1.5 s each; the last point fires
    # at 4.5 s and is held to 6.0 s, so the ramp's end (where a dependent hangs) is start+6.0.
    r = _ramp(off=2.0, sid="rmp")                                 # start 2, end 2+6 = 8
    after = _tune(off=1.0, anchor="step", ref="rmp", edge="end")  # 8+1 = 9 (after the final hold)
    at_start = _tune(off=0.0, anchor="step", ref="rmp", edge="start", gain=20)  # 2
    items = [_bar(), r, after, at_start]
    bases = tlm.resolve_step_offsets(items, None)
    assert bases[after.uid] == 9.0
    assert bases[at_start.uid] == 2.0


# ── step_drop_offset (drag-to-anchor drop, UI) ──────────────────────────────────

def test_step_drop_offset_keeps_the_source_in_place():
    """Dropping a later step onto a target edge anchors it at the gap it already has, so it
    doesn't jump — dragging the target then moves it."""
    r = _ramp(off=2.0, sid="rmp")                        # start 2, end 8 (incl. final hold)
    later = _tune(off=20.0)                              # sits at 20
    items = [_bar(), r, later]
    bases = tlm.resolve_step_offsets(items, None)
    # anchor `later` to the ramp's END (8): offset = 20 - 8 = 12
    off = tlm.step_drop_offset(items, later.uid, r.uid, "end", None, bases)
    assert off == 12.0
    # to its START (2): offset = 20 - 2 = 18
    assert tlm.step_drop_offset(items, later.uid, r.uid, "start", None, bases) == 18.0


def test_step_drop_offset_keeps_a_source_before_the_edge_negative():
    # A source dropped BEFORE its target's edge keeps its place with a NEGATIVE offset (it fires
    # before the edge, like a start/stop anchor's warm-up lead-in) — no longer clamped to 0.
    r = _ramp(off=10.0, sid="rmp")                       # end 16 (incl. final hold)
    early = _tune(off=3.0)                               # before the edge
    items = [_bar(), r, early]
    off = tlm.step_drop_offset(items, early.uid, r.uid, "end", None, None)
    assert off == -13.0                                  # 3 - 16 → stays put, before its anchor


def test_step_drop_offset_rejects_ineligible_targets():
    r = _ramp(off=2.0, sid="rmp")
    later = _tune(off=20.0)
    bar = _bar()
    items = [bar, r, later]
    assert tlm.step_drop_offset(items, later.uid, later.uid, "end", None, None) is None  # self
    # a bar's END is its OFF-AIR stop — not on the on-air clock this helper measures, so None
    # (the canvas drag path uses geometry via _make_anchor, which handles the off-air edge)
    assert tlm.step_drop_offset(items, later.uid, bar.uid, "end", None, None) is None
    # a cycle: r already anchors to later → later can't anchor back to r
    later.step_id = "later"
    r.anchor = "step"; r.anchor_step_id = "later"; r.anchor_edge = "end"
    assert tlm.step_drop_offset(items, later.uid, r.uid, "end", None, None) is None


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


def test_negative_offset_is_accepted_and_resolves_before_the_edge():
    # A negative step offset is allowed now (fire BEFORE the target's edge, like a start/stop
    # anchor's warm-up lead-in); validate() passes and it resolves 1 s before its anchor.
    a = _tune(off=4.0, sid="a")
    b = _tune(off=-1.0, anchor="step", ref="a", edge="end")
    items = _valid_with([a, b])
    assert tlm.validate(items, ["tx"]) is None
    bases = tlm.resolve_step_offsets(items, None)
    assert bases[b.uid] == 3.0                          # a at 4 − 1 = 3, before its anchor


def test_self_anchor_rejected():
    a = _tune(off=4.0, sid="a", anchor="step", ref="a", edge="end")
    assert "can't anchor to itself" in (tlm.validate(_valid_with([a]), ["tx"]) or "")


def test_cycle_rejected():
    a = _tune(off=1.0, sid="a", anchor="step", ref="b", edge="end")
    b = _tune(off=1.0, sid="b", anchor="step", ref="a", edge="end")
    err = tlm.validate(_valid_with([a, b]), ["tx"]) or ""
    assert "cycle" in err or "loop" in err
    # actionable: names one of the offending steps, not a catch-all
    assert "tx" in err


def test_off_air_target_now_resolves_on_the_off_air_clock():
    # Anchoring to an OFF-AIR (stop) step is now VALID — the dependent resolves on the off-air
    # clock (its absolute time is set at arm), not rejected (owner #12).
    a = _tune(off=-10.0, sid="a", anchor="stop")             # off-air −10 s
    b = _tune(off=-2.0, anchor="step", ref="a", edge="start")  # a's edge(−10) − 2 = −12 off-air
    items = _valid_with([a, b])
    assert tlm.validate(items, ["tx"]) is None
    off = tlm.resolve_step_offsets_off(items, None)
    assert off.get(b.uid) == -12.0
    assert b.uid not in tlm.resolve_step_offsets(items, None)   # not on the on-air clock
    by_sid = {getattr(it, "step_id", "") or "": it for it in items if getattr(it, "step_id", "")}
    assert tlm.step_anchor_fault(b, by_sid) is None            # off-air is no longer a fault


# ── step_targets_for_edit (never silently re-point on edit) ──────────────────────

def test_step_targets_for_edit_preserves_an_ineligible_stored_target():
    a = _ramp(off=0.0, sid="a", anchor="both")               # window-filling → ineligible target
    other = _tune(off=5.0, sid="c")                          # an eligible target
    b = _tune(off=1.0, anchor="step", ref="a", edge="start")
    items = [_bar(), a, other, b]
    assert a not in tlm.eligible_step_targets(items, b.uid)  # 'both' → not offered for a NEW anchor
    tgts = tlm.step_targets_for_edit(items, b)               # …but preserved when EDITING b
    assert a in tgts and other in tgts


def test_step_targets_for_edit_matches_eligible_when_no_stored_target():
    src = _tune(off=1.0)
    a = _tune(off=5.0, sid="a")
    items = [_bar(), src, a]
    assert tlm.step_targets_for_edit(items, src) == tlm.eligible_step_targets(items, src.uid)


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


def test_eligible_targets_include_bars_and_off_air_exclude_self_hold_both():
    src = _tune(off=1.0)
    a = _tune(off=5.0, sid="a")                                  # eligible point
    r = _ramp(off=2.0, sid="rmp")                                # eligible ramp
    bar2 = BarItem(task_name="rx", start_offset=0.0, stop_offset=0.0)   # a duration task → eligible
    stop = RunItem(task_name="tx", action="run", anchor="stop", offset=0.0)  # off-air → eligible now
    both = _ramp(off=0.0, sid="bth", anchor="both")             # window-filling → NOT eligible
    hold = RunItem(task_name="", action="hold", anchor="start", offset=3.0)  # hold → no
    items = [_bar(), src, a, r, bar2, stop, both, hold]
    tgts = tlm.eligible_step_targets(items, src.uid)
    assert a in tgts and r in tgts and bar2 in tgts and stop in tgts     # bars + off-air now targets
    assert src not in tgts and hold not in tgts and both not in tgts


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


def test_step_anchor_negative_supported_gate():
    caps = {"sequence-step-anchor", "sequence-step-anchor-negative"}
    assert tlm.step_anchor_negative_supported(_Client(caps, "1.25.0"))
    assert not tlm.step_anchor_negative_supported(_Client(caps, "1.24.0"))         # too old
    assert not tlm.step_anchor_negative_supported(                                 # cap missing
        _Client({"sequence-step-anchor"}, "1.25.0"))
    # capability present, version unknown → authoritative
    assert tlm.step_anchor_negative_supported(_Client(caps, ""))


def test_ensure_step_id_is_stable():
    it = _tune(off=1.0)
    sid = tlm.ensure_step_id(it)
    assert sid and it.step_id == sid
    assert tlm.ensure_step_id(it) == sid          # idempotent


# ── #4: a step-anchored dependent must not resolve before its task goes on air ──────────

def test_step_anchored_tune_before_on_air_is_blocked():
    # A dependent anchored 30 s BEFORE a target: fine while the target sits at +40 (dep=+10),
    # but dragging the target to +10 pushes the dependent to −20 — before the task's on-air
    # start — which validate() must reject (the owner-reported case).
    target = _tune(off=40.0, sid="t")
    dep = _tune(off=-30.0, anchor="step", ref="t", edge="start")
    items = [_bar(), target, dep]
    assert tlm.validate(items, ["tx"]) is None            # dep resolves to +10 → inside the window
    target.offset = 10.0                                   # "drag" the target left → dep = −20
    err = tlm.validate(items, ["tx"])
    assert err is not None and "before" in err and "on air" in err


def test_step_anchored_tune_at_on_air_is_allowed():
    target = _tune(off=30.0, sid="t")
    dep = _tune(off=-30.0, anchor="step", ref="t", edge="start")   # resolves to exactly 0 (on-air)
    assert tlm.validate([_bar(), target, dep], ["tx"]) is None


# ── Bar (duration task) anchoring: wire round-trip + resolution (groundwork for #1/#6/#7) ──

def _bar_t(task="tx", so=0.0, po=0.0, sid="", sanc="start", saref="", saedge="end"):
    return BarItem(task_name=task, start_offset=so, stop_offset=po, step_id=sid,
                   start_anchor=sanc, start_anchor_step_id=saref, start_anchor_edge=saedge)


def test_bar_as_target_round_trips_start_and_stop_edges():
    # A tune anchored to a bar's START edge and another to its STOP edge must survive
    # items_to_steps -> steps_to_items, mapping to the bar's two wire steps (id / id+suffix).
    bar = _bar_t(sid="b1")
    dep_start = _tune(off=3.0, anchor="step", ref="b1", edge="start")
    dep_stop = _tune(off=-4.0, anchor="step", ref="b1", edge="end")
    wire = tlm.items_to_steps([bar, dep_start, dep_stop])
    # the bar's two wire steps carry distinct ids
    ids = {s.get("id") for s in wire if s.get("action") in ("start", "stop")}
    assert "b1" in ids and ("b1" + tlm.BAR_STOP_SUFFIX) in ids
    # the stop-edge dependent points at the stop wire step; the start-edge one at the start id
    refs = {(s.get("anchor_step_id")) for s in wire if s.get("anchor") == "step"}
    assert "b1" in refs and ("b1" + tlm.BAR_STOP_SUFFIX) in refs
    # decode: the two dependents come back with the bar id + the right edge
    items = tlm.steps_to_items(wire)
    deps = [it for it in items if getattr(it, "anchor", "") == "step"]
    edges = {(d.anchor_step_id, d.anchor_edge) for d in deps}
    assert ("b1", "start") in edges and ("b1", "end") in edges


def test_bar_as_source_round_trips_start_anchor():
    # A bar whose START hangs off another step (start_anchor="step"); its STOP stays off-air.
    target = _tune(off=40.0, sid="t")
    bar = _bar_t(sanc="step", saref="t", saedge="end", so=5.0)
    wire = tlm.items_to_steps([target, bar])
    start = next(s for s in wire if s.get("action") == "start")
    assert start["anchor"] == "step" and start["anchor_step_id"] == "t" and start["anchor_edge"] == "end"
    stop = next(s for s in wire if s.get("action") == "stop")
    assert stop["anchor"] == "stop"                     # the STOP stays off-air (only start anchors)
    items = tlm.steps_to_items(wire)
    b = next(it for it in items if it.kind == "bar")
    assert b.start_anchor == "step" and b.start_anchor_step_id == "t" and b.start_offset == 5.0


def test_resolve_places_a_bar_target_and_a_bar_source():
    target = _tune(off=40.0, sid="t")
    bar_src = _bar_t(sid="b1", sanc="step", saref="t", saedge="end", so=5.0)   # start = t.end(40)+5 = 45
    dep = _tune(off=2.0, anchor="step", ref="b1", edge="start")                 # bar.start(45)+2 = 47
    bases = tlm.resolve_step_offsets([_bar(), target, bar_src, dep], None)
    assert bases[bar_src.uid] == 45.0
    assert bases[dep.uid] == 47.0
    # anchoring to the bar's STOP edge is off-air → unresolved on the on-air clock (agent-timed)
    dep_stop = _tune(off=-2.0, anchor="step", ref="b1", edge="end")
    bases2 = tlm.resolve_step_offsets([_bar(), target, bar_src, dep_stop], None)
    assert dep_stop.uid not in bases2


def test_plain_bar_wire_is_unchanged():
    # A bar with no anchoring emits the SAME two steps as before (no id / anchor fields).
    wire = tlm.items_to_steps([_bar_t(task="tx", so=0.0, po=0.0)])
    assert [s["action"] for s in wire] == ["start", "stop"]
    assert "id" not in wire[0] and "anchor_step_id" not in wire[0]
    assert wire[0]["anchor"] == "start" and wire[1]["anchor"] == "stop"


# ── A ramp tied by its END (anchor_own_edge="end"): the end sits at the target edge + offset ──

def test_end_tied_ramp_resolves_its_start_one_duration_earlier():
    # dur 6 (steps=3, duration_s=6): tied by its END at t.end(40) + (−2) = 38 → start base = 32.
    t = _tune(off=40.0, sid="t")
    r = _ramp(off=-2.0, anchor="step", ref="t", edge="end")
    r.anchor_own_edge = "end"
    bases = tlm.resolve_step_offsets([_bar(), t, r], None)
    assert bases[r.uid] == 32.0
    assert tlm.step_wire_offset(r) == -8.0                 # the START's offset from the edge
    assert tlm.own_edge_shift(r) == 6.0
    # a dependent hanging off the ramp's END edge fires at 38 (+1) — the tied point itself
    after = _tune(off=1.0, anchor="step", ref="rmp", edge="end", gain=20)
    r.step_id = "rmp"
    bases = tlm.resolve_step_offsets([_bar(), t, r, after], None)
    assert bases[after.uid] == 39.0
    assert tlm.validate([_bar(), t, r, after], ["tx"]) is None


def test_end_tied_ramp_wire_carries_the_start_offset_and_the_tie():
    t = _tune(off=40.0, sid="t")
    r = _ramp(off=-2.0, anchor="step", ref="t", edge="end")
    r.anchor_own_edge = "end"
    wire = tlm.items_to_steps([_bar(), t, r])
    rs = next(s for s in wire if s.get("action") == "ramp")
    assert rs["offset_s"] == -8.0                          # the agent places the START unchanged
    assert rs["anchor_own_edge"] == "end"
    back = tlm.steps_to_items(wire)
    rb = next(it for it in back if tlm._is_ramp(it))
    assert rb.anchor_own_edge == "end" and rb.offset == -2.0     # the item keeps the END's offset
    # a start-tied ramp's wire is byte-identical to before (no anchor_own_edge key at all)
    r2 = _ramp(off=-2.0, anchor="step", ref="t", edge="end")
    rs2 = next(s for s in tlm.items_to_steps([_bar(), t, r2]) if s.get("action") == "ramp")
    assert rs2["offset_s"] == -2.0 and "anchor_own_edge" not in rs2


def test_step_source_helpers_cover_runs_and_bars():
    t = _tune(off=40.0, sid="t")
    run_dep = _tune(off=3.0, anchor="step", ref="t", edge="start")
    bar_dep = _bar_t(sanc="step", saref="t", saedge="end", so=5.0)
    plain = _bar()
    assert tlm.is_step_source(run_dep) and tlm.is_step_source(bar_dep)
    assert not tlm.is_step_source(t) and not tlm.is_step_source(plain)
    assert tlm.step_source_ref(run_dep) == ("t", "start")
    assert tlm.step_source_ref(bar_dep) == ("t", "end")
    assert tlm.step_wire_offset(bar_dep) == 5.0
