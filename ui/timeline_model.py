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
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

# ── Capability gate ──────────────────────────────────────────────────────────
# Agent >= 1.16.0 understands a HOLD step (operator-gated pause). Phase 0 just
# defines the string here, alongside the calibration *_CAPABILITY cluster in
# ui/calibration_panel.py; wiring the _supports/_blocks_on_* gate into the arm/
# authoring UI is Phase 2 (docs/sequence-hold-step.md §11).
SEQUENCE_HOLD_CAPABILITY = "sequence-hold"
# Agent >= 1.18.0 exposes POST /sequence-runs/{id}/hold-now (Fast-Forward-to-Hold, Phase 3b):
# jump a RUNNING hold-aware run straight to its Hold. The client gates the "Hold now" button on it.
SEQUENCE_HOLD_NOW_CAPABILITY = "sequence-hold-now"
# Agent >= 1.19.0 honours ProceedRequest.steps (edit-while-holding, Phase 3c): proceed re-resolves
# window B from the operator's edited sequence. The client gates its window-B edit UI on it (a
# ≤1.18 agent would silently ignore the edit and run the stored window B).
SEQUENCE_HOLD_EDIT_CAPABILITY = "sequence-hold-edit"
# Agent >= 1.26.0 resolves anchor="enter" — a step measured from the Hold's ENTER instant (the
# pause's start; a ramp tied by its END so it finishes as the pause begins). Window A, known at arm.
# The client gates saving / hold-aware arming of such a sequence on it (a safety gate: an older agent
# rejects the anchor value). The schedule/plan path compiles the Hold out (collapse_hold), so it
# never sends the anchor.
SEQUENCE_HOLD_ENTER_CAPABILITY = "sequence-hold-enter"
SEQUENCE_HOLD_ENTER_MIN_VERSION = (1, 26, 0)
# Agent >= 1.21.0 serves GET /sequence-runs/{id}/log-table (the run's spreadsheet-shaped log). The
# client gates its "Export log…" button on it — an older agent has no such endpoint to export from.
SEQUENCE_LOG_TABLE_CAPABILITY = "sequence-log-table"
# Agent >= 1.24.0 resolves a step anchored to ANOTHER step's edge (anchor="step", the DAG). The
# client gates saving/arming a sequence that uses a step anchor on it — a ≤1.23 agent can't resolve
# the anchor (it would mis-fire), so this is a safety gate, not just a feature flag.
SEQUENCE_STEP_ANCHOR_CAPABILITY = "sequence-step-anchor"
SEQUENCE_STEP_ANCHOR_MIN_VERSION = (1, 24, 0)
# Agent >= 1.25.0 accepts a NEGATIVE offset on a step anchor (a step-anchored step firing BEFORE the
# edge it hangs off, like a start/stop anchor's lead-in). A ≤1.24 agent 400s on it, so the client
# gates saving/arming a sequence that uses a negative step offset on this string (a safety gate).
SEQUENCE_STEP_ANCHOR_NEG_CAPABILITY = "sequence-step-anchor-negative"
SEQUENCE_STEP_ANCHOR_NEG_MIN_VERSION = (1, 25, 0)

# Agent >= 1.28.0 detects a dead-but-alive RF fault (a halted GNU Radio flowgraph), stamps
# ProcessStatus.health / fires a TaskHealthEvent, couples it into an owning run, and auto-drops RF
# (docs/rf-fault-recovery.md §5). The client renders the fault pill + alarm UNCONDITIONALLY — those
# only reflect data an agent chooses to send, so an older agent simply never sends it (no gate
# needed). This capability is reserved for the Phase-2 "Restart" affordance (which the agent must
# understand); Phase 1 exposes it only so that button can gate on it later.
TASK_RF_HEALTH_CAPABILITY = "task-rf-health"

# The `sequence-hold` capability is advertised from agent 1.16.0, but 1.16.0 shipped the Hold
# DATA MODEL ONLY — a hold-aware arm was refused (the Phase-0 guard). The HOLDING RUNTIME (park →
# proceed) only works from 1.17.0, behind the SAME capability string, so the capability alone can't
# tell a 1.16.0 unit apart from a runnable one. `hold_runtime_supported` also checks the version so
# a 1.16.0 unit gets a clean client-side block instead of an agent-side refusal. See §11.
SEQUENCE_HOLD_RUNTIME_MIN_VERSION = (1, 17, 0)


def _agent_version_tuple(version: str) -> tuple:
    """Parse an agent version string ("1.17.0") to an int tuple for comparison. Stops at the
    first non-numeric component and returns () for a blank/unparseable version (treated as
    "unknown" by the caller)."""
    parts: list = []
    for chunk in str(version or "").split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def hold_runtime_supported(client) -> bool:
    """True iff the unit's agent both advertises `sequence-hold` AND runs the Phase-1 HOLDING
    RUNTIME (agent >= 1.17.0), so a hold-aware arm will actually park at the Hold rather than be
    refused. An unknown/blank version (e.g. before /info has been read) is treated as new enough
    when the capability is present — the capability stays authoritative where the version is
    simply unavailable — so this never blocks a real, capable unit. docs/sequence-hold-step.md §11."""
    try:
        if not client.supports(SEQUENCE_HOLD_CAPABILITY):
            return False
    except Exception:  # noqa: BLE001 — a client without a usable supports() can't run a Hold
        return False
    ver = _agent_version_tuple(getattr(client, "agent_version", "") or "")
    if not ver:                     # version unknown → capability is authoritative
        return True
    ver = ver + (0,) * (len(SEQUENCE_HOLD_RUNTIME_MIN_VERSION) - len(ver))   # pad "1.17" → (1,17,0)
    return ver >= SEQUENCE_HOLD_RUNTIME_MIN_VERSION


def hold_enter_supported(client) -> bool:
    """True iff the unit's agent resolves a step anchored to the Hold's START (anchor="enter":
    advertises `sequence-hold-enter` AND runs agent >= 1.26.0). Same shape as
    `hold_runtime_supported`: an unknown/blank version with the capability present is treated as
    capable, so this never blocks a real capable unit."""
    try:
        if not client.supports(SEQUENCE_HOLD_ENTER_CAPABILITY):
            return False
    except Exception:  # noqa: BLE001
        return False
    ver = _agent_version_tuple(getattr(client, "agent_version", "") or "")
    if not ver:
        return True
    ver = ver + (0,) * (len(SEQUENCE_HOLD_ENTER_MIN_VERSION) - len(ver))
    return ver >= SEQUENCE_HOLD_ENTER_MIN_VERSION


def uses_hold_enter(items) -> bool:
    """True when any item is anchored to the Hold's START (anchor="enter")."""
    return any(getattr(it, "anchor", "") == "enter" for it in items or [])


# Agent >= 1.27.0 PAUSES a ramp that crosses the Hold: the points up to the pause fire in window A,
# the level reached holds through the pause, and the remaining points resume after Proceed shifted
# by the pause's length. A ≤1.26 agent kept the whole ramp in window A and DELAYED the pause until
# the ramp finished (silently missing the hold offset), so the client gates saving / hold-aware
# arming of such a sequence on this string. The schedule/plan path compiles the Hold out (the ramp
# runs straight through), so it never needs the gate.
SEQUENCE_HOLD_RAMP_PAUSE_CAPABILITY = "sequence-hold-ramp-pause"
SEQUENCE_HOLD_RAMP_PAUSE_MIN_VERSION = (1, 27, 0)


def hold_ramp_pause_supported(client) -> bool:
    """True iff the unit's agent pauses a ramp that crosses the Hold (advertises
    `sequence-hold-ramp-pause` AND runs agent >= 1.27.0). Same shape as `hold_enter_supported`:
    an unknown/blank version with the capability present is treated as capable."""
    try:
        if not client.supports(SEQUENCE_HOLD_RAMP_PAUSE_CAPABILITY):
            return False
    except Exception:  # noqa: BLE001
        return False
    ver = _agent_version_tuple(getattr(client, "agent_version", "") or "")
    if not ver:
        return True
    ver = ver + (0,) * (len(SEQUENCE_HOLD_RAMP_PAUSE_MIN_VERSION) - len(ver))
    return ver >= SEQUENCE_HOLD_RAMP_PAUSE_MIN_VERSION


def step_anchor_supported(client) -> bool:
    """True iff the unit's agent resolves a step-to-step anchor (advertises
    `sequence-step-anchor` AND runs agent >= 1.24.0). Same belt-and-suspenders shape as
    `hold_runtime_supported`: an unknown/blank version with the capability present is treated
    as capable (the capability stays authoritative), so this never blocks a real capable unit."""
    try:
        if not client.supports(SEQUENCE_STEP_ANCHOR_CAPABILITY):
            return False
    except Exception:  # noqa: BLE001
        return False
    ver = _agent_version_tuple(getattr(client, "agent_version", "") or "")
    if not ver:
        return True
    ver = ver + (0,) * (len(SEQUENCE_STEP_ANCHOR_MIN_VERSION) - len(ver))
    return ver >= SEQUENCE_STEP_ANCHOR_MIN_VERSION


def step_anchor_negative_supported(client) -> bool:
    """True iff the unit's agent accepts a NEGATIVE offset on a step anchor (advertises
    `sequence-step-anchor-negative` AND runs agent >= 1.25.0). Same belt-and-suspenders shape as
    `step_anchor_supported`; an unknown/blank version with the capability present is capable."""
    try:
        if not client.supports(SEQUENCE_STEP_ANCHOR_NEG_CAPABILITY):
            return False
    except Exception:  # noqa: BLE001
        return False
    ver = _agent_version_tuple(getattr(client, "agent_version", "") or "")
    if not ver:
        return True
    ver = ver + (0,) * (len(SEQUENCE_STEP_ANCHOR_NEG_MIN_VERSION) - len(ver))
    return ver >= SEQUENCE_STEP_ANCHOR_NEG_MIN_VERSION


def task_rf_health_supported(client) -> bool:
    """True iff the unit's agent advertises `task-rf-health` — it detects a dead-but-alive RF
    fault, stamps ProcessStatus.health, fires a TaskHealthEvent, and auto-drops RF. The Phase-1
    fault PILL + ALARM never gate on this (they only reflect data an older agent won't send); this
    is for the Phase-2 "Restart" affordance the agent must understand. Capability-only (no version
    floor — the string is added at 1.28.0 alongside the behaviour)."""
    try:
        return bool(client.supports(TASK_RF_HEALTH_CAPABILITY))
    except Exception:  # noqa: BLE001
        return False

# ── Geometry constants ───────────────────────────────────────────────────────
SCALE = 3.0            # px per second in the warm-up / cool-down zones
MIDDLE_GAP = 220       # base px between ON-AIR and OFF-AIR (the on-air band)
BAND_PAD = 130         # min px gap between the on-air and off-air groups in the band
EDGE_PAD = 70          # px of empty space beyond the furthest item on each side
HEADROOM_S = 30.0      # seconds of extra drag room kept beyond the furthest item
MIN_SIDE_S = 60.0      # each side is at least this many seconds wide
SNAP_S = 1.0           # drag snaps offsets to this granularity (seconds)

_ids = itertools.count(1)

# A bar (duration task) has TWO anchorable edges but flattens to two wire steps; the OFF-AIR stop
# step's wire id is the bar's id + this suffix, so a dependent can anchor to either edge. Chosen
# unlikely to collide with a generated id ("st-<hex>").
BAR_STOP_SUFFIX = "~stop"


