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
    # If the run is armed with a resume offset, pass it to this task's start (only a
    # resumable duration task honours it). Carried through edit so it isn't reset.
    inject_resume_offset: bool = False
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


# ── Ramp geometry (a ramp draws as a bar between two anchored endpoints) ──────

def _ramp_duration(r: dict) -> float:
    from api import ramp as _ramp
    try:
        return _ramp.resolve_ramp(r.get("start"), r.get("stop"), steps=r.get("steps"), step=r.get("step"),
                                  hold_s=r.get("hold_s"), duration_s=r.get("duration_s"),
                                  include_first=r.get("include_first", True),
                                  include_last=r.get("include_last", True)).duration_s
    except (ValueError, TypeError):
        return 0.0


def ramp_span(it):
    """A ramp's two timeline endpoints as ((left_anchor, left_off), (right_anchor,
    right_off)) — so it can be drawn as a duration bar. A 'both' ramp spans on-air
    to off-air; a single-anchor ramp runs `duration` seconds from its anchor."""
    r = dict(getattr(it, "ramp", None) or {})
    if it.anchor == "both":
        return (("start", float(it.offset)), ("stop", float(getattr(it, "offset_end", 0.0))))
    dur = _ramp_duration(r)
    if it.anchor == "stop":
        return (("stop", float(it.offset) - dur), ("stop", float(it.offset)))
    return (("start", float(it.offset)), ("start", float(it.offset) + dur))


def _is_ramp(it) -> bool:
    return getattr(it, "action", "run") == "ramp"


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

    def take(anchor: str, off: float):
        nonlocal left_s, right_s, max_on, max_off
        if anchor == "start":
            if off < 0:
                left_s = max(left_s, -off)
            elif off > 0:
                max_on = max(max_on, off)
        else:  # off-air anchored
            if off > 0:
                right_s = max(right_s, off)
            elif off < 0:
                max_off = max(max_off, -off)

    for it in items:
        if it.kind == "bar":
            take("start", it.start_offset)
            take("stop", it.stop_offset)
        elif _is_ramp(it):
            (la, lo), (ra, ro) = ramp_span(it)
            take(la, lo)
            take(ra, ro)
        elif it.anchor == "start":
            take("start", it.offset)
        else:  # run/tune, off-air anchored
            take("stop", it.offset)
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
             "task_name": it.task_name, "args": list(it.args), "replace_args": it.replace_args,
             "inject_resume_offset": bool(getattr(it, "inject_resume_offset", False))},
            {"anchor": "stop", "offset_s": it.stop_offset, "action": "stop",
             "task_name": it.task_name, "args": [], "replace_args": False},
        ]
    if getattr(it, "action", "run") == "tune":
        return [
            {"anchor": it.anchor, "offset_s": it.offset, "action": "tune",
             "task_name": it.task_name, "params": dict(it.params or {})},
        ]
    if getattr(it, "action", "run") == "ramp":
        # A run-mode ramp carries the OTHER params' fixed values as args; a tune ramp
        # has none. replace_args mirrors a one-shot (the args are the complete set).
        return [
            {"anchor": it.anchor, "offset_s": it.offset, "action": "ramp",
             "offset_end_s": getattr(it, "offset_end", 0.0),
             "task_name": it.task_name, "ramp": dict(it.ramp or {}),
             "args": list(getattr(it, "args", []) or []),
             "replace_args": bool(getattr(it, "replace_args", True))},
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
                args=list(s.get("args") or []),
                replace_args=bool(s.get("replace_args", True)),
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
            stop_offset=float(stop["offset_s"]) if stop else 0.0,
            inject_resume_offset=bool(st.get("inject_resume_offset", False))))

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
            if spec.get("steps") is None and spec.get("step") is None and spec.get("hold_s") is None:
                return "a window-filling ramp needs a step count or hold time"
        else:
            _ramp.resolve_ramp(spec.get("start"), spec.get("stop"),
                               steps=spec.get("steps"), step=spec.get("step"), hold_s=spec.get("hold_s"),
                               duration_s=spec.get("duration_s"),
                               include_first=spec.get("include_first", True),
                               include_last=spec.get("include_last", True))
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def step_within_task_error(spans: List[Tuple[float, float]], anchor: str,
                           offset: float, offset_end: float = 0.0,
                           kind: str = "tune") -> Optional[str]:
    """Error string if a tune/ramp step fires outside every on-air span of its target
    task; None if it fits one. `spans` is [(start_offset, stop_offset), …] of the
    task's duration bars.

    A step fires anchor-relative (start → on-air + offset, stop → off-air + offset),
    so only the anchor-consistent edge can be checked without the schedule's window
    length: a start step can't precede the task's on-air start, a stop step can't
    follow its off-air stop, and a window-filling ('both') ramp must sit inside both
    edges."""
    from .param_form import fmt_duration
    if not spans:
        return f"the target task has no duration step in this sequence for the {kind} to sit inside"
    tol = 1e-6

    def fits(s: float, e: float) -> bool:
        if anchor == "both":
            return offset >= s - tol and offset_end <= e + tol
        if anchor == "stop":
            return offset <= e + tol
        return offset >= s - tol   # start

    if any(fits(s, e) for s, e in spans):
        return None
    s, e = spans[0]
    if anchor == "both":
        return (f"the ramp must stay inside the task's on-air span — start at or after "
                f"{fmt_duration(s, signed=True)} from on-air and end at or before "
                f"{fmt_duration(e, signed=True)} from off-air")
    if anchor == "stop":
        return (f"the {kind} would fire after the task goes off air; its offset must be "
                f"at or before {fmt_duration(e, signed=True)}")
    return (f"the {kind} would fire before the task goes on air; its offset must be "
            f"at or after {fmt_duration(s, signed=True)}")


