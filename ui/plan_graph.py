"""
Plan graph — the timing model of the plan editor redesign.

A plan is a set of sequences (PlanItems) on several units. Each item's two edges (its ON-AIR and
its OFF-AIR) hang off SOMETHING — the plan's own anchors, another item's edge, or a step's edge
inside another item — and each step hangs off its own sequence's edges, another step in its
sequence, or (plan-level anchoring) a step / window edge in ANOTHER item. "Anything anchors to
anything" (owner ask #6).

Everything resolves to a (clock, seconds) pair on the PLAN's two clocks:

    ("on",  t)   t seconds after the plan's ON-AIR (T0)       — known at authoring
    ("off", t)   t seconds after the plan's OFF-AIR (T_end)   — fixed when the plan is armed

A chain inherits its root's clock. A cycle, a missing target or an un-addressable step resolves
to None (the editor refuses to save/arm such a plan; `describe_fault` says what's wrong).

The graph works on the WIRE steps of each item (`PlanItem.steps`, SequenceSteps or the dicts
`timeline_model.items_to_steps` emits) — every reference on the wire names a single step's
start/end (a duration task is two wire steps, `X` and `X~stop`; a ramp's end = start + duration).
The plan editor maps its canvas items onto these through their step ids.

`compile_plan` is the arm-time half: given absolute T0 (+ T_end) it turns every item's edges into
absolute instants and REWRITES every cross-item step anchor into a plain on-air offset of its own
sequence — so an agent (which only ever sees one sequence) receives a self-contained step list.
Pure; no Qt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from api import models as m
from . import timeline_model as tlm

Clocked = Tuple[str, float]      # ("on" | "off", seconds from that plan anchor)

ITEM_ID_PREFIX = "pi-"
EXTENT_ROUND_S = 60.0            # the defined regions are rounded up to whole minutes…
EXTENT_PAD_S = 30.0              # …after this much room past the furthest point


class PlanResolveError(ValueError):
    """A plan whose timing can't be resolved (a loop, a missing target, or an on-air that hangs
    off the plan's off-air in an open-ended arm)."""


def new_item_id() -> str:
    import uuid
    return ITEM_ID_PREFIX + uuid.uuid4().hex[:8]


def ensure_item_ids(items: Iterable) -> None:
    """Give every item without an id one (in place). The editor calls this on load so any item
    can be referenced; the ids persist on save."""
    for it in items:
        if not (getattr(it, "id", "") or ""):
            it.id = new_item_id()


def _get(s, key: str, default=None):
    if isinstance(s, dict):
        return s.get(key, default)
    return getattr(s, key, default)


def _action(s) -> str:
    a = _get(s, "action")
    return a.value if hasattr(a, "value") else str(a or "")


def _ramp_dur(s) -> float:
    if _action(s) != "ramp":
        return 0.0
    r = _get(s, "ramp") or {}
    if hasattr(r, "model_dump"):
        r = r.model_dump()
    return tlm._ramp_duration(dict(r))


@dataclass
class ItemTiming:
    on: Optional[Clocked]
    off: Optional[Clocked]