# ── Items ────────────────────────────────────────────────────────────────────

@dataclass
class BarItem:
    """A duration task: start end (on-air) + stop end (off-air), one task."""
    task_name: str
    args: List[str] = field(default_factory=list)
    replace_args: bool = True
    start_offset: float = 0.0   # seconds relative to the START anchor (see start_anchor)
    stop_offset: float = 0.0    # seconds relative to OFF-AIR (anchor="stop")
    # Which anchor the START end hangs off: "start" = ON-AIR (T0, the usual case),
    # "hold" = the Hold's resume instant (a window-B duration task), or "step" = ANOTHER
    # step/task's edge (start_anchor_step_id + start_anchor_edge below). Its STOP always stays
    # OFF-AIR. start_offset is measured from that anchor. "hold" only when a Hold exists.
    start_anchor: str = "start"
    # For being an anchor TARGET: a stable cross-reference id (the bar's ON-AIR start edge; its
    # OFF-AIR stop edge is the wire id + BAR_STOP_SUFFIX). Assigned when another step anchors to
    # this task. And for being an anchor SOURCE (start_anchor == "step"): which step this task's
    # START hangs off + which of its edges. Round-tripped; resolved by resolve_step_offsets.
    step_id: str = ""
    start_anchor_step_id: str = ""
    start_anchor_edge: str = "end"
    # If the run is armed with a resume offset, pass it to this task's start (only a
    # resumable duration task honours it). Carried through edit so it isn't reset.
    inject_resume_offset: bool = False
    # The calibrated --power CONTROL QUANTITY this step is authored in (a CAL_POWER_LAWS view id,
    # or None for the signal default) — the quantity HELD from this step forward. See SequenceStep.
    power_view: Optional[str] = None
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
    # The calibrated --power CONTROL QUANTITY this step is authored in (a CAL_POWER_LAWS view id,
    # or None for the signal default) — the quantity HELD from this step forward. See SequenceStep.
    power_view: Optional[str] = None
    # When set, the dest of a --power INJECTED by the hold precompute (a --bw change re-deriving base
    # to hold the standing quantity), not set by the operator. Present only on DEPLOYED items; the
    # canvas is always clean (set_steps strips this dest), so the authoring UI never shows it.
    power_hold_dest: Optional[str] = None
    # Step-to-step anchoring (Phase 1). `step_id` is a stable cross-reference id (wire `id`),
    # assigned only when this item is referenced by another; `anchor_step_id`/`anchor_edge` are set
    # when THIS item hangs off another (anchor == "step"): it fires at the target's start/end edge
    # + `offset` (offset >= 0). Round-tripped; resolved topologically by resolve_step_offsets.
    step_id: str = ""
    anchor_step_id: str = ""
    anchor_edge: str = "end"    # "start" | "end" of the target's extent
    # Which of THIS item's edges is tied to the target (step anchors only). A point has one
    # edge, so this only matters for a RAMP: "start" (the default — the ramp runs forward from
    # `target edge + offset`) or "end" (the ramp's END sits at `target edge + offset` and the ramp
    # runs BACKWARD from there; `offset` is then the END's offset). On the wire `offset_s` is always
    # the START's offset (= offset − duration for an end tie), so the agent needs no new logic.
    anchor_own_edge: str = "start"
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


def ramp_span(it, h_off: Optional[float] = None, step_bases: Optional[Dict[int, float]] = None,
              off_bases: Optional[Dict[int, float]] = None):
    """A ramp's two timeline endpoints as ((left_anchor, left_off), (right_anchor,
    right_off)) — so it can be drawn as a duration bar. A 'both' ramp spans on-air
    to off-air; a single-anchor ramp runs `duration` seconds from its anchor. A
    window-B (`anchor="hold"`) ramp is placed as if start-anchored at the hold's
    position (`h_off + its offset`) — the geometry treats the hold as a start-side
    dwell; the stored anchor stays "hold" (see effective_anchor_offset). A step-anchored
    (`anchor="step"`) ramp runs forward from its resolved base — on the ON-AIR clock
    (step_bases[uid]) or, when its chain roots off-air, the OFF-AIR clock (off_bases[uid])."""
    r = dict(getattr(it, "ramp", None) or {})
    if it.anchor == "both":
        return (("start", float(it.offset)), ("stop", float(getattr(it, "offset_end", 0.0))))
    dur = _ramp_duration(r)
    if it.anchor == "stop":
        return (("stop", float(it.offset) - dur), ("stop", float(it.offset)))
    if it.anchor == "hold" and h_off is not None:
        base = h_off + float(it.offset)
        return (("start", base), ("start", base + dur))
    if it.anchor == "enter" and h_off is not None:
        # Tied by its END to the Hold's ENTER instant (the pause's start): `offset` is the end's
        # offset from the pause (≤ 0 — like a stop-anchored ramp's offset from off-air), the ramp
        # runs backward from it, in window A on the on-air clock.
        end = h_off + float(it.offset)
        return (("start", end - dur), ("start", end))
    if it.anchor == "step":
        uid = getattr(it, "uid", None)
        base = (step_bases or {}).get(uid)
        if base is not None:
            return (("start", base), ("start", base + dur))
        obase = (off_bases or {}).get(uid)
        if obase is not None:
            return (("stop", obase), ("stop", obase + dur))     # chain roots off-air
        base = float(it.offset)              # orphan → draw sanely
        return (("start", base), ("start", base + dur))
    return (("start", float(it.offset)), ("start", float(it.offset) + dur))


def _is_ramp(it) -> bool:
    return getattr(it, "action", "run") == "ramp"


def is_step_source(it) -> bool:
    """True when `it` hangs off ANOTHER step (anchor="step") — a run/tune/ramp via `anchor`, or a
    duration task (bar) via its START anchor (`start_anchor="step"`; its stop stays off-air)."""
    if getattr(it, "kind", None) == "bar":
        return getattr(it, "start_anchor", "start") == "step"
    return getattr(it, "anchor", "start") == "step"


def step_source_ref(it) -> Tuple[str, str]:
    """(target step_id, target edge) a step-anchor SOURCE hangs off — a run via anchor_*, a bar
    via start_anchor_*. ("", "end") for anything that isn't a step source."""
    if getattr(it, "kind", None) == "bar":
        return (getattr(it, "start_anchor_step_id", "") or "",
                getattr(it, "start_anchor_edge", "end") or "end")
    return (getattr(it, "anchor_step_id", "") or "",
            getattr(it, "anchor_edge", "end") or "end")


def own_edge_shift(it) -> float:
    """Seconds between a step-anchored item's TIED edge and its START: the ramp duration for a
    ramp tied by its END (`anchor_own_edge="end"`), else 0. `offset − own_edge_shift` is the
    start's offset from the target edge (the wire `offset_s`)."""
    if (_is_ramp(it) and getattr(it, "anchor", "start") == "step"
            and (getattr(it, "anchor_own_edge", "start") or "start") == "end"):
        return _ramp_duration(dict(getattr(it, "ramp", None) or {}))
    return 0.0


def step_wire_offset(it) -> float:
    """The `offset_s` a step-anchored RunItem puts on the wire: its START's offset from the target
    edge. Equal to `offset` except for an end-tied ramp (offset − duration). A bar's start offset
    is already its start's offset."""
    if getattr(it, "kind", None) == "bar":
        return float(getattr(it, "start_offset", 0.0))
    return float(getattr(it, "offset", 0.0)) - own_edge_shift(it)


def _is_hold(it) -> bool:
    return getattr(it, "action", "run") == "hold"


def hold_offset(items) -> Optional[float]:
    """The on-air offset of the Hold marker (window A's end / the hold's position),
    or None if the timeline has no Hold. There is at most one Hold in v1."""
    for it in items:
        if _is_hold(it):
            return float(getattr(it, "offset", 0.0))
    return None


def has_hold(items) -> bool:
    return any(_is_hold(it) for it in items)


# ── Per-task colour + row ordering (editor redesign) ──────────────────────────────
# Each DURATION task gets a stable hue; its tunes/ramps inherit it (same task_name);
# a one-shot task gets its own. Hues are picked DISTINCT from the reserved status trio
# (green #1D9E75 / amber #BA7517 / red #C23B3B) so colour means "which task", never state.
TASK_HUES = ["#1E8FA3", "#6D5AC4", "#B5487E", "#3F63C4", "#157F93", "#7E4FB0", "#B06A2E", "#2E7D57"]


def task_hue_map(items) -> Dict[str, str]:
    """task_name -> hue hex, assigned in first-seen order over the (non-Hold) items. A tune
    or ramp shares its parent duration task's ``task_name``, so it resolves to the same hue;
    a one-shot's own task gets the next hue. Deterministic for a given item order."""
    order: List[str] = []
    for it in items:
        if _is_hold(it):
            continue
        name = getattr(it, "task_name", "") or ""
        if name and name not in order:
            order.append(name)
    return {name: TASK_HUES[i % len(TASK_HUES)] for i, name in enumerate(order)}


def _row_fire(it, step_bases: Optional[Dict[int, float]] = None) -> float:
    """Best-effort on-air offset used only to ORDER a task's steps within its group. A
    step-anchored item is ordered at its RESOLVED base (target edge + offset, from
    step_bases) — not its raw offset-from-edge, which would sort it as if it fired at that
    raw value regardless of when its target actually fires."""
    if getattr(it, "kind", None) == "bar":
        return float(getattr(it, "start_offset", 0.0))
    if getattr(it, "anchor", "start") == "step" and step_bases is not None:
        base = step_bases.get(getattr(it, "uid", None))
        if base is not None:
            return base
    return float(getattr(it, "offset", 0.0))


def _is_oneshot(it) -> bool:
    """A one-shot RUN launches a task once — it does not modify a running duration task
    (a tune/ramp does), so it is not a child of any duration bar and never groups under
    one, even a bar that happens to share its task_name."""
    return getattr(it, "kind", None) != "bar" and getattr(it, "action", "run") == "run"


def _group_key_of(it):
    """Grouping key for display_order. Tunes/ramps group under their parent task
    (task_name, beneath its bar); a one-shot run stands alone (a unique per-item key)."""
    if _is_oneshot(it):
        return ("\x00oneshot", getattr(it, "uid", id(it)))
    return getattr(it, "task_name", "") or ""


def _row_kind_rank(it) -> int:
    """Within a task's group the rows sort by KIND first: the duration bar, then that
    task's RAMPS, then its TUNES — so a task's ramps always sit directly under it, above
    the tunes (fire time breaks ties within each kind). A one-shot run is its own group,
    so this rank only orders a bar and its tune/ramp children."""
    if getattr(it, "kind", None) == "bar":
        return 0
    action = getattr(it, "action", "run")
    if action == "ramp":
        return 1
    if action == "tune":
        return 2
    return 3