def _args_to_values(args: List[str], flag_to_dest: Dict[str, str]) -> Dict[str, float]:
    """Numeric ``{dest: value}`` from a CLI arg list, keeping only flags the schema knows
    and values that parse as numbers (freq/power are numbers). A flag repeated keeps its
    last value."""
    out: Dict[str, float] = {}
    i = 0
    while i < len(args):
        dest = flag_to_dest.get(args[i])
        if dest is not None and i + 1 < len(args):
            try:
                out[dest] = float(args[i + 1])
            except (TypeError, ValueError):
                pass
            i += 2
        else:
            i += 1
    return out


def _carry_order_key(it) -> Tuple[int, float]:
    """A best-effort absolute-order key for carrying parameter state forward along one
    task's items. Start-anchored items (a bar's start, a start-anchored tune) order by
    their on-air offset; stop-anchored items happen near the end, so they sort after every
    start-anchored one. (The on-air window length isn't known at author time, so start- and
    stop-anchored offsets can't be interleaved exactly — this orders the common case, a
    series of start-anchored steps, correctly.)"""
    anchor = getattr(it, "anchor", "start")
    if it.kind == "bar":
        return (0, float(getattr(it, "start_offset", 0.0)))
    off = float(getattr(it, "offset", 0.0))
    return (1, off) if anchor == "stop" else (0, off)