class PlanGraph:
    """Resolves one plan's items + wire steps. Build once per plan state; results are memoised."""

    def __init__(self, items: Iterable):
        self.items: List = list(items)
        self.by_id: Dict[str, object] = {}
        for idx, it in enumerate(self.items):
            iid = (getattr(it, "id", "") or "") or f"#{idx}"
            self.by_id[iid] = it
        self._id_of = {id(it): iid for iid, it in self.by_id.items()}
        # (item id) → wire steps; (item id, step id) → index
        self._steps: Dict[str, list] = {}
        self._index: Dict[Tuple[str, str], int] = {}
        for iid, it in self.by_id.items():
            steps = list(getattr(it, "steps", None) or [])
            self._steps[iid] = steps
            for i, s in enumerate(steps):
                sid = _get(s, "id", "") or ""
                if sid:
                    self._index[(iid, sid)] = i
        self._memo: Dict[tuple, Optional[Clocked]] = {}
        self._stack: set = set()

    # ── addressing ────────────────────────────────────────────────────────────
    def item_id(self, item) -> str:
        return self._id_of.get(id(item), getattr(item, "id", "") or "")

    def steps_of(self, item_id: str) -> list:
        return self._steps.get(item_id, [])

    def hold_offset(self, item_id: str) -> float:
        for s in self.steps_of(item_id):
            if _action(s) == "hold":
                return float(_get(s, "offset_s", 0.0) or 0.0)
        return 0.0

    # ── resolution ────────────────────────────────────────────────────────────
    def _guarded(self, key: tuple, fn) -> Optional[Clocked]:
        if key in self._memo:
            return self._memo[key]
        if key in self._stack:
            return None                                   # a loop
        self._stack.add(key)
        try:
            res = fn()
        finally:
            self._stack.discard(key)
        self._memo[key] = res
        return res

    def item_edge(self, item_id: str, edge: str) -> Optional[Clocked]:
        """(clock, t) of an item's ON-AIR (edge "on") or OFF-AIR (edge "off")."""
        it = self.by_id.get(item_id)
        if it is None:
            return None

        def calc() -> Optional[Clocked]:
            pre = "on_air" if edge == "on" else "off_air"
            anchor = getattr(it, f"{pre}_anchor", "plan") or "plan"
            off = float(getattr(it, f"{pre}_offset_s", 0.0) or 0.0)
            a_edge = getattr(it, f"{pre}_anchor_edge", "on" if edge == "on" else "off") or \
                ("on" if edge == "on" else "off")
            if anchor == "plan":
                clock = "off" if a_edge == "off" else "on"
                return (clock, off)
            tgt = (getattr(it, f"{pre}_anchor_item", "") or "") or item_id   # "" = self
            if anchor == "item":
                base = self.item_edge(tgt, "off" if a_edge == "off" else "on")
            elif anchor == "step":
                sid = getattr(it, f"{pre}_anchor_step", "") or ""
                base = self.step_edge_by_id(tgt, sid, "end" if a_edge == "end" else "start")
            else:
                return None
            return None if base is None else (base[0], base[1] + off)
        return self._guarded(("item", item_id, edge), calc)

    def step_edge_by_id(self, item_id: str, step_id: str, edge: str) -> Optional[Clocked]:
        """A step's start/end edge, addressed by its wire id. A blank step id addresses the ITEM's
        own window edge ("start" = its on-air, "end" = its off-air)."""
        if not step_id:
            return self.item_edge(item_id, "on" if edge == "start" else "off")
        idx = self._index.get((item_id, step_id))
        if idx is None:
            return None
        return self.step_edge(item_id, idx, edge)

    def step_edge(self, item_id: str, idx: int, edge: str) -> Optional[Clocked]:
        """(clock, t) of the start ("start") or end ("end") edge of the idx-th wire step of an
        item. A ramp's end is its full duration after its start; every other step is a point."""
        steps = self.steps_of(item_id)
        if not (0 <= idx < len(steps)):
            return None
        s = steps[idx]

        def calc() -> Optional[Clocked]:
            anchor = str(_get(s, "anchor", "start") or "start")
            off = float(_get(s, "offset_s", 0.0) or 0.0)
            if edge == "end" and anchor == "both":
                base = self.item_edge(item_id, "off")
                end_off = _get(s, "offset_end_s", 0.0)
                return None if base is None else (base[0], base[1] + float(end_off or 0.0))
            if anchor in ("start", "both"):
                base = self.item_edge(item_id, "on")
            elif anchor in ("hold", "enter"):
                base = self.item_edge(item_id, "on")
                if base is not None:
                    base = (base[0], base[1] + self.hold_offset(item_id))
            elif anchor == "stop":
                base = self.item_edge(item_id, "off")
            elif anchor == "step":
                a_item = str(_get(s, "anchor_item", "") or "") or item_id
                a_sid = str(_get(s, "anchor_step_id", "") or "")
                a_edge = str(_get(s, "anchor_edge", "end") or "end")
                base = self.step_edge_by_id(a_item, a_sid, a_edge)
            else:
                return None
            if base is None:
                return None
            start = (base[0], base[1] + off)
            if edge == "end":
                return (start[0], start[1] + _ramp_dur(s))
            return start
        return self._guarded(("step", item_id, idx, edge), calc)

    def item_timing(self, item_id: str) -> ItemTiming:
        return ItemTiming(self.item_edge(item_id, "on"), self.item_edge(item_id, "off"))

    # ── whole-plan views ───────────────────────────────────────────────────────
    def all_times(self) -> List[Clocked]:
        out: List[Clocked] = []
        for iid in self.by_id:
            for e in ("on", "off"):
                c = self.item_edge(iid, e)
                if c is not None:
                    out.append(c)
            for i in range(len(self.steps_of(iid))):
                for e in ("start", "end"):
                    c = self.step_edge(iid, i, e)
                    if c is not None:
                        out.append(c)
        return out

    def extents(self) -> Tuple[float, float]:
        """(forward_s, backward_s): how far the DEFINED regions reach — past the plan's on-air
        (everything on the on clock) and before its off-air (everything on the off clock) — rounded
        up to whole minutes with a little room, like the mockup's axis."""
        times = self.all_times()
        fwd = max([0.0] + [t for c, t in times if c == "on"])
        bwd = max([0.0] + [-t for c, t in times if c == "off"])

        def rnd(v: float) -> float:
            import math
            return math.ceil((v + EXTENT_PAD_S) / EXTENT_ROUND_S) * EXTENT_ROUND_S
        return rnd(fwd), rnd(bwd)

    def earliest_on_clock_s(self, item_id: str) -> Optional[float]:
        """The earliest on-clock instant anything of this item fires (its edges + its steps) — the
        plan's warm-up lead-in for that item; None if nothing of it is on the on clock."""
        vals = []
        for e in ("on", "off"):
            c = self.item_edge(item_id, e)
            if c is not None and c[0] == "on":
                vals.append(c[1])
        for i in range(len(self.steps_of(item_id))):
            c = self.step_edge(item_id, i, "start")
            if c is not None and c[0] == "on":
                vals.append(c[1])
        return min(vals) if vals else None

    def describe_fault(self, item_id: str) -> Optional[str]:
        """Why an item can't be timed (None when it can)."""
        it = self.by_id.get(item_id)
        if it is None:
            return "unknown plan item"
        name = getattr(it, "sequence_name", "") or item_id
        for edge, pre in (("on", "on_air"), ("off", "off_air")):
            if self.item_edge(item_id, edge) is None:
                anchor = getattr(it, f"{pre}_anchor", "plan") or "plan"
                tgt = getattr(it, f"{pre}_anchor_item", "") or ""
                if anchor in ("item", "step") and tgt and tgt not in self.by_id:
                    return (f"“{name}”'s {edge}-air anchors to a sequence that is no longer in "
                            f"the plan — re-anchor it")
                return (f"“{name}”'s {edge}-air can't be timed — its anchor chain loops back on "
                        f"itself or points at a missing step; re-anchor it")
        for i, s in enumerate(self.steps_of(item_id)):
            if self.step_edge(item_id, i, "start") is None:
                return (f"a step on “{name}” ({_get(s, 'task_name', '') or 'step'}) anchors to "
                        f"something that can't be timed — re-anchor it")
        return None

    def first_fault(self) -> Optional[str]:
        for iid in self.by_id:
            msg = self.describe_fault(iid)
            if msg:
                return msg
        return None