def display_order(items):
    """(rows, holds) for the Gantt-style canvas: a duration bar leads its group with that
    task's ramps then its tunes beneath it (fire time breaking ties within each kind); a
    one-shot RUN stands on its OWN row, never grouped under a duration task (it launches a
    task once rather than modifying a running one). The duration-task groups come first
    (ordered by earliest fire), then ALL one-shots collected at the BOTTOM (ordered among
    themselves by fire time) — they are independent of the tasks, so they don't interleave.
    Holds own no row (they paint as dividers) and are returned separately. Pure; does not
    mutate the input and is never used for serialisation (that keeps the authored order)."""
    holds = [it for it in items if _is_hold(it)]
    step_bases = resolve_step_offsets(items, hold_offset(items))
    seen: list = []
    groups: Dict[object, list] = {}
    for it in items:
        if _is_hold(it):
            continue
        key = _group_key_of(it)
        if key not in groups:
            groups[key] = []
            seen.append(key)
        groups[key].append(it)
    # Duration-task groups first, then all one-shots at the bottom; within each band by
    # earliest fire, first-seen index breaking ties so the order is stable.
    def _group_key(key):
        g = groups[key]
        band = 1 if all(_is_oneshot(it) for it in g) else 0
        return (band, min(_row_fire(it, step_bases) for it in g), seen.index(key))
    rows: list = []
    for key in sorted(seen, key=_group_key):
        g = sorted(groups[key],
                   key=lambda it: (_row_kind_rank(it), _row_fire(it, step_bases)))
        rows.extend(g)
    return rows, holds


def effective_anchor_offset(item, h_off: Optional[float],
                            step_bases: Optional[Dict[int, float]] = None,
                            off_bases: Optional[Dict[int, float]] = None) -> Tuple[str, float]:
    """(anchor, offset) used for GEOMETRY/placement only. A window-B item (`anchor="hold"`) is
    placed as if start-anchored at `hold_offset + its offset`; a step-anchored item
    (`anchor="step"`) whose chain roots at ON-AIR is placed start-side at its resolved base
    (`step_bases[uid]`), one whose chain roots at OFF-AIR stop-side at its off-air base
    (`off_bases[uid]`, when supplied — otherwise it falls back on-air); every other item keeps
    its own anchor/offset. The stored item keeps its real anchor — drawing only, never round-trip.
    An ORPHANED anchor is placed start-side at its own offset, so it draws sanely on-air."""
    anchor = getattr(item, "anchor", "start")
    off = float(getattr(item, "offset", 0.0))
    if anchor in ("hold", "enter"):
        # "hold" = forward from the Hold's RESUME instant (window B); "enter" = measured from
        # the Hold's ENTER instant (the pause's start, window A). Both live at h_off + offset on
        # the on-air axis for geometry (the canvas inserts the hold band for "hold" only).
        return "start", (h_off or 0.0) + off
    if anchor == "step":
        uid = getattr(item, "uid", None)
        base = (step_bases or {}).get(uid)
        if base is not None:
            return "start", base
        obase = (off_bases or {}).get(uid)
        if obase is not None:
            return "stop", obase                      # its chain roots at off-air
        return "start", off                           # orphan fallback
    return anchor, off


def ramp_hold_cross(it, h_off: Optional[float], step_bases: Optional[Dict[int, float]] = None,
                    off_bases: Optional[Dict[int, float]] = None) -> Optional[Tuple[float, float]]:
    """(start, end) on the on-air clock of a window-A ramp that CROSSES the Hold — it starts at
    or before the pause and ends after it — else None. Such a ramp is PAUSED at the Hold (agent
    ≥ 1.27.0, `sequence-hold-ramp-pause`): the level it has reached holds through the pause and
    its remaining points resume after Proceed, shifted by the pause's length. A window-B
    (hold-anchored) ramp starts at/after the resume edge and an enter-anchored one ends at/before
    the pause, so neither crosses; a window-filling 'both' ramp has no single on-air span."""
    if h_off is None or not _is_ramp(it):
        return None
    if getattr(it, "anchor", "start") in ("hold", "enter", "both"):
        return None
    try:
        (la, lo), (ra, ro) = ramp_span(it, h_off, step_bases, off_bases)
    except (ValueError, TypeError):
        return None
    if la != "start" or ra != "start":
        return None
    lo, hi = min(lo, ro), max(lo, ro)
    if lo > h_off + 1e-6 or hi <= h_off + 1e-6:
        return None
    # Crossing means a POINT FIRES after the pause (the agent defers by fires); a last level whose
    # hold merely spills past the pause is absorbed by it — nothing is left to resume.
    if not _ramp_fires_after(dict(getattr(it, "ramp", None) or {}), lo, h_off):
        return None
    return (float(lo), float(hi))


def _ramp_fires_after(r: dict, start_offset: float, h_off: float) -> bool:
    """True when a start-laid ramp spec placed at `start_offset` has a point firing after `h_off`."""
    try:
        resolved = _resolve_ramp_points(r, "start", 0.0)
        fires = _place_ramp_points(r, "start", float(start_offset), resolved)
    except (ValueError, TypeError):
        return False
    return any(float(off) > h_off + 1e-6 for (_a, off, _v) in fires)


def ramp_crosses_hold(items) -> bool:
    """True when any ramp on the timeline crosses the Hold (see `ramp_hold_cross`)."""
    h_off = hold_offset(items)
    if h_off is None:
        return False
    sb = resolve_step_offsets(items, h_off)
    ob = resolve_step_offsets_off(items, h_off)
    return any(ramp_hold_cross(it, h_off, sb, ob) is not None for it in items)


def ramp_level_at_pause(it, h_off: Optional[float]) -> Optional[float]:
    """The value a Hold-crossing ramp is FROZEN at through the pause: its last point fired at or
    before the pause (the agent holds the level reached). None when the ramp doesn't cross."""
    cross = ramp_hold_cross(it, h_off)
    if cross is None:
        return None
    r = dict(getattr(it, "ramp", None) or {})
    try:
        resolved = _resolve_ramp_points(r, "start", 0.0)
        fires = _place_ramp_points(r, "start", cross[0], resolved)
    except (ValueError, TypeError):
        return None
    before = [v for (_a, off, v) in fires if float(off) <= h_off + 1e-6]
    return float(before[-1]) if before else None


def _wire_get(s, key: str, default=None):
    return s.get(key, default) if isinstance(s, dict) else getattr(s, key, default)


def ramp_crosses_hold_steps(steps) -> bool:
    """Wire-level `ramp_crosses_hold` for stored SequenceSteps (the arm-time gate): a
    start-anchored RAMP step that starts at or before the Hold and ends after it."""
    h_off = None
    for s in steps or []:
        a = _wire_get(s, "action")
        if str(getattr(a, "value", a) or "") == "hold":
            h_off = float(_wire_get(s, "offset_s", 0.0) or 0.0)
            break
    if h_off is None:
        return False
    for s in steps or []:
        a = _wire_get(s, "action")
        if str(getattr(a, "value", a) or "") != "ramp":
            continue
        if (_wire_get(s, "anchor", "start") or "start") != "start":
            continue
        r = _wire_get(s, "ramp")
        rd = r if isinstance(r, dict) else (r.model_dump() if hasattr(r, "model_dump") else None)
        if not rd:
            continue
        lo = float(_wire_get(s, "offset_s", 0.0) or 0.0)
        if lo <= h_off + 1e-6 and _ramp_fires_after(rd, lo, h_off):
            return True
    return False


_ANCHOR_ON_AIR = ("start", "hold", "enter", "step")   # anchors whose edges live in on-air-offset space


def _resolve_step_clocked(items, h_off: Optional[float]) -> Dict[int, Tuple[str, float]]:
    """Resolve every step-anchored item to a (clock, offset) — clock 'start' = ON-AIR, 'stop' =
    OFF-AIR — by hanging it off its target step's referenced edge (+ its own offset). A chain
    inherits its ROOT's clock: a dependent whose chain roots at on-air is placed relative to
    on-air; one rooted at off-air (a stop-anchored step, or a bar's off-air stop edge) relative
    to off-air (its absolute time isn't known until arm, but its OFF-AIR-relative position is).

    Resolution is TOPOLOGICAL (mirrors the agent's `_resolve_steps`): a target may itself be
    step-anchored, so chains resolve. A cyclic/unknown target is left UNRESOLVED and omitted."""
    by_id = {getattr(it, "step_id", "") or "": it for it in items if getattr(it, "step_id", "")}

    def own_base(it, seen: set) -> Optional[Tuple[str, float]]:
        """(clock, offset) of the item's OWN start edge. A bar hangs off `start_anchor`, a run
        off `anchor`; a stop anchor roots off-air; both stays unresolved (window-filling)."""
        if getattr(it, "kind", None) == "bar":
            sa = getattr(it, "start_anchor", "start")
            if sa == "start":
                return ("start", float(getattr(it, "start_offset", 0.0)))
            if sa == "hold":
                return ("start", (h_off or 0.0) + float(getattr(it, "start_offset", 0.0)))
            if sa == "step":
                return resolve_base(it, seen)
            return None
        anchor = getattr(it, "anchor", "start")
        if anchor == "start":
            return ("start", float(getattr(it, "offset", 0.0)))
        if anchor == "stop":
            return ("stop", float(getattr(it, "offset", 0.0)))
        if anchor == "hold":
            return ("start", (h_off or 0.0) + float(getattr(it, "offset", 0.0)))
        if anchor == "step":
            return resolve_base(it, seen)
        return None                                   # both — window-filling, not a point edge

    def edge_offset(it, edge: str, seen: set) -> Optional[Tuple[str, float]]:
        # A bar's END edge is its OFF-AIR stop (relative to off-air), not the start clock.
        if getattr(it, "kind", None) == "bar" and edge == "end":
            return ("stop", float(getattr(it, "stop_offset", 0.0)))
        cb = own_base(it, seen)
        if cb is None:
            return None
        clock, base = cb
        if edge == "end" and _is_ramp(it):
            base += _ramp_duration(dict(getattr(it, "ramp", None) or {}))
        return (clock, base)                          # point: start == end; ramp/bar start edge

    def resolve_base(it, seen: set) -> Optional[Tuple[str, float]]:
        uid = getattr(it, "uid", None)
        if uid in seen:
            return None                               # cycle
        # A bar SOURCE hangs its start off a step; a run its start too — except a ramp tied by its
        # END, whose start sits `duration` before the tied point (step_wire_offset folds that in).
        ref, edge = step_source_ref(it)
        own = step_wire_offset(it)
        tgt = by_id.get(ref)
        if tgt is None:
            return None                               # unknown target
        e = edge_offset(tgt, edge, seen | {uid})
        if e is None:
            return None
        return (e[0], e[1] + own)

    out: Dict[int, Tuple[str, float]] = {}
    for it in items:
        if is_step_source(it):
            cb = resolve_base(it, set())
            if cb is not None:
                out[getattr(it, "uid", None)] = cb
    return out


def resolve_step_offsets(items, h_off: Optional[float]) -> Dict[int, float]:
    """{uid: on-air base offset} for every step-anchored item whose chain roots at ON-AIR
    (a point's start==end==offset; a ramp's end == start+duration). Off-air-rooted chains and
    cyclic/unknown targets are omitted (see resolve_step_offsets_off / the agent backstop).
    Kept as plain floats — the shape most consumers (ordering, in-task checks) rely on."""
    return {uid: off for uid, (clock, off) in _resolve_step_clocked(items, h_off).items()
            if clock == "start"}


def resolve_step_offsets_off(items, h_off: Optional[float]) -> Dict[int, float]:
    """{uid: off-air base offset} for every step-anchored item whose chain roots at OFF-AIR
    (anchored to a stop step, a bar's off-air stop edge, or a chain that reaches one). The
    offset is relative to off-air; its absolute time is set at arm."""
    return {uid: off for uid, (clock, off) in _resolve_step_clocked(items, h_off).items()
            if clock == "stop"}


