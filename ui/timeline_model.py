"""
Pure (no-Qt) model + geometry for the sequence timeline.

Two kinds of timeline objects:
  - BarItem  — a long-running (duration) task: one task with a START end anchored
               to ON-AIR and a STOP end anchored to OFF-AIR. Compiles to two
               sequence steps (start + stop) for the same task; added/removed as a
               pair.
  - RunItem  — a fire-and-exit one-shot: a single point (action="run"), no end.

Everything the Qt widget needs that can be reasoned about without a screen lives
here so it can be unit-tested: coordinate mapping, the not-to-scale on-air gap,
drag resolution + constraints, and conversion to/from the agent's flat step list.

Coordinate model (all x in pixels):
    … warm-up (to scale) … │ON-AIR      · on air ·      OFF-AIR│ … cool-down …
  - warm-up / cool-down zones are SCALE px per second.
  - the on-air window between the anchors is a FIXED width (MIDDLE_GAP) because a
    sequence doesn't know the real window length — it's chosen at arm time.
  - a bar's start end lives on the on-air side (anchor="start"); its stop end on
    the off-air side (anchor="stop"); handles never cross the gap midpoint.
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── Geometry constants ───────────────────────────────────────────────────────
SCALE = 3.0            # px per second in the warm-up / cool-down zones
MIDDLE_GAP = 220       # base px between ON-AIR and OFF-AIR (the on-air band)
BAND_PAD = 130         # min px gap between the on-air and off-air groups in the band
EDGE_PAD = 70          # px of empty space beyond the furthest item on each side
HEADROOM_S = 30.0      # seconds of extra drag room kept beyond the furthest item
MIN_SIDE_S = 60.0      # each side is at least this many seconds wide
SNAP_S = 1.0           # drag snaps offsets to this granularity (seconds)

_ids = itertools.count(1)


# ── Items ────────────────────────────────────────────────────────────────────

@dataclass
class BarItem:
    """A duration task: start end (on-air) + stop end (off-air), one task."""
    task_name: str
    args: List[str] = field(default_factory=list)
    replace_args: bool = True
    start_offset: float = 0.0   # seconds relative to ON-AIR  (anchor="start")
    stop_offset: float = 0.0    # seconds relative to OFF-AIR (anchor="stop")
    uid: int = 0
    kind: str = "bar"

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_ids)


@dataclass
class RunItem:
    """A single-point step. Geometrically one point on the timeline (no end);
    what it does depends on `action`:
      - "run"  — fire-and-exit one-shot: launch the task, it self-terminates.
      - "tune" — retune a *running* duration task's live parameters (params below)
                 at this offset; carries `params`, not `args`.
      - "ramp" — sweep one live parameter over time; carries `ramp` (the spec).
                 anchor may also be "both" (fills the on-air window).
    Sharing one item type keeps the canvas geometry (drag/lanes/hit) identical."""
    task_name: str
    args: List[str] = field(default_factory=list)
    replace_args: bool = True
    anchor: str = "start"       # "start" (on-air) | "stop" (off-air) | "both" (ramp)
    offset: float = 0.0         # on-air-side offset for a "both" ramp
    offset_end: float = 0.0     # off-air-side inset for a "both" ramp (≤ 0)
    action: str = "run"         # "run" | "tune" | "ramp"
    params: Dict[str, object] = field(default_factory=dict)   # tune: {name: value}
    ramp: Optional[Dict[str, object]] = None                  # ramp: the RampSpec dict
    uid: int = 0
    kind: str = "run"

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_ids)


# ── Coordinate mapping ───────────────────────────────────────────────────────

def compute_anchors(items, zoom: float = 1.0) -> Tuple[float, float, int]:
    """Return (on_air_x, off_air_x, canvas_width). Everything is placed to scale
    from its anchor (SCALE px/s). Warm-up (left of ON-AIR) and cool-down (right of
    OFF-AIR) grow to hold their items plus headroom.

    The on-air band in the middle is also to scale, but it WIDENS so every point
    that fires during on-air stays ordered: on-air-anchored points (a positive
    on-air offset — one-shots or a bar's start) fill the band from the left, and
    off-air-anchored points (a negative off-air offset — one-shots or a bar's stop)
    fill it from the right, and the band is kept wide enough (plus BAND_PAD) that
    the two groups never overlap — so the last on-air point is always left of the
    first off-air point.

    `zoom` scales the horizontal (time) axis only: pixels-per-second and the band's
    pixel dimensions are multiplied by it, while EDGE_PAD (the fixed edge margin)
    is not — so zooming spreads items apart without changing the edge inset."""
    eff = SCALE * zoom
    left_s = MIN_SIDE_S
    right_s = MIN_SIDE_S
    max_on = 0.0    # largest positive on-air offset that lands in the band
    max_off = 0.0   # largest |negative off-air offset| that lands in the band
    for it in items:
        if it.kind == "bar":
            if it.start_offset < 0:
                left_s = max(left_s, -it.start_offset)
            elif it.start_offset > 0:
                max_on = max(max_on, it.start_offset)
            if it.stop_offset > 0:
                right_s = max(right_s, it.stop_offset)
            elif it.stop_offset < 0:
                max_off = max(max_off, -it.stop_offset)
        elif it.anchor == "start":
            if it.offset < 0:
                left_s = max(left_s, -it.offset)
            elif it.offset > 0:
                max_on = max(max_on, it.offset)
        else:  # run, off-air anchored
            if it.offset > 0:
                right_s = max(right_s, it.offset)
            elif it.offset < 0:
                max_off = max(max_off, -it.offset)
    left_s += HEADROOM_S
    right_s += HEADROOM_S
    band_gap = max(MIDDLE_GAP * zoom, (max_on + max_off) * eff + BAND_PAD * zoom)
    on_air_x = EDGE_PAD + left_s * eff
    off_air_x = on_air_x + band_gap
    width = int(off_air_x + right_s * eff + EDGE_PAD)
    return on_air_x, off_air_x, width


def is_band_run(item) -> bool:
    """True for a one-shot that fires DURING on-air — an on-air-anchored step with
    a positive offset, or an off-air-anchored step with a negative one."""
    if getattr(item, "kind", None) != "run":
        return False
    return (item.anchor == "start" and item.offset > 0) or \
           (item.anchor == "stop" and item.offset < 0)


def anchor_x(anchor: str, on_air_x: float, off_air_x: float) -> float:
    return on_air_x if anchor == "start" else off_air_x


def offset_to_x(anchor: str, offset: float, on_air_x: float, off_air_x: float,
                zoom: float = 1.0) -> float:
    return anchor_x(anchor, on_air_x, off_air_x) + offset * SCALE * zoom


def _snap(v: float) -> float:
    v = round(v / SNAP_S) * SNAP_S
    return 0.0 if v == 0 else v


def midpoint(on_air_x: float, off_air_x: float) -> float:
    return (on_air_x + off_air_x) / 2.0


# ── Drag resolution ──────────────────────────────────────────────────────────

def resolve_bar_start(center_x: float, on_air_x: float, off_air_x: float,
                      zoom: float = 1.0) -> float:
    """New start_offset for a bar's start handle at center_x. Constrained to the
    on-air side (can't cross the gap midpoint)."""
    x = min(center_x, midpoint(on_air_x, off_air_x))
    return _snap((x - on_air_x) / (SCALE * zoom))


def resolve_bar_stop(center_x: float, on_air_x: float, off_air_x: float,
                     zoom: float = 1.0) -> float:
    """New stop_offset for a bar's stop handle at center_x. Constrained to the
    off-air side (can't cross the gap midpoint)."""
    x = max(center_x, midpoint(on_air_x, off_air_x))
    return _snap((x - off_air_x) / (SCALE * zoom))


# A one-shot's anchor is set only in the editor, never by dragging — so there is
# no drag-time re-anchor resolver. Dragging changes only its offset (to scale from
# its fixed anchor; see the canvas), and compute_anchors widens the band so the
# on-air-anchored points stay ordered before the off-air-anchored ones.


# ── Task command → script + default args ─────────────────────────────────────

def script_of_command(command: List[str]) -> Tuple[str, List[str]]:
    """Split a task command into (script_filename, default_args).

    A task command is ``[interpreter, <dir>/<script>.py, *args]``. The script's
    parameter schema is fetched by its basename (e.g. "freq.py"); the args after
    it are the task's default values, used to pre-fill the step's parameter form.
    Returns ("", []) if the command has no .py script element.
    """
    for i, a in enumerate(command):
        if isinstance(a, str) and a.endswith(".py"):
            return a.rsplit("/", 1)[-1], list(command[i + 1:])
    return "", []


# ── Item ⇄ step conversion (the agent's flat step list) ──────────────────────

def _action_of(step: dict) -> str:
    a = step.get("action")
    return a.value if hasattr(a, "value") else str(a)


def item_to_steps(it) -> List[dict]:
    """Flatten one item to sequence-step dicts (anchor/offset_s/action/task_name/
    args/replace_args)."""
    if it.kind == "bar":
        return [
            {"anchor": "start", "offset_s": it.start_offset, "action": "start",
             "task_name": it.task_name, "args": list(it.args), "replace_args": it.replace_args},
            {"anchor": "stop", "offset_s": it.stop_offset, "action": "stop",
             "task_name": it.task_name, "args": [], "replace_args": False},
        ]
    if getattr(it, "action", "run") == "tune":
        return [
            {"anchor": it.anchor, "offset_s": it.offset, "action": "tune",
             "task_name": it.task_name, "params": dict(it.params or {})},
        ]
    if getattr(it, "action", "run") == "ramp":
        return [
            {"anchor": it.anchor, "offset_s": it.offset, "action": "ramp",
             "offset_end_s": getattr(it, "offset_end", 0.0),
             "task_name": it.task_name, "ramp": dict(it.ramp or {})},
        ]
    return [
        {"anchor": it.anchor, "offset_s": it.offset, "action": "run",
         "task_name": it.task_name, "args": list(it.args), "replace_args": it.replace_args},
    ]


def items_to_steps(items) -> List[dict]:
    out: List[dict] = []
    for it in items:
        out.extend(item_to_steps(it))
    return out


def steps_to_items(steps: List[dict]) -> List:
    """Group a flat step list back into bars + run points. Runs map 1:1; each
    start is paired with a stop of the same task (in order) to form a bar."""
    items: List = []
    starts: List[dict] = []
    stops_by_task: Dict[str, List[dict]] = defaultdict(list)

    for s in steps:
        action = _action_of(s)
        if action == "run":
            items.append(RunItem(
                task_name=s["task_name"], args=list(s.get("args") or []),
                replace_args=bool(s.get("replace_args", True)),
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"])))
        elif action == "tune":
            items.append(RunItem(
                task_name=s["task_name"], action="tune",
                params=dict(s.get("params") or {}),
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"])))
        elif action == "ramp":
            items.append(RunItem(
                task_name=s["task_name"], action="ramp",
                ramp=dict(s.get("ramp") or {}),
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"]),
                offset_end=float(s.get("offset_end_s") or 0.0)))
        elif action == "start":
            starts.append(s)
        elif action == "stop":
            stops_by_task[s["task_name"]].append(s)

    for st in starts:
        task = st["task_name"]
        rem = stops_by_task.get(task) or []
        stop = rem.pop(0) if rem else None
        items.append(BarItem(
            task_name=task, args=list(st.get("args") or []),
            replace_args=bool(st.get("replace_args", True)),
            start_offset=float(st["offset_s"]),
            stop_offset=float(stop["offset_s"]) if stop else 0.0))

    # A stop with no matching start → a bar whose start sits at on-air (0s).
    for task, rem in stops_by_task.items():
        for stop in rem:
            items.append(BarItem(task_name=task, args=[], start_offset=0.0,
                                 stop_offset=float(stop["offset_s"])))
    return items


# ── Validation (mirrors the agent's _validate_steps) ─────────────────────────

def validate(items, known_tasks: Optional[List[str]] = None) -> Optional[str]:
    """Return an error string if the item set wouldn't make a valid sequence."""
    if not items:
        return "add at least one duration or one-shot task"
    steps = items_to_steps(items)
    if any(not s["task_name"] for s in steps):
        return "every step needs a task"
    if known_tasks:
        unknown = sorted({s["task_name"] for s in steps if s["task_name"] not in known_tasks})
        if unknown:
            return "unknown task(s): " + ", ".join(unknown)
    if not any(s["anchor"] == "start" for s in steps):
        return "needs at least one on-air step"
    if not any(s["anchor"] == "stop" for s in steps):
        return "needs at least one off-air step (a duration task provides both)"
    # A tune step retunes a running duration task, so the task it targets must be
    # started by a duration (bar) step in this same sequence.
    duration_tasks = {it.task_name for it in items if getattr(it, "kind", None) == "bar"}
    for it in items:
        act = getattr(it, "action", "run")
        if act in ("tune", "ramp") and it.task_name not in duration_tasks:
            return (f"{act} step targets '{it.task_name or '(no task)'}', which no "
                    f"duration task in this sequence starts")
        if act == "ramp":
            err = _ramp_spec_error(getattr(it, "ramp", None), it.anchor)
            if err:
                return f"ramp on '{it.task_name}': {err}"
    return None


def _ramp_spec_error(spec: Optional[dict], anchor: str) -> Optional[str]:
    """Validate a ramp spec the same way the agent does (so a bad ramp is caught
    before deploy). Returns an error string or None."""
    if not spec:
        return "no ramp defined"
    from api import ramp as _ramp
    try:
        if anchor == "both":
            if spec.get("step") is None and spec.get("hold_s") is None:
                return "a window-filling ramp needs a step size or hold time"
        else:
            _ramp.resolve_ramp(spec.get("start"), spec.get("stop"),
                               step=spec.get("step"), hold_s=spec.get("hold_s"),
                               duration_s=spec.get("duration_s"))
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def min_on_air_duration(items) -> float:
    """The shortest on-air window this item set fits in (seconds). Delegates to the
    shared api.ramp math after normalising items to step-shaped objects."""
    from types import SimpleNamespace
    from api import ramp as _ramp
    objs = []
    for s in items_to_steps(items):
        r = s.get("ramp")
        robj = None
        if r:
            robj = SimpleNamespace(
                start=r.get("start"), stop=r.get("stop"), step=r.get("step"),
                hold_s=r.get("hold_s"), duration_s=r.get("duration_s"))
        objs.append(SimpleNamespace(
            anchor=s.get("anchor", "start"), offset_s=s.get("offset_s", 0.0),
            offset_end_s=s.get("offset_end_s", 0.0),
            action=s.get("action", ""), ramp=robj))
    return _ramp.min_on_air_duration(objs)