def sequence_effective_values(items, task: str, base_args: List[str], specs: List[dict],
                              target_uid: int, target_key: Optional[Tuple[int, float]] = None
                              ) -> Dict[str, float]:
    """The numeric parameter state (``{dest: value}`` — e.g. the effective ``freq`` /
    ``power``) a task is running with when the step ``target_uid`` fires, by replaying the
    task's deployed ``base_args`` then every earlier same-task item in this sequence.

    A duration bar's args are the on-air baseline; a ``tune`` step merges its ``params``;
    a fire-and-exit ``run`` with ``replace_args`` resets the args. Ordering is best-effort
    (see ``_carry_order_key``). Used to fold the --power range and flag a clamp at the
    frequency actually in effect at that offset — not the step's own default."""
    flag_to_dest: Dict[str, str] = {}
    for s in specs:
        for f in s.get("flags") or []:
            flag_to_dest[f] = s.get("dest") or s.get("name")
    state = _args_to_values(base_args or [], flag_to_dest)
    mine = [it for it in items if getattr(it, "task_name", None) == task
            and getattr(it, "uid", None) != target_uid]
    if target_key is None:
        target_key = (1, float("inf"))                 # no anchor info → replay all priors
    for it in sorted(mine, key=_carry_order_key):
        if _carry_order_key(it) >= target_key:
            continue
        if it.kind == "bar":
            if getattr(it, "replace_args", True):
                state = _args_to_values(list(it.args), flag_to_dest)
            else:
                state.update(_args_to_values(list(it.args), flag_to_dest))
        elif getattr(it, "action", "run") == "tune":
            for name, val in (it.params or {}).items():
                try:
                    state[name] = float(val)
                except (TypeError, ValueError):
                    pass
        elif getattr(it, "action", "run") == "run":
            if getattr(it, "replace_args", True):
                state = _args_to_values(list(it.args), flag_to_dest)
            else:
                state.update(_args_to_values(list(it.args), flag_to_dest))
        # a ramp sweeps a single param over time — skip (no single carried value)
    return state


# ── Power achievability across the sequence (a TEMPORAL check) ────────────────────────────────
# Whether a commanded --power is deliverable depends on the transmit FREQUENCY and the calibration
# BRIDGE PARAMS (--bw, --sidelobes/enbw, …) in effect AT THE MOMENT it is commanded. In a sequence
# those change over time as steps fire, so a power RAMP's top levels can become unachievable
# partway through when a LATER tune step retunes the carrier — something a single per-step fold
# can't express. This walks each task's timeline in fire-time order, folds the achievable range at
# the state in effect at each ramp point, and flags any point that will clamp. Warn, never block;
# the fold math mirrors state.power_fold (the runtime/transmit path), so the warning agrees with
# what the unit will actually do. Held-power (fixed --power under a later retune) and bridge-param
# RAMPS are a separate, deferred case — see docs/sequence-power-achievability.md §8.


@dataclass
class AchievabilityIssue:
    """One contiguous group of power-ramp points that won't be delivered as asked (the runtime
    clamps them). ``message`` is the operator-facing line; the structured fields let callers
    regroup/reformat and let tests assert precisely. ``points`` = (step_index 0-based, level,
    fire_time_s)."""
    task: str
    param: str
    direction: str                   # "high" (clamped down to a ceiling) | "low" (raised to a floor)
    bound: float                     # the ceiling (high) / floor (low) the points hit, operating unit
    unit: str
    freq_hz: Optional[float]         # the carrier the fold used (None when unknown / constant)
    points: List[Tuple[int, float, float]]
    message: str


def _fire_time_s(anchor: str, offset: float, window_s: float) -> float:
    """Absolute seconds from ON-AIR (T0) a point fires at. start → offset; stop → window+offset
    (offset ≤ 0). Stop/'both' timing is approximate until the schedule fixes the on-air window;
    ``window_s`` is the minimum window that still fits the sequence."""
    return float(offset) if anchor != "stop" else float(window_s) + float(offset)


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    sign, sec = ("-", -sec) if sec < 0 else ("", sec)
    return f"{sign}{sec // 60}:{sec % 60:02d}"


def _steps_phrase(idxs: List[int], n: int) -> str:
    """Human phrase for a set of 0-based step indices out of ``n`` — 1-based, a contiguous run
    collapsed to a range ('steps 9–11 of 11')."""
    ones = sorted({i + 1 for i in idxs})
    if len(ones) == 1:
        return f"step {ones[0]} of {n}"
    if ones == list(range(ones[0], ones[-1] + 1)):
        return f"steps {ones[0]}–{ones[-1]} of {n}"
    return "steps " + ", ".join(str(o) for o in ones) + f" of {n}"