def _reaches(src_uid, target_it, by_sid, seen=None) -> bool:
    """True if following anchor edges FROM target_it reaches the item with uid src_uid —
    i.e. anchoring src → target would close a cycle. Follows a run's `anchor_step_id` and a
    bar's `start_anchor_step_id` (a bar hangs off a step via its START)."""
    seen = seen or set()
    uid = getattr(target_it, "uid", None)
    if uid in seen:
        return False
    seen.add(uid)
    if uid == src_uid:
        return True
    if getattr(target_it, "kind", None) == "bar":
        if getattr(target_it, "start_anchor", "start") != "step":
            return False
        nxt = by_sid.get(getattr(target_it, "start_anchor_step_id", "") or "")
    else:
        if getattr(target_it, "anchor", "start") != "step":
            return False
        nxt = by_sid.get(getattr(target_it, "anchor_step_id", "") or "")
    return nxt is not None and _reaches(src_uid, nxt, by_sid, seen)


def _target_label(it) -> str:
    """A short human label for a step-anchor target (its kind + task)."""
    act = getattr(it, "action", "run")
    name = {"tune": "Tune", "ramp": "Ramp", "run": "Run"}.get(act, act.capitalize())
    task = getattr(it, "task_name", "") or "?"
    return f"{name} · {task}"


def step_anchor_fault(item, by_sid: Dict[str, object]) -> Optional[str]:
    """Diagnose WHY a step-anchored `item` won't resolve on the on-air clock — a specific,
    actionable message naming the offending step and reason, so a save that fails
    validation tells the operator what to fix (instead of a catch-all "cycle or can't be
    timed"). Walks the single anchor chain up to its root; returns None if it resolves.

    `by_sid` maps step_id → item (for every item that carries a step_id)."""
    label = _target_label(item)
    seen = set()
    cur = item
    while getattr(cur, "anchor", "start") == "step":
        uid = getattr(cur, "uid", None)
        if uid in seen:
            return (f"{label} is part of a step-anchor loop (a step ends up anchored back "
                    f"to itself) — re-anchor one of the steps to break the cycle")
        seen.add(uid)
        nxt = by_sid.get(getattr(cur, "anchor_step_id", "") or "")
        if nxt is None:
            return f"{label} anchors to a step that no longer exists — pick a new target"
        cur = nxt
    # `cur` is the chain's ultimate root. An off-air (stop) root is fine — it resolves on the
    # off-air clock (set at arm). Only a window-filling ('both') ramp has no single edge to hang
    # off, so a chain rooted there can't be placed.
    if getattr(cur, "anchor", "start") == "both":
        return (f"{label} anchors to a window-filling ramp ({_target_label(cur)}), which has no "
                f"single edge to hang off — anchor it to a point/edge step instead")
    return None


def eligible_step_targets(items, source_uid) -> List:
    """The items `source_uid` may anchor to (anchor="step") without forming a cycle: run/tune/ramp
    points+ramps AND duration tasks (a bar — its on-air start edge or its off-air stop edge),
    on EITHER clock (an off-air-anchored step is a valid target now; the Hold and a window-filling
    'both' ramp are not). Excludes the source itself and anything that already (transitively)
    anchors back to it. Order mirrors the timeline."""
    by_sid = {getattr(it, "step_id", "") or "": it for it in items if getattr(it, "step_id", "")}
    out: List = []
    for it in items:
        if getattr(it, "uid", None) == source_uid or _is_hold(it):
            continue
        if getattr(it, "kind", None) == "bar":
            if not _reaches(source_uid, it, by_sid):
                out.append(it)
            continue
        if getattr(it, "action", "run") not in ("run", "tune", "ramp"):
            continue
        if getattr(it, "anchor", "start") == "both":   # window-filling — no single point edge
            continue
        if _reaches(source_uid, it, by_sid):
            continue
        out.append(it)
    return out


def step_targets_for_edit(items, item) -> List:
    """The step-anchor targets to OFFER when EDITING `item`: the eligible on-air targets
    (`eligible_step_targets`), PLUS — when `item` is already step-anchored to a target that
    is no longer eligible (it was edited to off-air / into a bar, or now looks like a cycle) —
    that stored target itself, so re-editing the step never SILENTLY re-points it to a
    different one. The stored target is only added when it still exists as an item; a target
    that genuinely can't be timed is left for `validate()`/the agent to reject, with the
    anchor the operator chose intact (not swapped out behind their back)."""
    targets = list(eligible_step_targets(items, getattr(item, "uid", None)))
    if getattr(item, "anchor", "start") == "step":
        want = getattr(item, "anchor_step_id", "") or ""
        if want and not any((getattr(t, "step_id", "") or "") == want for t in targets):
            stored = next(
                (o for o in items
                 if (getattr(o, "step_id", "") or "") == want
                 and getattr(o, "uid", None) != getattr(item, "uid", None)), None)
            if stored is not None:
                targets.append(stored)
    return targets


def step_edge_offset(items, target_step_id: str, edge: str,
                     h_off: Optional[float]) -> Optional[float]:
    """On-air offset of the step `target_step_id`'s start/end edge (None if the target is
    unknown or its edge isn't on the on-air clock — a bar's off-air stop, or a stop/both step).
    Reuses the shared topological resolution (`resolve_step_offsets` + `_item_edge_offset`) so
    bar targets resolve the same way everywhere."""
    tgt = next((it for it in items if (getattr(it, "step_id", "") or "") == (target_step_id or "")
                and target_step_id), None)
    if tgt is None:
        return None
    return _item_edge_offset(items, tgt, edge or "end", h_off,
                             resolve_step_offsets(items, h_off))


def ensure_step_id(it) -> str:
    """Assign (once) and return a stable cross-reference id for `it` so another step can
    anchor to it. Idempotent — a loaded item keeps its wire id."""
    import uuid
    sid = getattr(it, "step_id", "") or ""
    if not sid:
        sid = "st-" + uuid.uuid4().hex[:8]
        try:
            it.step_id = sid
        except Exception:  # noqa: BLE001 — a read-only/namespace item can't carry one
            return ""
    return sid


def _by_uid(items, uid):
    for it in items:
        if getattr(it, "uid", None) == uid:
            return it
    return None


def _item_edge_clocked(items, it, edge: str, h_off: Optional[float],
                       step_bases: Optional[Dict[int, float]] = None,
                       off_bases: Optional[Dict[int, float]] = None) -> Optional[Tuple[str, float]]:
    """(clock, offset) of `it`'s start/end edge for GEOMETRY — clock 'start' = on-air, 'stop' =
    off-air. A bar's END edge is its OFF-AIR stop; a ramp's END edge is its full duration (last
    fire + the final level's hold). None for a window-filling ('both') / orphaned edge."""
    if getattr(it, "kind", None) == "bar":
        if edge == "end":
            return ("stop", float(getattr(it, "stop_offset", 0.0)))
        return bar_start_placement(it, h_off, step_bases, off_bases)     # its start edge
    a, base = effective_anchor_offset(it, h_off, step_bases, off_bases)
    if a not in ("start", "stop"):
        return None                                   # 'both' — no single point edge here
    if edge == "end" and _is_ramp(it):
        base += _ramp_duration(dict(getattr(it, "ramp", None) or {}))
    return (a, base)


def _item_edge_offset(items, it, edge: str, h_off: Optional[float],
                      step_bases: Optional[Dict[int, float]] = None) -> Optional[float]:
    """ON-AIR offset of `it`'s edge (None if the edge isn't on the on-air clock). A thin
    on-air-only view of `_item_edge_clocked`, kept for callers that only place on-air."""
    ce = _item_edge_clocked(items, it, edge, h_off, step_bases)
    return ce[1] if ce is not None and ce[0] == "start" else None


def step_drop_offset(items, source_uid, target_uid, edge: str,
                     h_off: Optional[float],
                     step_bases: Optional[Dict[int, float]] = None) -> Optional[float]:
    """The offset that anchors `source_uid`'s start to `target_uid`'s `edge` while keeping the
    source visually where it already sits — i.e. the gap between the source's current start and
    the target edge, snapped. The offset may be NEGATIVE (the source sits before the edge — like
    a start/stop anchor's lead-in); the agent accepts that from 1.25.0, and the save/arm gate on
    `sequence-step-anchor-negative` keeps it off an older unit. None when the drop is invalid: the
    target isn't an eligible (cycle-safe, on-air) target for the source, or its edge isn't
    on the on-air clock. Pure — the caller assigns the ids and records the anchor."""
    src = _by_uid(items, source_uid)
    tgt = _by_uid(items, target_uid)
    if src is None or tgt is None or src is tgt:
        return None
    if tgt not in eligible_step_targets(items, source_uid):
        return None
    tgt_edge = _item_edge_offset(items, tgt, edge, h_off, step_bases)
    if tgt_edge is None:
        return None
    src_base = _item_edge_offset(items, src, "start", h_off, step_bases)
    if src_base is None:                      # a stop/both-anchored source snaps to the edge
        return 0.0
    return _snap(src_base - tgt_edge)         # may be negative — the source keeps its place