# ── layout helpers (pure) ─────────────────────────────────────────────────────

def pack_lanes(spans: List[Tuple[str, float, float]], gap: float = 6.0) -> Dict[str, int]:
    """Greedy sub-lane assignment for folded units: {id: lane}. Spans are (id, x0, x1) in any
    common unit; two spans share a lane iff they don't overlap (keeping `gap` between them)."""
    lanes: List[List[Tuple[float, float]]] = []
    out: Dict[str, int] = {}
    for sid, x0, x1 in sorted(spans, key=lambda s: (s[1], s[2])):
        for li, lane in enumerate(lanes):
            if all(b + gap <= x0 or x1 + gap <= a for a, b in lane):
                lane.append((x0, x1)); out[sid] = li
                break
        else:
            lanes.append([(x0, x1)]); out[sid] = len(lanes) - 1
    return out


def channel_conflicts(spans: Dict[str, Tuple[float, float]], unit_of: Dict[str, str]) -> set:
    """Ids of the sequences that OVERLAP another sequence on the SAME unit (one TX channel each):
    a channel conflict. Spans are the sequences' drawn extents (their window plus every step's
    warm-up / cool-down), in any common unit."""
    out: set = set()
    ids = list(spans)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if unit_of.get(a) != unit_of.get(b):
                continue
            a0, a1 = spans[a]; b0, b1 = spans[b]
            if a0 < b1 and b0 < a1:
                out.add(a); out.add(b)
    return out