def _ramp_issues(task: str, param: str, unit: str,
                 hits: List[Tuple[int, float, float, str, float, Optional[float], int]]
                 ) -> List[AchievabilityIssue]:
    """Group one ramp's clamped points into issues keyed by (direction, bound, carrier) — so
    distinct retunes that each clamp a stretch read as distinct warnings, each naming its own
    ceiling/floor and carrier. Each hit = (step_index, level, fire_s, direction, bound, freq_hz,
    total_points)."""
    total = max((h[6] for h in hits), default=0)
    groups: Dict[tuple, list] = defaultdict(list)
    for (i, val, fire_s, direction, bound, freq_hz, _n) in hits:
        groups[(direction, round(bound, 3), None if freq_hz is None else round(freq_hz))].append(
            (i, val, fire_s, bound, freq_hz))
    out: List[AchievabilityIssue] = []
    for _key, grp in sorted(groups.items(), key=lambda kv: min(g[0] for g in kv[1])):
        grp.sort()
        idxs = [g[0] for g in grp]
        vals = [g[1] for g in grp]
        times = [g[2] for g in grp]
        direction = _key[0]
        bound, freq_hz = grp[0][3], grp[0][4]
        at_f = f" at {freq_hz / 1e6:.3f} MHz" if freq_hz is not None else ""
        tspan = _mmss(times[0]) if len(times) == 1 else f"{_mmss(min(times))}–{_mmss(max(times))}"
        span = _steps_phrase(idxs, total)
        vlo, vhi = min(vals), max(vals)
        if direction == "high":
            msg = (f"⚠ {task}: ramp ‘{param}’ — {span} exceed what this unit can deliver{at_f} "
                   f"(max {bound:.2f} {unit}); levels {vlo:.2f}–{vhi:.2f} {unit} at {tspan} "
                   f"will be clamped down to it.")
        else:
            msg = (f"⚠ {task}: ramp ‘{param}’ — {span} fall below what this unit can deliver{at_f} "
                   f"(min {bound:.2f} {unit}); levels {vlo:.2f}–{vhi:.2f} {unit} at {tspan} "
                   f"will be raised up to it.")
        out.append(AchievabilityIssue(
            task=task, param=param, direction=direction, bound=bound, unit=unit, freq_hz=freq_hz,
            points=[(g[0], g[1], g[2]) for g in grp], message=msg))
    return out


