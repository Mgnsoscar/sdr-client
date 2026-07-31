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
MIDDLE_GAP = 220       # px between ON-AIR and OFF-AIR (fixed; the busiest region)
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
    """A one-shot fire-and-exit step: a single point, no end."""
    task_name: str
    args: List[str] = field(default_factory=list)
    replace_args: bool = True
    anchor: str = "start"       # "start" (on-air) | "stop" (off-air)
    offset: float = 0.0
    uid: int = 0
    kind: str = "run"

    def __post_init__(self):
        if not self.uid:
            self.uid = next(_ids)


# ── Coordinate mapping ───────────────────────────────────────────────────────

def compute_anchors(items) -> Tuple[float, float, int]:
    """Return (on_air_x, off_air_x, canvas_width) sized to hold every item plus
    headroom. Warm-up extends left of ON-AIR, cool-down right of OFF-AIR."""
    left_s = MIN_SIDE_S
    right_s = MIN_SIDE_S
    for it in items:
        if it.kind == "bar":
            if it.start_offset < 0:
                left_s = max(left_s, -it.start_offset)
            if it.stop_offset > 0:
                right_s = max(right_s, it.stop_offset)
        else:  # run
            if it.anchor == "start" and it.offset < 0:
                left_s = max(left_s, -it.offset)
            if it.anchor == "stop" and it.offset > 0:
                right_s = max(right_s, it.offset)
    left_s += HEADROOM_S
    right_s += HEADROOM_S
    on_air_x = EDGE_PAD + left_s * SCALE
    off_air_x = on_air_x + MIDDLE_GAP
    width = int(off_air_x + right_s * SCALE + EDGE_PAD)
    return on_air_x, off_air_x, width


def anchor_x(anchor: str, on_air_x: float, off_air_x: float) -> float:
    return on_air_x if anchor == "start" else off_air_x


def offset_to_x(anchor: str, offset: float, on_air_x: float, off_air_x: float) -> float:
    return anchor_x(anchor, on_air_x, off_air_x) + offset * SCALE


def _snap(v: float) -> float:
    v = round(v / SNAP_S) * SNAP_S
    return 0.0 if v == 0 else v


def midpoint(on_air_x: float, off_air_x: float) -> float:
    return (on_air_x + off_air_x) / 2.0


# ── Drag resolution ──────────────────────────────────────────────────────────

def resolve_bar_start(center_x: float, on_air_x: float, off_air_x: float) -> float:
    """New start_offset for a bar's start handle at center_x. Constrained to the
    on-air side (can't cross the gap midpoint)."""
    x = min(center_x, midpoint(on_air_x, off_air_x))
    return _snap((x - on_air_x) / SCALE)


def resolve_bar_stop(center_x: float, on_air_x: float, off_air_x: float) -> float:
    """New stop_offset for a bar's stop handle at center_x. Constrained to the
    off-air side (can't cross the gap midpoint)."""
    x = max(center_x, midpoint(on_air_x, off_air_x))
    return _snap((x - off_air_x) / SCALE)


def resolve_run(center_x: float, on_air_x: float, off_air_x: float) -> Tuple[str, float]:
    """New (anchor, offset) for a run point at center_x. Left of the midpoint is
    on-air, right is off-air — so a run re-anchors by crossing the middle."""
    if center_x < midpoint(on_air_x, off_air_x):
        return "start", _snap((center_x - on_air_x) / SCALE)
    return "stop", _snap((center_x - off_air_x) / SCALE)


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
    return None