# ── arm-time compile ──────────────────────────────────────────────────────────

@dataclass
class CompiledItem:
    on_air_at: datetime
    off_air_at: Optional[datetime]           # None for an open-ended arm whose off-air floats
    steps: Optional[List[m.SequenceStep]]    # None = the item carries no plan-local steps


def _abs(t0: datetime, t_end: Optional[datetime], c: Clocked) -> Optional[datetime]:
    clock, t = c
    if clock == "on":
        return t0 + timedelta(seconds=t)
    if t_end is None:
        return None
    return t_end + timedelta(seconds=t)


def compile_plan(items: Iterable, t0: datetime,
                 t_end: Optional[datetime]) -> List[CompiledItem]:
    """Absolute timing for every item (in the items' order), and its steps with every CROSS-ITEM
    anchor rewritten to a plain on-air offset of its own sequence (the agent only ever sees one
    sequence). Same-sequence step anchors are left for the agent to resolve. Raises
    PlanResolveError when an item can't be timed, or its on-air hangs off the plan's off-air in an
    open-ended arm (`t_end` None)."""
    g = PlanGraph(items)
    out: List[CompiledItem] = []
    for it in g.items:
        iid = g.item_id(it)
        fault = g.describe_fault(iid)
        if fault:
            raise PlanResolveError(fault)
        on_c = g.item_edge(iid, "on"); off_c = g.item_edge(iid, "off")
        name = getattr(it, "sequence_name", "") or iid
        on_at = _abs(t0, t_end, on_c)
        if on_at is None:
            raise PlanResolveError(
                f"“{name}”'s on-air is timed from the plan's OFF-AIR, which an open-ended arm "
                f"doesn't have — arm with a stop time, or anchor it to the plan's on-air")
        off_at = _abs(t0, t_end, off_c)
        steps_in = list(getattr(it, "steps", None) or [])
        if not steps_in:
            out.append(CompiledItem(on_at, off_at, None))
            continue
        steps_out: List[m.SequenceStep] = []
        for idx, s in enumerate(steps_in):
            if str(_get(s, "anchor", "start") or "start") == "step" and (_get(s, "anchor_item", "") or ""):
                st = g.step_edge(iid, idx, "start")
                if st is None:
                    raise PlanResolveError(g.describe_fault(iid) or f"“{name}” can't be timed")
                st_abs = _abs(t0, t_end, st)
                if st_abs is None:
                    raise PlanResolveError(
                        f"a step on “{name}” is timed from the plan's OFF-AIR, which an open-ended "
                        f"arm doesn't have — arm with a stop time")
                rel = (st_abs - on_at).total_seconds()
                s2 = s if isinstance(s, m.SequenceStep) else m.SequenceStep(**dict(s))
                steps_out.append(s2.model_copy(update={
                    "anchor": "start", "offset_s": round(rel, 3), "anchor_step_id": "",
                    "anchor_edge": "end", "anchor_own_edge": "start", "anchor_item": ""}))
            else:
                steps_out.append(s if isinstance(s, m.SequenceStep) else m.SequenceStep(**dict(s)))
        out.append(CompiledItem(on_at, off_at, steps_out))
    return out


def plan_uses_anchors(items: Iterable) -> bool:
    """True when any item is anchored to something other than the plan's own anchors, or any step
    is anchored into another item — i.e. the plan needs the graph (an older client / agent would
    read its offsets as plain plan offsets)."""
    for it in items:
        if (getattr(it, "on_air_anchor", "plan") or "plan") != "plan":
            return True
        if (getattr(it, "off_air_anchor", "plan") or "plan") != "plan":
            return True
        for s in (getattr(it, "steps", None) or []):
            if _get(s, "anchor_item", "") or "":
                return True
    return False