def achievability_warnings(items, resolve) -> List[AchievabilityIssue]:
    """Flag every power-RAMP point a unit can't deliver at the frequency/params in effect when it
    fires — the runtime clamps it, so it delivers a different power than the ramp asks. Warn, never
    block; each returned issue carries an operator-facing ``message`` plus structured fields.

    ``resolve(task_name)`` returns the task's calibration context, or None to skip the task::

        {
          "artifact":    resolved-calibration artifact dict,
          "specs":       the script's param specs (list[dict]; includes derived/hidden fields),
          "base_args":   the task's deployed command args (its on-air baseline; list[str]),
          "freq_param":  dest of the CAL_FREQ_PARAM field (str | None),
          "freq_factor": Hz per unit of that freq field (float; e.g. 1e6 for MHz),
          "power_dest":  dest of the --power field (str),
        }

    The model stays calibration-agnostic: the caller (the editor) owns cal lookup + unit scaling.
    Analysed on a FREQUENCY- or PARAMETER-dependent chain (a constant chain's fixed range is already
    enforced by the From/To field): every POWER-ramp point; a directly-SET --power (a tune/run/
    baseline step) that clamps at the operating point in effect when it fires (e.g. a spectral
    density set to the bw-10 max while an earlier step has already widened the sweep to 20); AND a
    HELD --power (set by an earlier step, not re-commanded) that a LATER freq/bridge-param event
    pushes out of range (§5 step 4 — e.g. a fixed density that clamps once a later tune doubles the
    sweep bandwidth). Bridge-param RAMPS remain a deferred case (docs/sequence-power-achievability.md
    §8)."""
    from state.power_fold import PowerFold, fold_params_from_values   # pure (no Qt); lazy

    issues: List[AchievabilityIssue] = []
    items = list(items)
    try:
        window = min_on_air_duration(items)
    except Exception:                      # noqa: BLE001 — a warning helper must never break
        window = 0.0
    tol = 0.05

    tasks: List[str] = []
    for it in items:
        t = getattr(it, "task_name", None)
        if t and t not in tasks:
            tasks.append(t)

    for task in tasks:
        info = resolve(task) if resolve else None
        if not info:
            continue
        artifact = info.get("artifact")
        specs = info.get("specs") or []
        power_dest = info.get("power_dest")
        if power_dest is None:
            continue
        fold = PowerFold.from_artifact(artifact or {})
        # A constant chain never moves under a ramp — its fixed range is enforced by the field.
        if fold is None or not (fold.freq_dependent or fold.param_dependent):
            continue
        freq_param = info.get("freq_param")
        freq_factor = float(info.get("freq_factor") or 1.0)
        base_args = list(info.get("base_args") or [])
        unit = (artifact or {}).get("operating_unit") or "dBm"

        flag_to_dest = {f: (s.get("dest") or s.get("name"))
                        for s in specs for f in (s.get("flags") or [])}
        name_to_dest = {(s.get("name") or s.get("dest")): s.get("dest") for s in specs}

        def _clamp(eval_state):
            """(direction, bound, freq_hz) if eval_state's --power clamps here, else None. Mirrors
            state.power_fold.clamp_warning's over/under test so the number matches the caption."""
            p = eval_state.get(power_dest)
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                return None
            fv = eval_state.get(freq_param) if freq_param else None
            freq_hz = (float(fv) * freq_factor
                       if isinstance(fv, (int, float)) and not isinstance(fv, bool) else None)
            params = fold_params_from_values(artifact, specs, eval_state)
            b = fold.bounds_at(freq_hz, params)
            lo, hi = b["min_power_dbm"], b["max_power_dbm"]
            if p > hi + tol:
                return ("high", hi, freq_hz)
            if p < lo - tol:
                return ("low", lo, freq_hz)
            return None

        # Build fire-time-ordered events for this task.
        events: list = []                        # (fire_s, seq_idx, kind, payload)
        for seq_idx, it in enumerate(items):
            if getattr(it, "task_name", None) != task:
                continue
            act = getattr(it, "action", "run")
            if getattr(it, "kind", None) == "bar":
                events.append((_fire_time_s("start", getattr(it, "start_offset", 0.0), window),
                               seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args)}))
            elif act == "tune":
                events.append((_fire_time_s(it.anchor, it.offset, window), seq_idx, "tune",
                               {"params": dict(getattr(it, "params", {}) or {})}))
            elif act == "run":
                events.append((_fire_time_s(it.anchor, it.offset, window), seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args)}))
            elif act == "ramp":
                r = dict(getattr(it, "ramp", None) or {})
                rdest = name_to_dest.get(r.get("param")) or flag_to_dest.get(r.get("flag"))
                if rdest != power_dest:
                    continue                     # only a POWER ramp is analysed (see docstring)
                try:
                    resolved = _resolve_ramp_points(r, it.anchor, window)
                    fires = _place_ramp_points(r, it.anchor, float(it.offset), resolved)
                except (ValueError, TypeError):
                    continue
                run_mode = r.get("mode") == "run"
                run_state = _args_to_values(list(getattr(it, "args", []) or []), flag_to_dest) \
                    if run_mode else None
                for i, (fa, foff, val) in enumerate(fires):
                    events.append((_fire_time_s(fa, foff, window), seq_idx, "ramp_point",
                                   {"uid": getattr(it, "uid", seq_idx), "i": i, "n": len(fires),
                                    "val": float(val), "rdest": rdest, "param": r.get("param"),
                                    "run_mode": run_mode, "run_state": run_state}))
        events.sort(key=lambda e: (e[0], e[1]))

        # Labels for the held-power message: the --power field's name, and each field's name (so a
        # retune/bandwidth change can be named as the operator knows it).
        pspec = next((s for s in specs if s.get("dest") == power_dest), {})
        power_label = ((pspec.get("flags") or [power_dest])[0] or power_dest).lstrip("-")
        dest_label = {s.get("dest"): ((s.get("flags") or [s.get("dest")])[0]
                                      or s.get("dest")).lstrip("-") for s in specs}

        def _changed_desc(touched: set) -> str:
            """Name what a freq/param event changed, for the held-power message — the retune to the
            new carrier and/or the bridge params it moved."""
            parts: List[str] = []
            if freq_param and freq_param in touched:
                fv = state.get(freq_param)
                if isinstance(fv, (int, float)) and not isinstance(fv, bool):
                    parts.append(f"retune to {float(fv) * freq_factor / 1e6:.3f} MHz")
            for d in sorted(touched):
                if d == freq_param or d == power_dest:
                    continue
                v = state.get(d)
                lbl = dest_label.get(d, d)
                parts.append(f"‘{lbl}’ change to {v:g}"
                             if isinstance(v, (int, float)) and not isinstance(v, bool)
                             else f"‘{lbl}’ change")
            return ", ".join(parts) or "change"

        # Walk in fire-time order, maintaining the running task state; collect ramp-point clamps AND
        # held-power clamps (a fixed --power pushed out of range by a LATER freq/bridge-param event).
        state = _args_to_values(base_args, flag_to_dest)
        hits: Dict[object, list] = defaultdict(list)
        meta: Dict[object, dict] = {}
        held_flagged = False           # is the current standing --power already known to clamp?
        for _fire_s, _si, kind, p in events:
            touched: set = set()
            if kind == "args":
                new_vals = _args_to_values(p["args"], flag_to_dest)
                if p["replace"]:
                    touched = set(state) | set(new_vals)     # a replace resets every param
                    state = new_vals
                else:
                    touched = set(new_vals)
                    state.update(new_vals)
            elif kind == "tune":
                for k, v in p["params"].items():
                    try:
                        d = name_to_dest.get(k, k)
                        state[d] = float(v)
                        touched.add(d)
                    except (TypeError, ValueError):
                        pass
            elif kind == "ramp_point":
                if p["run_mode"]:
                    eval_state = dict(p["run_state"] or {})
                    eval_state[p["rdest"]] = p["val"]
                else:
                    state[p["rdest"]] = p["val"]
                    eval_state = state
                viol = _clamp(eval_state)
                if viol:
                    direction, bound, freq_hz = viol
                    hits[p["uid"]].append((p["i"], p["val"], _fire_s, direction, bound, freq_hz,
                                           p["n"]))
                    meta[p["uid"]] = {"param": p["param"] or power_dest}
                # keep the held-power flag in step with the ramp's last value, so a later freq/param
                # event re-checks it correctly (a ramp point sets --power directly).
                held_flagged = bool(_clamp(state)) if not p["run_mode"] else held_flagged
                continue
            # Two temporal checks, split on whether THIS event commands --power itself:
            #   • it does NOT (a freq/bridge-param event): re-check the STANDING power — a later
            #     retune/bandwidth change can push a HELD --power out of range (§5 step 4). Warn on
            #     the transition INTO violation only (never re-warn while it stays clamped).
            #   • it DOES (a tune/run/baseline step setting --power): flag the COMMAND when it clamps
            #     at this moment's operating point — the operator asked for a level the unit can't
            #     deliver *here* (e.g. a density set to bw-10's max after an earlier step widened the
            #     sweep to 20). Each explicit command is its own warning, at its own fire time.
            # A constant chain is already skipped above, so any violation means a real retune/command.
            if power_dest not in touched:
                viol = _clamp(state)
                if viol and not held_flagged:
                    direction, bound, freq_hz = viol
                    issues.append(_held_power_issue(
                        task, power_label, unit, float(state[power_dest]), direction, bound,
                        freq_hz, _fire_s, _changed_desc(touched)))
                held_flagged = bool(viol)
            else:
                viol = _clamp(state)
                if viol:
                    direction, bound, freq_hz = viol
                    issues.append(_set_power_issue(
                        task, power_label, unit, float(state[power_dest]), direction, bound,
                        freq_hz, _fire_s))
                held_flagged = bool(viol)
        for uid, hitlist in hits.items():
            issues.extend(_ramp_issues(task, meta[uid]["param"], unit, hitlist))
    return issues