def bar_start_placement(item, h_off: Optional[float],
                        step_bases: Optional[Dict[int, float]] = None,
                        off_bases: Optional[Dict[int, float]] = None) -> Tuple[str, float]:
    """(anchor, offset) for drawing a duration bar's START handle. A window-B bar
    (`start_anchor="hold"`) places its start at the Hold divider (`hold_offset + start_offset`);
    a STEP-anchored bar (`start_anchor="step"`) places it at its resolved base — on-air
    (`step_bases[uid]`) or, when its chain roots off-air, off-air (`off_bases[uid]`); a normal
    bar places it on-air at its own offset. Drawing only — the stored bar keeps its round-trip
    fields."""
    sa = getattr(item, "start_anchor", "start")
    if sa == "hold" and h_off is not None:
        return "start", (h_off or 0.0) + float(getattr(item, "start_offset", 0.0))
    if sa == "step":
        uid = getattr(item, "uid", None)
        base = (step_bases or {}).get(uid)
        if base is not None:
            return "start", base
        obase = (off_bases or {}).get(uid)
        if obase is not None:
            return "stop", obase
        return "start", float(getattr(item, "start_offset", 0.0))
    return "start", float(getattr(item, "start_offset", 0.0))


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

    h_off = hold_offset(items)
    step_bases = resolve_step_offsets(items, h_off)
    for it in items:
        if it.kind == "bar":
            take(*bar_start_placement(it, h_off, step_bases))   # honours a step-anchored start
            take("stop", it.stop_offset)
        elif _is_ramp(it):
            (la, lo), (ra, ro) = ramp_span(it, h_off, step_bases)
            take(la, lo)
            take(ra, ro)
        else:  # run/tune/hold — map a window-B/step-anchored item to the on-air side
            a, o = effective_anchor_offset(it, h_off, step_bases)
            take(a, o)
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
    pv = getattr(it, "power_view", None)
    hd = getattr(it, "power_hold_dest", None)
    # Step-to-step anchoring fields, emitted only when present so a plain sequence's wire
    # shape is byte-identical (a bar/hold is never a step anchor source in Phase 1).
    sid = getattr(it, "step_id", "") or ""
    asid = getattr(it, "anchor_step_id", "") or ""
    aedge = getattr(it, "anchor_edge", "end") or "end"

    def _sa(step: dict) -> dict:
        if sid:
            step["id"] = sid
        if getattr(it, "anchor", "start") == "step":
            step["anchor_step_id"] = asid
            step["anchor_edge"] = aedge
            # A ramp tied by its END: the wire offset is the START's (offset − duration) — the
            # agent runs the ramp forward from there unchanged; the tie is carried as metadata so
            # the client redraws/edits it by its end (emitted only when set, plain wire unchanged).
            if (getattr(it, "anchor_own_edge", "start") or "start") == "end" and _is_ramp(it):
                step["offset_s"] = step_wire_offset(it)
                step["anchor_own_edge"] = "end"
        return step

    if it.kind == "bar":
        # A duration task flattens to a START step (on-air, or window-B via the Hold, or hung off
        # another step) + a STOP step (off-air). When the bar is an anchor TARGET (it has a
        # step_id) BOTH wire steps carry an id — the start step the bar's id, the stop step
        # id+suffix — so a dependent can anchor to either edge. When it is an anchor SOURCE
        # (start_anchor="step") the START step carries the anchor fields (its STOP stays off-air).
        start_anchor = getattr(it, "start_anchor", "start")
        start: dict = {
            "anchor": start_anchor, "offset_s": it.start_offset, "action": "start",
            "task_name": it.task_name, "args": list(it.args), "replace_args": it.replace_args,
            "inject_resume_offset": bool(getattr(it, "inject_resume_offset", False)),
            "power_view": pv, "power_hold_dest": hd}
        stop: dict = {"anchor": "stop", "offset_s": it.stop_offset, "action": "stop",
                      "task_name": it.task_name, "args": [], "replace_args": False}
        if sid:
            start["id"] = sid
            stop["id"] = sid + BAR_STOP_SUFFIX
        if start_anchor == "step":
            start["anchor_step_id"] = getattr(it, "start_anchor_step_id", "") or ""
            start["anchor_edge"] = getattr(it, "start_anchor_edge", "end") or "end"
        return [start, stop]
    if getattr(it, "action", "run") == "hold":
        # The Hold boundary marker (docs/sequence-hold-step.md): anchor="start" at the
        # end of window A. It names no task and carries no work — just a divider between
        # window A and window B (anchor="hold" steps). Phase 0 round-trips it; the canvas
        # doesn't render it yet (Phase 2).
        return [
            {"anchor": "start", "offset_s": it.offset, "action": "hold",
             "task_name": getattr(it, "task_name", "") or "",
             "args": [], "replace_args": False},
        ]
    if getattr(it, "action", "run") == "tune":
        return [_sa(
            {"anchor": it.anchor, "offset_s": it.offset, "action": "tune",
             "task_name": it.task_name, "params": dict(it.params or {}),
             "power_view": pv, "power_hold_dest": hd}),
        ]
    if getattr(it, "action", "run") == "ramp":
        # A run-mode ramp carries the OTHER params' fixed values as args; a tune ramp
        # has none. replace_args mirrors a one-shot (the args are the complete set).
        return [_sa(
            {"anchor": it.anchor, "offset_s": it.offset, "action": "ramp",
             "offset_end_s": getattr(it, "offset_end", 0.0),
             "task_name": it.task_name, "ramp": dict(it.ramp or {}),
             "args": list(getattr(it, "args", []) or []),
             "replace_args": bool(getattr(it, "replace_args", True)),
             "power_view": pv, "power_hold_dest": hd}),
        ]
    return [_sa(
        {"anchor": it.anchor, "offset_s": it.offset, "action": "run",
         "task_name": it.task_name, "args": list(it.args), "replace_args": it.replace_args,
         "power_view": pv, "power_hold_dest": hd}),
    ]


def _bar_step_ids(items) -> set:
    """The step_ids of every bar (duration task) that is an anchor target — so a dependent's
    reference to a bar's OFF-AIR stop edge can be re-pointed to the bar's stop wire step."""
    return {getattr(it, "step_id", "") or "" for it in items
            if getattr(it, "kind", None) == "bar" and getattr(it, "step_id", "")}


def _encode_anchor_ref(asid: str, aedge: str, bar_ids: set) -> Tuple[str, str]:
    """Wire (anchor_step_id, anchor_edge) for an item that anchors to `asid`'s `aedge`. A bar
    target has two wire steps: the START step (id = the bar id) and the STOP step (id + suffix),
    each a single fire — so a dependent on the bar's END edge points to the stop step (edge
    'start'), and one on the START edge points to the start step. Non-bar targets pass through."""
    if asid in bar_ids:
        return (asid + BAR_STOP_SUFFIX, "start") if aedge == "end" else (asid, "start")
    return asid, aedge


def _decode_anchor_ref(asid: str, aedge: str) -> Tuple[str, str]:
    """Item (anchor_step_id, anchor_edge) from a wire ref — the inverse of _encode_anchor_ref: a
    ref to a bar's stop wire step (id + suffix) maps back to the bar id + the 'end' edge."""
    if asid.endswith(BAR_STOP_SUFFIX):
        return asid[: -len(BAR_STOP_SUFFIX)], "end"
    return asid, aedge


def items_to_steps(items) -> List[dict]:
    bar_ids = _bar_step_ids(items)
    out: List[dict] = []
    for it in items:
        for step in item_to_steps(it):
            # Re-point a reference to a bar TARGET at the correct wire step (its start or stop).
            ref = step.get("anchor_step_id")
            if ref:
                step["anchor_step_id"], step["anchor_edge"] = _encode_anchor_ref(
                    ref, step.get("anchor_edge", "end") or "end", bar_ids)
            out.append(step)
    return out


def _step_anchor_fields(s: dict) -> dict:
    """The step-to-step anchoring fields carried from a wire step onto a RunItem (blank when
    absent, so a plain step round-trips unchanged). A reference to a bar's stop wire step
    (id + suffix) decodes back to the bar id + the 'end' edge."""
    asid, aedge = _decode_anchor_ref(str(s.get("anchor_step_id") or ""),
                                     str(s.get("anchor_edge") or "end"))
    own = "end" if str(s.get("anchor_own_edge") or "start") == "end" else "start"
    return {"step_id": str(s.get("id") or ""), "anchor_step_id": asid, "anchor_edge": aedge,
            "anchor_own_edge": own}


def _ramp_item_offset(s: dict) -> float:
    """A ramp wire step's item `offset`: the START's offset (`offset_s`), or — for a ramp tied by
    its END — the end's offset (`offset_s` + duration), which is what the item stores/edits."""
    off = float(s["offset_s"])
    if str(s.get("anchor") or "start") == "step" and str(s.get("anchor_own_edge") or "") == "end":
        off += _ramp_duration(dict(s.get("ramp") or {}))
    return off