def _held_power_issue(task: str, param: str, unit: str, power: float, direction: str,
                      bound: float, freq_hz: Optional[float], fire_s: float,
                      changed: str) -> AchievabilityIssue:
    """A held --power (set by an earlier step) pushed out of range by a LATER freq/bridge-param
    change — named specifically: what changed, when, the held level, and the bound it now hits.
    ``points`` carries the single held point as ``(-1, level, fire_s)`` (−1 = not a ramp step)."""
    at_f = f" at {freq_hz / 1e6:.3f} MHz" if freq_hz is not None else ""
    if direction == "high":
        tail = (f"exceeds what this unit can deliver{at_f} (max {bound:.2f} {unit}) and will be "
                f"clamped down to it")
    else:
        tail = (f"falls below what this unit can deliver{at_f} (min {bound:.2f} {unit}) and will be "
                f"raised up to it")
    msg = (f"⚠ {task}: the held ‘{param}’ {power:.2f} {unit} {tail} after the {_mmss(fire_s)} "
           f"{changed}.")
    return AchievabilityIssue(task=task, param=param, direction=direction, bound=bound, unit=unit,
                              freq_hz=freq_hz, points=[(-1, power, fire_s)], message=msg)


def _set_power_issue(task: str, param: str, unit: str, power: float, direction: str,
                     bound: float, freq_hz: Optional[float], fire_s: float) -> AchievabilityIssue:
    """A --power COMMAND (a tune/run/baseline step) whose level the unit can't deliver at the
    operating point in effect when it fires — named specifically: the level, when it's commanded,
    the carrier folded at, and the bound it hits. Distinct from a HELD power pushed out of range by
    a later change (``_held_power_issue``): this is the operator's own explicit command clamping.
    ``points`` carries the single commanded point as ``(-1, level, fire_s)`` (−1 = not a ramp
    step)."""
    at_f = f" at {freq_hz / 1e6:.3f} MHz" if freq_hz is not None else ""
    if direction == "high":
        tail = (f"exceeds what this unit can deliver{at_f} (max {bound:.2f} {unit}) and will be "
                f"clamped down to it")
    else:
        tail = (f"falls below what this unit can deliver{at_f} (min {bound:.2f} {unit}) and will be "
                f"raised up to it")
    msg = f"⚠ {task}: ‘{param}’ set to {power:.2f} {unit} at {_mmss(fire_s)} {tail}."
    return AchievabilityIssue(task=task, param=param, direction=direction, bound=bound, unit=unit,
                              freq_hz=freq_hz, points=[(-1, power, fire_s)], message=msg)


def _resolve_ramp_points(r: dict, anchor: str, window_s: float):
    """resolve_ramp for a ramp spec; a 'both' (window-filling) ramp resolves against the minimum
    on-air window (its point TIMES are then approximate until the schedule fixes the window)."""
    from api import ramp as _ramp
    return _ramp.resolve_ramp(
        r.get("start"), r.get("stop"), steps=r.get("steps"), step=r.get("step"),
        hold_s=r.get("hold_s"), duration_s=r.get("duration_s"),
        window_s=(window_s if anchor == "both" else None),
        include_first=r.get("include_first", True), include_last=r.get("include_last", True))


def _place_ramp_points(r: dict, anchor: str, offset: float, resolved):
    from api import ramp as _ramp
    return _ramp.place_ramp("start" if anchor == "both" else anchor, offset, resolved)


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
                start=r.get("start"), stop=r.get("stop"),
                steps=r.get("steps"), step=r.get("step"),
                hold_s=r.get("hold_s"), duration_s=r.get("duration_s"))
        objs.append(SimpleNamespace(
            anchor=s.get("anchor", "start"), offset_s=s.get("offset_s", 0.0),
            offset_end_s=s.get("offset_end_s", 0.0),
            action=s.get("action", ""), ramp=robj))
    return _ramp.min_on_air_duration(objs)