def steps_to_items(steps: List[dict]) -> List:
    """Group a flat step list back into bars + run points. Runs map 1:1; each
    start is paired with a stop of the same task (in order) to form a bar."""
    items: List = []
    starts: List[dict] = []
    stops_by_task: Dict[str, List[dict]] = defaultdict(list)

    for s in steps:
        action = _action_of(s)
        if action == "hold":
            # The Hold boundary marker → a minimal RunItem(action="hold"). No task,
            # no params, no power. Kept out of the start/stop bar pairing below.
            items.append(RunItem(
                task_name=s.get("task_name") or "", action="hold",
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"])))
        elif action == "run":
            items.append(RunItem(
                task_name=s["task_name"], args=list(s.get("args") or []),
                replace_args=bool(s.get("replace_args", True)),
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"]),
                power_view=s.get("power_view"), **_step_anchor_fields(s)))
        elif action == "tune":
            items.append(RunItem(
                task_name=s["task_name"], action="tune",
                params=dict(s.get("params") or {}),
                anchor=s.get("anchor", "start"), offset=float(s["offset_s"]),
                power_view=s.get("power_view"), **_step_anchor_fields(s)))
        elif action == "ramp":
            items.append(RunItem(
                task_name=s["task_name"], action="ramp",
                ramp=dict(s.get("ramp") or {}),
                args=list(s.get("args") or []),
                replace_args=bool(s.get("replace_args", True)),
                anchor=s.get("anchor", "start"), offset=_ramp_item_offset(s),
                offset_end=float(s.get("offset_end_s") or 0.0),
                power_view=s.get("power_view"), **_step_anchor_fields(s)))
        elif action == "start":
            starts.append(s)
        elif action == "stop":
            stops_by_task[s["task_name"]].append(s)

    for st in starts:
        task = st["task_name"]
        rem = stops_by_task.get(task) or []
        stop = rem.pop(0) if rem else None
        # The bar's cross-reference id comes from its START step's id; its start may hang off
        # another step (anchor="step" → start_anchor + start_anchor_step_id/edge, bar-ref decoded).
        sanc = st.get("anchor", "start")
        sasid, saedge = _decode_anchor_ref(str(st.get("anchor_step_id") or ""),
                                           str(st.get("anchor_edge") or "end"))
        items.append(BarItem(
            task_name=task, args=list(st.get("args") or []),
            replace_args=bool(st.get("replace_args", True)),
            start_offset=float(st["offset_s"]),
            stop_offset=float(stop["offset_s"]) if stop else 0.0,
            start_anchor=sanc,                        # "hold" → window-B; "step" → hung off a step
            step_id=str(st.get("id") or ""),
            start_anchor_step_id=sasid, start_anchor_edge=saedge,
            inject_resume_offset=bool(st.get("inject_resume_offset", False)),
            power_view=st.get("power_view")))

    # A stop with no matching start → a bar whose start sits at on-air (0s).
    for task, rem in stops_by_task.items():
        for stop in rem:
            items.append(BarItem(task_name=task, args=[], start_offset=0.0,
                                 stop_offset=float(stop["offset_s"])))
    return items


# ── Validation (mirrors the agent's _validate_steps) ─────────────────────────

# ── Step-conflict validation (a tune/ramp can't step outside its task, and two steps
#    can't fight over the same live control at the same time) ─────────────────────────

# --power (calibrated) and --gain (raw SDR gain) are two ways to drive the SAME output level,
# so a tune/ramp on either conflicts with a tune/ramp on the other.
_LEVEL_DESTS = frozenset({"power", "gain"})


def _control_key(dest: str) -> str:
    """Normalise a parameter dest to its CONTROL identity: power and gain both drive the one
    output level, so they share a key; every other parameter is its own key."""
    return "level" if dest in _LEVEL_DESTS else dest


def _controlled_keys(it) -> set:
    """The normalised control keys a tune/ramp step SETS — a tune's changed params, or a ramp's
    swept param. Empty for anything else (a bar / one-shot / Hold)."""
    act = getattr(it, "action", "run")
    if act == "tune":
        return {_control_key(str(d)) for d in (getattr(it, "params", None) or {})}
    if act == "ramp":
        p = (getattr(it, "ramp", None) or {}).get("param")
        return {_control_key(str(p))} if p else set()
    return set()


def _step_time_span(it, h_off, step_bases):
    """A tune/ramp's fire interval as (clock, lo, hi) on a comparable axis, or None when it
    isn't time-comparable at authoring (an unresolved step anchor). `clock` is "on" (the on-air
    clock — start/hold/step anchors), "off" (the off-air clock — a stop anchor; its offset is
    ≤ 0), or "both" (a window-filling ramp, which overlaps everything). A tune is a point
    (lo == hi); a ramp spans its duration from the anchored edge. Cross-clock intervals ("on"
    vs "off") aren't comparable until the window length is fixed at arm, so they never overlap
    here (the agent stays the backstop)."""
    anchor = getattr(it, "anchor", "start")
    if _is_ramp(it):
        if anchor == "both":
            return ("both", 0.0, 0.0)
        dur = _ramp_duration(dict(getattr(it, "ramp", None) or {}))
        if anchor == "stop":
            off = float(getattr(it, "offset", 0.0))
            return ("off", off - dur, off)
        a, base = effective_anchor_offset(it, h_off, step_bases)
        if a != "start":
            return None
        return ("on", base, base + dur)
    # a point (tune / run)
    if anchor == "stop":
        off = float(getattr(it, "offset", 0.0))
        return ("off", off, off)
    a, base = effective_anchor_offset(it, h_off, step_bases)
    if a != "start":
        return None
    return ("on", base, base)


def _spans_overlap(s1, s2, tol: float = 1e-6) -> bool:
    """True if two `_step_time_span` intervals overlap in time. A window-filling ("both") ramp
    overlaps everything; two intervals on different clocks (on vs off) are not comparable at
    authoring, so they don't."""
    c1, lo1, hi1 = s1
    c2, lo2, hi2 = s2
    if c1 == "both" or c2 == "both":
        return True
    if c1 != c2:
        return False
    pt1 = abs(hi1 - lo1) <= tol
    pt2 = abs(hi2 - lo2) <= tol
    if pt1 and pt2:
        return abs(lo1 - lo2) <= tol                    # two points: the same instant
    # A ramp occupies [lo, hi): its last level's hold ENDS at hi, so a step firing exactly at hi
    # comes AFTER it (a ramp chained onto another's end, or a crossing ramp's resumed remainder
    # right after its run-up) — while a step at its START instant still collides.
    if pt1:
        return lo2 - tol <= lo1 < hi2 - tol
    if pt2:
        return lo1 - tol <= lo2 < hi1 - tol
    return lo1 < hi2 - tol and lo2 < hi1 - tol


def _step_conflict_error(items) -> Optional[str]:
    """Error string if any tune/ramp fires outside its parent duration task, if a task is
    started more than once, or if two steps drive the SAME live control at the SAME time
    (a tune vs tune, tune vs ramp, or ramp vs ramp on the same task / output level). None if
    the set is conflict-free. Off-air / hold / step-anchored timing that can't be resolved on
    a single clock at authoring is left to the agent's runtime validation."""
    h_off = hold_offset(items)
    step_bases = resolve_step_offsets(items, h_off)

    # Rule E — a task can be started at most once (you can't run one task twice at a time;
    # its bars would share the on-air window). The off-air time isn't known until arm, so
    # "overlap" can't be computed here — a second bar for a task is refused outright.
    bars_by_task: Dict[str, list] = {}
    for it in items:
        if getattr(it, "kind", None) == "bar":
            bars_by_task.setdefault(it.task_name or "", []).append(it)
    for task, bars in bars_by_task.items():
        if len(bars) > 1:
            return (f"'{task}' is started {len(bars)} times — a duration task can run only once "
                    f"per sequence; remove the extra duration step (retune it with a tune/ramp "
                    f"instead of restarting it)")

    # Rule A — a tune/ramp must fire inside its parent task's on-air span (start/stop/both
    # anchors; a hold-anchored step is timed at proceed, checked elsewhere).
    for it in items:
        act = getattr(it, "action", "run")
        if act not in ("tune", "ramp"):
            continue
        anchor = getattr(it, "anchor", "start")
        spans = [(float(b.start_offset), float(b.stop_offset))
                 for b in bars_by_task.get(it.task_name or "", [])]
        if not spans:
            continue   # the "targets a task with a duration step" check already covers this
        if anchor in ("start", "stop", "both"):
            err = step_within_task_error(spans, anchor, float(getattr(it, "offset", 0.0)),
                                         float(getattr(it, "offset_end", 0.0)), kind=act)
            if err:
                return f"{_target_label(it)}: {err}"
        elif anchor == "step":
            # A step-anchored tune/ramp fires at its RESOLVED on-air time (via the anchor chain).
            # If that lands BEFORE its own task goes on air it's invalid — e.g. dragging its target
            # so the dependent falls before the task's start (the owner-reported case). Only the
            # lower (on-air) bound is checkable at authoring — off-air floats until arm — and only
            # when the chain resolves on the on-air clock (an off-air-rooted chain is the agent's).
            base = step_bases.get(getattr(it, "uid", None))
            if base is None:
                continue
            s0 = min(s for s, _ in spans)
            if base < s0 - 1e-6:
                from .param_form import fmt_duration
                return (f"{_target_label(it)}: anchored to another step, this {act} resolves to "
                        f"{fmt_duration(base, signed=True)} from on-air — before '{it.task_name}' "
                        f"goes on air; it must fire at or after on-air "
                        f"({fmt_duration(s0, signed=True)})")

    # Rules B / C / D — two steps can't drive the same control at the same time. Group tune/ramp
    # steps by task, then any pair whose control keys intersect AND whose time spans overlap is a
    # conflict (tune·tune at one instant, a tune inside a ramp's sweep, or two overlapping ramps).
    by_task: Dict[str, list] = {}
    for it in items:
        if getattr(it, "action", "run") not in ("tune", "ramp"):
            continue
        keys = _controlled_keys(it)
        span = _step_time_span(it, h_off, step_bases)
        if not keys or span is None:
            continue
        by_task.setdefault(it.task_name or "", []).append((it, keys, span))
    for task, entries in by_task.items():
        for i in range(len(entries)):
            it_a, keys_a, span_a = entries[i]
            for j in range(i + 1, len(entries)):
                it_b, keys_b, span_b = entries[j]
                shared = keys_a & keys_b
                if not shared or not _spans_overlap(span_a, span_b):
                    continue
                what = "the output level" if shared == {"level"} else \
                    "'" + "', '".join(sorted(shared)) + "'"
                return (f"{_target_label(it_a)} and {_target_label(it_b)} both set {what} at "
                        f"the same time on '{task}' — two steps can't drive one control at once; "
                        f"move one, or remove it")
    return None


def validate(items, known_tasks: Optional[List[str]] = None) -> Optional[str]:
    """Return an error string if the item set wouldn't make a valid sequence."""
    if not items:
        return "add at least one duration or one-shot task"
    steps = items_to_steps(items)
    # The Hold marker is a boundary — it names no task and defines no work, so it is
    # excluded from the task checks (docs/sequence-hold-step.md §5.1).
    task_steps = [s for s in steps if _action_of(s) != "hold"]
    if any(not s["task_name"] for s in task_steps):
        return "every step needs a task"
    if known_tasks:
        unknown = sorted({s["task_name"] for s in task_steps if s["task_name"] not in known_tasks})
        if unknown:
            return "unknown task(s): " + ", ".join(unknown)
    # A Hold is anchor="start" but not a real on-air step; require a genuine one.
    if not any(s["anchor"] == "start" and _action_of(s) != "hold" for s in steps):
        return "needs at least one on-air step"
    if not any(s["anchor"] == "stop" for s in steps):
        return "needs at least one off-air step (a duration task provides both)"
    # A window-B (anchor="hold") step needs a Hold marker to anchor to — otherwise it's
    # orphaned (e.g. the Hold was removed but its post-hold steps left behind), which the
    # geometry/walk mis-place (mirrors the agent's _validate_steps).
    if not has_hold(items) and any(s["anchor"] == "hold" for s in steps):
        return "a post-hold (‘hold’-anchored) step needs a Hold — add one or re-anchor the step"
    # A step anchored to the Hold's START (anchor="enter" — it ends/fires at or before the pause)
    # likewise needs the Hold, and can't reach INTO the pause (the run is holding then).
    if not has_hold(items) and any(s["anchor"] == "enter" for s in steps):
        return "a step anchored to the Hold's start needs a Hold — add one or re-anchor the step"
    if any(s["anchor"] == "enter" and float(s["offset_s"]) > 1e-9 for s in steps):
        return ("a step anchored to the Hold's start must end at or before the pause (its "
                "offset can't be positive)")
    # ── Step-to-step anchoring (mirrors the agent's _validate_steps) ──────────────────────
    step_items = [it for it in items if is_step_source(it)]
    if step_items:
        if has_hold(items):
            return ("step-to-step anchoring isn't supported in a sequence with a Hold yet — "
                    "anchor to on-air/off-air/Hold instead")
        by_sid = {getattr(it, "step_id", "") or "": it for it in items if getattr(it, "step_id", "")}
        for it in step_items:
            tgt_id, aedge = step_source_ref(it)
            if not tgt_id:
                return f"a step anchored to another step needs a target (on '{it.task_name}')"
            if aedge not in ("start", "end"):
                return "a step anchor's edge must be ‘start’ or ‘end’"
            # A negative offset is allowed (fire BEFORE the target's edge — like a start/stop anchor's
            # lead-in); the agent accepts it from 1.25.0. Saving/arming with a negative step offset is
            # gated on `sequence-step-anchor-negative` in sequence_editor/sequences_panel, not here.
            if tgt_id == (getattr(it, "step_id", "") or ""):
                return f"a step can't anchor to itself (on '{it.task_name}')"
            if tgt_id not in by_sid:
                return f"a step on '{it.task_name}' anchors to a step that no longer exists"
        # Resolution catches cycles and a chain rooted at a window-filling ('both') ramp (no single
        # edge). A chain that resolves on EITHER clock — on-air or off-air (set at arm) — is valid.
        bases = resolve_step_offsets(items, None)
        off = resolve_step_offsets_off(items, None)
        for it in step_items:
            uid = getattr(it, "uid", None)
            if uid not in bases and uid not in off:
                return (step_anchor_fault(it, by_sid)
                        or "a step anchors to one that can't be timed — re-anchor it")
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
    # A step must fire inside its parent task, a task starts once, and two steps can't drive the
    # same live control at the same time (tune·tune / tune·ramp / ramp·ramp on one output level).
    conflict = _step_conflict_error(items)
    if conflict:
        return conflict
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


def carry_order_key(anchor: str, offset: float, h_off: Optional[float]) -> Tuple[int, float]:
    """Fire-order key for carrying parameter state forward, HOLD-boundary aware. Three phases
    so window B is replayed after window A: window A start-anchored work (phase 0, by on-air
    offset), then window B — a hold-anchored step (phase 1, at ``hold_offset + its offset``,
    so it inherits the operating point held across the hold) then stop/off-air work (phase 2).
    ``h_off`` is the timeline's hold offset (None when there's no Hold — the pre-hold behaviour,
    with stop-anchored steps still after start ones)."""
    if anchor == "hold" and h_off is not None:
        return (1, h_off + offset)
    if anchor == "enter" and h_off is not None:     # before the pause: window A, on-air clock
        return (0, h_off + offset)
    if anchor == "stop":
        return (2, offset)
    return (0, offset)


def _carry_order_key(it, h_off: Optional[float] = None,
                     step_bases: Optional[Dict[int, float]] = None) -> Tuple[int, float]:
    """``carry_order_key`` for a timeline item. A duration bar starts the task (phase 0 at its
    start offset); a run/tune/ramp keys off its anchor + offset (window-B steps ordered past the
    hold when ``h_off`` is given). A step-anchored item orders at its RESOLVED on-air offset
    (phase 0, like a start-anchored step) so it carries state in its true fire order."""
    if getattr(it, "kind", None) == "bar":
        # A window-B duration task (start_anchor="hold") starts at the resume instant, so it
        # orders in phase 1 like other window-B work; a normal bar is phase-0 on-air baseline.
        if getattr(it, "start_anchor", "start") == "hold" and h_off is not None:
            return (1, h_off + float(getattr(it, "start_offset", 0.0)))
        return (0, float(getattr(it, "start_offset", 0.0)))
    if getattr(it, "anchor", "start") == "step":
        base = (step_bases or {}).get(getattr(it, "uid", None))
        if base is not None:
            return (0, base)
    return carry_order_key(getattr(it, "anchor", "start"),
                           float(getattr(it, "offset", 0.0)), h_off)


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
    h_off = hold_offset(items)                          # place window B after window A
    step_bases = resolve_step_offsets(items, h_off)     # order step-anchored work by fire time
    if target_key is None:
        target_key = (float("inf"), float("inf"))      # no anchor info → replay all priors
    for it in sorted(mine, key=lambda it: _carry_order_key(it, h_off, step_bases)):
        if _carry_order_key(it, h_off, step_bases) >= target_key:
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


def _view_law_of(spec):
    """Parse the resolver-supplied controlled-view law spec (a CAL_POWER_LAWS entry the operator
    authors --power in — e.g. a chirp's ``psd_live`` keyed on --bw) into a ``Law``, or None when
    absent/unparseable. Defensive: a warning helper must never break on a malformed law."""
    if not spec:
        return None
    try:
        from state.power_law import parse_law
        return parse_law(spec)
    except (ValueError, TypeError):
        return None


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
          "view_law":    the DEFAULT controlled-view law spec (a step recording no power_view) — a
                         restates_measurement CAL_POWER_LAWS entry keyed on a live param (a chirp's
                         psd_live on --bw), or None. When present the achievable range is folded in
                         THAT view at each event's fire-time param, so a held/commanded live density
                         is checked at the live sweep width even though the base range is invariant.
          "view_laws":   {view_id: law_spec} for every declared control view, so the walk holds
                         whichever quantity the LATEST power-setting step was authored in (its
                         ``power_view``); the walk keeps only the bw-keyed ones (total power / gain /
                         dBm resolve to base-held). Optional; omit for a single default view.
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
    from state.power_fold import (PowerFold, fold_params_from_values,   # pure (no Qt); lazy
                                  resolve_keyed_values)

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
        if fold is None:
            continue
        # The CONTROLLED-view law: the restates_measurement law the operator authors --power in (e.g.
        # a chirp's live spectral density keyed on --bw). The BASE range is bandwidth-invariant for
        # such a signal, but the controlled view moves with --bw, so a held/commanded live density can
        # become undeliverable at a later sweep width even though the base fold can't see it. The held
        # quantity is the LATEST-SET step's control view (its ``power_view``): ``default_view_law`` is
        # the signal default (a step with no recorded view), ``laws_by_id`` maps a recorded view id to
        # its law, and ``active`` tracks the current one as power-setting steps fire. A view that is
        # NOT bw-keyed (total power, gain, dBm) resolves to None → the base is held (no --bw effect).
        default_view_law = _view_law_of(info.get("view_law"))
        laws_by_id = {vid: _view_law_of(spec)
                      for vid, spec in (info.get("view_laws") or {}).items()}
        laws_by_id = {vid: law for vid, law in laws_by_id.items()
                      if law is not None and law.params()}       # keep only bw-keyed views

        def _law_for_view(pv):
            """The controlled-view law a step's ``power_view`` selects: the signal default when the
            step records no view (None — a legacy step or the default quantity), else the recorded
            view's law, or None when that view isn't a bw-keyed density (→ hold base)."""
            return default_view_law if pv is None else laws_by_id.get(pv)

        active = [default_view_law]                              # the currently-held view law (cell)
        view_moves = bool((default_view_law is not None and default_view_law.params())
                          or laws_by_id)
        # A constant chain never moves under a ramp/retune — its fixed range is enforced by the
        # From/To field; the temporal pass stays out unless something (frequency, a bridge param,
        # or a keyed controlled view) actually moves the achievable power at a point in time.
        if not (fold.freq_dependent or fold.param_dependent or view_moves):
            continue
        freq_param = info.get("freq_param")
        freq_factor = float(info.get("freq_factor") or 1.0)
        base_args = list(info.get("base_args") or [])
        unit = (artifact or {}).get("operating_unit") or "dBm"

        def _view_delta(eval_state) -> float:
            """dB the currently-held controlled view adds over the BASE quantity at ``eval_state``'s
            live bridge params — 0.0 when the held quantity is the base (no view law, or a non-bw
            view), so every non-density path is byte-identical. The chirp's psd_live view keys on
            --bw, so this shift moves with the live sweep width even though the base range does not."""
            law = active[0]
            if law is None:
                return 0.0
            keyed = resolve_keyed_values(specs, eval_state, law.params())
            try:
                return law.delta_db(keyed) if keyed else law.rep_delta_db()
            except (ValueError, TypeError):
                return law.rep_delta_db()

        def _view_value_of(eval_state):
            """The commanded --power (base quantity) expressed in the CONTROLLED view at
            ``eval_state``'s live params — the density the operator actually set/holds. None when
            --power isn't a usable number in the state."""
            p = eval_state.get(power_dest)
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                return None
            return float(p) + _view_delta(eval_state)

        flag_to_dest = {f: (s.get("dest") or s.get("name"))
                        for s in specs for f in (s.get("flags") or [])}
        name_to_dest = {(s.get("name") or s.get("dest")): s.get("dest") for s in specs}

        def _clamp(view_value, eval_state):
            """(direction, bound, freq_hz) if the intended CONTROLLED-view power ``view_value``
            can't be delivered at ``eval_state``'s operating point, else None. The achievable range
            is the base range shifted into the controlled view at the live bridge params
            (``+_view_delta``), so a held/commanded live density is checked at the FIRE-TIME --bw
            even though the base range is bandwidth-invariant. With no view law ``_view_delta`` is 0
            and this is exactly the base-range check (mirrors state.power_fold.clamp_warning, so the
            number matches the caption). ``bound`` is returned in the controlled view."""
            if not isinstance(view_value, (int, float)) or isinstance(view_value, bool):
                return None
            fv = eval_state.get(freq_param) if freq_param else None
            freq_hz = (float(fv) * freq_factor
                       if isinstance(fv, (int, float)) and not isinstance(fv, bool) else None)
            params = fold_params_from_values(artifact, specs, eval_state)
            b = fold.bounds_at(freq_hz, params)
            vd = _view_delta(eval_state)
            lo, hi = b["min_power_dbm"] + vd, b["max_power_dbm"] + vd
            if view_value > hi + tol:
                return ("high", hi, freq_hz)
            if view_value < lo - tol:
                return ("low", lo, freq_hz)
            return None

        # The Hold is a clock-reset boundary (docs/sequence-hold-step.md §6.5): a window-B
        # (anchor="hold") step is placed as if start-anchored at hold_offset + its offset, so
        # it fires AFTER every window-A step and inherits the operating point held at the hold
        # (the up-ramp's final --power + bridge params). Ordering, not absolute wall-clock, is
        # all the temporal walk needs — the held level then seeds window B automatically.
        h_off = hold_offset(items)
        step_bases = resolve_step_offsets(items, h_off)

        # Build fire-time-ordered events for this task.
        events: list = []                        # (fire_s, seq_idx, kind, payload)
        for seq_idx, it in enumerate(items):
            if getattr(it, "task_name", None) != task:
                continue
            act = getattr(it, "action", "run")
            pv = getattr(it, "power_view", None)
            if getattr(it, "kind", None) == "bar":
                events.append((_fire_time_s("start", getattr(it, "start_offset", 0.0), window),
                               seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args),
                                "power_view": pv}))
            elif act == "tune":
                fa, fo = effective_anchor_offset(it, h_off, step_bases)
                events.append((_fire_time_s(fa, fo, window), seq_idx, "tune",
                               {"params": dict(getattr(it, "params", {}) or {}), "power_view": pv}))
            elif act == "run":
                fa, fo = effective_anchor_offset(it, h_off, step_bases)
                events.append((_fire_time_s(fa, fo, window), seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args),
                                "power_view": pv}))
            elif act == "ramp":
                r = dict(getattr(it, "ramp", None) or {})
                rdest = name_to_dest.get(r.get("param")) or flag_to_dest.get(r.get("flag"))
                if rdest != power_dest:
                    continue                     # only a POWER ramp is analysed (see docstring)
                # A window-B/step-anchored ramp resolves/places from its on-air base, so its
                # points order after the steps it follows.
                r_anchor, r_offset = effective_anchor_offset(it, h_off, step_bases)
                try:
                    resolved = _resolve_ramp_points(r, r_anchor, window)
                    fires = _place_ramp_points(r, r_anchor, r_offset, resolved)
                except (ValueError, TypeError):
                    continue
                run_mode = r.get("mode") == "run"
                run_state = _args_to_values(list(getattr(it, "args", []) or []), flag_to_dest) \
                    if run_mode else None
                for i, (fa, foff, val) in enumerate(fires):
                    events.append((_fire_time_s(fa, foff, window), seq_idx, "ramp_point",
                                   {"uid": getattr(it, "uid", seq_idx), "i": i, "n": len(fires),
                                    "val": float(val), "rdest": rdest, "param": r.get("param"),
                                    "run_mode": run_mode, "run_state": run_state,
                                    "power_view": pv}))
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
        # The controlled-view delta a ramp was AUTHORED at (per ramp uid). A ramp is drawn once, at
        # the sweep width carried when it starts, so every point's intended controlled-view value is
        # constant across the ramp; captured at the ramp's first (earliest-firing) point — see the
        # ramp_point branch below.
        ramp_auth_vd: Dict[object, float] = {}
        held_flagged = False           # is the current standing --power already known to clamp?
        # The standing --power expressed in the CONTROLLED view (the density the operator holds),
        # captured at set-time so a LATER --bw change re-checks the intended density, not the base.
        held_view = _view_value_of(state)
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
                # A ramp point COMMANDS --power at its fire moment; the ramp's control view becomes
                # the held quantity. The ramp was AUTHORED once, at the sweep width in effect when it
                # starts (the ramp editor folds its From/To in the controlled view at THAT --bw), so
                # each point's intended controlled-view value (the density the operator drew) is
                # base + view_delta(authoring --bw) — CONSTANT across the ramp, not re-derived at each
                # point's fire-time --bw. Capture the authoring delta at the ramp's first (earliest-
                # firing) point, which fires at the start width, and hold it; then _clamp folds the
                # achievable range at each point's own fire-time --bw, so a mid-ramp --bw change makes
                # the later points clamp (the temporal case, Issue 1). Using the fire-time delta here
                # would cancel against _clamp's own +view_delta shift and never flag anything, because
                # the base range is bandwidth-invariant.
                active[0] = _law_for_view(p.get("power_view"))
                if p["uid"] not in ramp_auth_vd:
                    ramp_auth_vd[p["uid"]] = _view_delta(eval_state)
                dval = float(p["val"]) + ramp_auth_vd[p["uid"]]
                viol = _clamp(dval, eval_state)
                if viol:
                    direction, bound, freq_hz = viol
                    hits[p["uid"]].append((p["i"], dval, _fire_s, direction, bound, freq_hz,
                                           p["n"]))
                    meta[p["uid"]] = {"param": p["param"] or power_dest}
                # keep the held-power state in step with the ramp's last value (its intended
                # controlled-view value at the authoring width), so a later freq/param event re-checks
                # the standing density correctly (a tune-mode ramp point sets --power directly).
                if not p["run_mode"]:
                    held_view = float(p["val"]) + ramp_auth_vd[p["uid"]]
                    held_flagged = bool(_clamp(held_view, state))
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
                # HELD: re-check the STANDING controlled-view value (the density the operator set)
                # at this event's operating point — a wider --bw can push it out of range even
                # though the base value is unchanged and the base range is bandwidth-invariant.
                viol = _clamp(held_view, state) if held_view is not None else None
                if viol and not held_flagged:
                    direction, bound, freq_hz = viol
                    issues.append(_held_power_issue(
                        task, power_label, unit, float(held_view), direction, bound,
                        freq_hz, _fire_s, _changed_desc(touched)))
                held_flagged = bool(viol)
            else:
                # DIRECTLY-SET: this step commands --power; its control view becomes the held
                # quantity (latest-set-wins), and its controlled-view value the new standing value,
                # flagged if it can't be delivered at this moment's --bw.
                active[0] = _law_for_view(p.get("power_view"))
                held_view = _view_value_of(state)
                viol = _clamp(held_view, state)
                if viol:
                    direction, bound, freq_hz = viol
                    issues.append(_set_power_issue(
                        task, power_label, unit, float(held_view), direction, bound,
                        freq_hz, _fire_s))
                held_flagged = bool(viol)
        for uid, hitlist in hits.items():
            issues.extend(_ramp_issues(task, meta[uid]["param"], unit, hitlist))
    return issues


def hold_control_quantity(items, resolve):
    """Return a COPY of ``items`` with the calibrated --power INJECTED so the latest-set control
    quantity is HELD across --bw changes — the client-side precompute of the hold the Run/Tune form
    already does live (keep the displayed quantity fixed on a --bw change; re-send the recomputed
    base). Applied to the DEPLOYED steps (``TimelineEditor.steps``): at each --bw change under a held
    bw-keyed density, the standing density's base is re-derived at the new sweep width and injected
    into that step, clamped to the achievable range (warn-never-block — ``achievability_warnings``
    flags where it clamps). Injected steps are marked ``power_auto_held`` so the client strips them
    on load (the authored --bw step stays a --bw step) and re-derives them fresh on the next save.

    A no-op for a task with no bw-keyed control view (total power / gain / dBm / a constant chain
    all hold via a constant base, which the runtime already keeps). The walk mirrors
    ``achievability_warnings`` (fire-time order, per-step control view, set-time held value) so the
    injected value and the warning agree. Ramps are handled only insofar as a --power ramp updates
    the standing density; injecting a held --power INTO ramp points is Surface B / Stage 3."""
    from state.power_fold import (PowerFold, fold_params_from_values,   # pure (no Qt); lazy
                                  resolve_keyed_values)

    items = list(items)
    try:
        window = min_on_air_duration(items)
    except Exception:                      # noqa: BLE001 — a transform helper must never break
        window = 0.0

    injections: Dict[int, tuple] = {}      # item index → (base_new, view_id)

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
        if fold is None:
            continue
        default_view_law = _view_law_of(info.get("view_law"))
        default_view_id = default_view_law.id if default_view_law is not None else None
        laws_by_id = {vid: _view_law_of(spec)
                      for vid, spec in (info.get("view_laws") or {}).items()}
        laws_by_id = {vid: law for vid, law in laws_by_id.items()
                      if law is not None and law.params()}       # only bw-keyed views need holding
        # Nothing to hold on a --bw change unless SOME control view is bw-keyed.
        if not ((default_view_law is not None and default_view_law.params()) or laws_by_id):
            continue

        def _law_for_view(pv):
            return default_view_law if pv is None else laws_by_id.get(pv)

        def _id_for_view(pv):
            return default_view_id if pv is None else pv

        freq_param = info.get("freq_param")
        freq_factor = float(info.get("freq_factor") or 1.0)
        base_args = list(info.get("base_args") or [])
        flag_to_dest = {f: (s.get("dest") or s.get("name"))
                        for s in specs for f in (s.get("flags") or [])}
        name_to_dest = {(s.get("name") or s.get("dest")): s.get("dest") for s in specs}

        active = [default_view_law]
        active_id = [default_view_id]

        def _view_delta(state) -> float:
            law = active[0]
            if law is None:
                return 0.0
            keyed = resolve_keyed_values(specs, state, law.params())
            try:
                return law.delta_db(keyed) if keyed else law.rep_delta_db()
            except (ValueError, TypeError):
                return law.rep_delta_db()

        def _held_value(state):
            p = state.get(power_dest)
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                return None
            return float(p) + _view_delta(state)

        # The Hold is a clock-reset boundary (docs/sequence-hold-step.md §6.5): a window-B
        # (anchor="hold") step orders after window A at hold_offset + its offset, so an injection
        # re-derives the held base at the right moment (mirrors achievability_warnings).
        h_off = hold_offset(items)
        step_bases = resolve_step_offsets(items, h_off)

        # Fire-time events, keeping the item index so a --bw change can be injected into (mirrors
        # achievability_warnings' event build).
        events: list = []
        for seq_idx, it in enumerate(items):
            if getattr(it, "task_name", None) != task:
                continue
            act = getattr(it, "action", "run")
            pv = getattr(it, "power_view", None)
            if getattr(it, "kind", None) == "bar":
                events.append((_fire_time_s("start", getattr(it, "start_offset", 0.0), window),
                               seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args),
                                "power_view": pv}))
            elif act == "tune":
                fa, fo = effective_anchor_offset(it, h_off, step_bases)
                events.append((_fire_time_s(fa, fo, window), seq_idx, "tune",
                               {"params": dict(getattr(it, "params", {}) or {}), "power_view": pv}))
            elif act == "run":
                fa, fo = effective_anchor_offset(it, h_off, step_bases)
                events.append((_fire_time_s(fa, fo, window), seq_idx, "args",
                               {"replace": getattr(it, "replace_args", True), "args": list(it.args),
                                "power_view": pv}))
            # A ramp is a --power sweep; it updates the standing density (last point) but is not
            # itself injected here (Stage 3). Approximate its end value by the ramp's stop.
            elif act == "ramp":
                r = dict(getattr(it, "ramp", None) or {})
                rdest = name_to_dest.get(r.get("param")) or flag_to_dest.get(r.get("flag"))
                if rdest != power_dest or r.get("mode") == "run":
                    continue
                r_anchor, r_offset = effective_anchor_offset(it, h_off, step_bases)
                try:
                    resolved = _resolve_ramp_points(r, r_anchor, window)
                    fires = _place_ramp_points(r, r_anchor, r_offset, resolved)
                except (ValueError, TypeError):
                    continue
                if fires:
                    fa, foff, val = fires[-1]
                    events.append((_fire_time_s(fa, foff, window), seq_idx, "rampend",
                                   {"val": float(val), "rdest": rdest, "power_view": pv}))
        events.sort(key=lambda e: (e[0], e[1]))

        state = _args_to_values(base_args, flag_to_dest)
        held = _held_value(state)          # standing control-quantity value (view units)
        for _fire_s, seq_idx, kind, p in events:
            touched: set = set()
            if kind == "args":
                new_vals = _args_to_values(p["args"], flag_to_dest)
                if p["replace"]:
                    touched = set(state) | set(new_vals)
                    state = new_vals
                else:
                    touched = set(new_vals)
                    state.update(new_vals)
            elif kind == "tune":
                for k, v in p["params"].items():
                    try:
                        state[name_to_dest.get(k, k)] = float(v)
                        touched.add(name_to_dest.get(k, k))
                    except (TypeError, ValueError):
                        pass
            elif kind == "rampend":
                active[0] = _law_for_view(p.get("power_view"))
                active_id[0] = _id_for_view(p.get("power_view"))
                state[p["rdest"]] = p["val"]
                held = _held_value(state)
                continue

            if power_dest in touched:
                # A power-setting step: its control view becomes the held quantity (latest-set-wins).
                active[0] = _law_for_view(p.get("power_view"))
                active_id[0] = _id_for_view(p.get("power_view"))
                held = _held_value(state)
            else:
                # A change that doesn't set --power. If it moved a param the HELD bw-keyed view keys
                # on (a --bw change), re-derive the base to keep the held density constant and inject.
                law = active[0]
                if law is not None and held is not None and (set(law.params()) & touched):
                    fv = state.get(freq_param) if freq_param else None
                    freq_hz = (float(fv) * freq_factor
                               if isinstance(fv, (int, float)) and not isinstance(fv, bool) else None)
                    params = fold_params_from_values(artifact, specs, state)
                    b = fold.bounds_at(freq_hz, params)
                    base_new = held - _view_delta(state)            # base to deliver held at new op
                    base_new = min(max(base_new, b["min_power_dbm"]), b["max_power_dbm"])   # clamp
                    base_new = round(base_new, 4)
                    injections[seq_idx] = (base_new, active_id[0], power_dest)
                    state[power_dest] = base_new    # subsequent holds see the (clamped) standing base

    if not injections:
        return items
    out = []
    for i, it in enumerate(items):
        inj = injections.get(i)
        if inj is None or getattr(it, "action", None) != "tune":
            out.append(it)
            continue
        base_new, view_id, pdest = inj
        new_params = dict(getattr(it, "params", {}) or {})
        new_params[pdest] = base_new
        out.append(replace(it, params=new_params, power_view=view_id, power_hold_dest=pdest))
    return out


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
    # Pre-resolve step anchors to start-anchored offsets: api.ramp is drift-guarded and only
    # knows start/stop/both, so a step-anchored tail must be expressed on the on-air clock at
    # its resolved base before delegating (the agent resolves anchor="step" in sequence_runner,
    # not in ramp.py). A step-anchored item's canonical uid is on the ITEM, not the flat step,
    # so resolve over items and map by fire order.
    h_off = hold_offset(items)
    step_bases = resolve_step_offsets(items, h_off)
    # Build a parallel list of resolved (anchor, offset) overrides keyed by step index, in the
    # same order item_to_steps emits. A step-anchored RunItem emits exactly one step.
    overrides: Dict[int, Tuple[str, float]] = {}
    flat_idx = 0
    for it in items:
        n = len(item_to_steps(it))
        if getattr(it, "anchor", "start") == "step" and getattr(it, "uid", None) in step_bases:
            overrides[flat_idx] = ("start", step_bases[getattr(it, "uid")])
        flat_idx += n
    objs = []
    for i, s in enumerate(items_to_steps(items)):
        r = s.get("ramp")
        robj = None
        if r:
            robj = SimpleNamespace(
                start=r.get("start"), stop=r.get("stop"),
                steps=r.get("steps"), step=r.get("step"),
                hold_s=r.get("hold_s"), duration_s=r.get("duration_s"))
        anchor, offset = s.get("anchor", "start"), s.get("offset_s", 0.0)
        if i in overrides:
            anchor, offset = overrides[i]
        objs.append(SimpleNamespace(
            anchor=anchor, offset_s=offset,
            offset_end_s=s.get("offset_end_s", 0.0),
            action=s.get("action", ""), ramp=robj))
    return _ramp.min_on_air_duration(objs)
