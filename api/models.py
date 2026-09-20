"""
Client-side Pydantic models mirroring the agent's API contract.

These intentionally match agent/models.py field-for-field so the GUI and agent
share one source of truth for the API shape. If the agent's models change, mirror
the change here.

Models are split into:
  - Response models (what endpoints return)
  - Request models (what endpoints accept)
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Dict, List
from pydantic import BaseModel


# ══════════════════════════════════════════════════════════════════════════════
# Unit types + library scoping
# ══════════════════════════════════════════════════════════════════════════════
#
# A fleet is heterogeneous: mobile Raspberry Pi "broadcaster" units and stationary
# Ettus "x410" units. One canonical library serves both — every item carries a
# `types` list saying which unit kinds it applies to. Empty = shared (all kinds),
# which is the back-compatible default so pre-existing items deploy everywhere.
# These constants live here in the dependency-free models layer; config.py
# re-exports them for the UI.

UNIT_TYPES = ("broadcaster", "x410")
UNIT_TYPE_LABELS = {"broadcaster": "Broadcaster", "x410": "X410"}
DEFAULT_UNIT_TYPE = "broadcaster"


def applies_to_type(item_types: List[str], unit_type: str) -> bool:
    """Does a library item scoped to `item_types` apply to a unit of `unit_type`?
    Empty `item_types` means shared → applies to every unit kind."""
    return not item_types or unit_type in item_types


# ══════════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════════

class ProcessState(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    STOPPING = "stopping"
    CRASHED  = "crashed"


class TaskHealth(str, Enum):
    """A task's RF / flowgraph health — a SEPARATE axis from the exit-driven ProcessState
    (mirrors agent/models.py; docs/rf-fault-recovery.md §5.3). A halted GNU Radio flowgraph
    is still process-RUNNING, so its fault is this field, never a ProcessState value. Phase 1
    only ever sets OK and RF_FAULT; STALLED/UNKNOWN are reserved for the follow-ups."""
    OK       = "ok"
    STALLED  = "stalled"     # sustained underflow / degraded (reserved; not set in Phase 1)
    RF_FAULT = "rf_fault"    # the flowgraph halted / a GR buffer fault — dead-but-alive
    UNKNOWN  = "unknown"


class EventState(str, Enum):
    ARMED     = "armed"
    RUNNING   = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED   = "aborted"


class SequenceState(str, Enum):
    ARMED     = "armed"
    RUNNING   = "running"
    HOLDING   = "holding"      # parked at a Hold, RF live, awaiting operator proceed
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED   = "aborted"


class StepAction(str, Enum):
    START = "start"   # launch a long-running task (paired with a STOP)
    STOP  = "stop"    # stop a long-running task
    RUN   = "run"     # fire-and-exit one-shot: launch, self-terminates, no stop
    TUNE  = "tune"    # retune a running task's live parameters (see SequenceStep.params)
    RAMP  = "ramp"    # sweep one live parameter over time (expands to tunes on the unit)
    HOLD  = "hold"    # operator-gated pause marker: a boundary step (no task work) that
                      # splits a sequence into window A (pre-hold) and window B
                      # (anchor="hold"). See docs/sequence-hold-step.md.


class RampSpec(BaseModel):
    """A parameter ramp for a RAMP step: sweep `param` from start to stop. Give any
    two of {step, hold_s, duration_s}; the third derives. A both-anchored ramp fills
    the plan's on-air window, so only one of {step, hold_s} is given. Mirrors the
    agent's RampSpec; expansion lives in api.ramp."""
    param: str
    start: float
    stop: float
    steps: Optional[int] = None          # number of equal increments (divides evenly)
    step: Optional[float] = None         # OR a fixed value increment
    hold_s: Optional[float] = None       # dwell each level is held
    duration_s: Optional[float] = None   # total held time = levels × hold (single-anchor)
    # Every emitted level is held for `hold`, incl. the last; drop the start/stop
    # level (and its hold) to chain ramps without a doubled seam. Single-anchor only.
    include_first: bool = True
    include_last: bool = True
    mode: str = "tune"                   # "tune" (live set_params) | "run" (task per point)
    flag: Optional[str] = None           # run mode: CLI flag for the ramped param
    integer: bool = False                # run mode: round each value to an int


# ══════════════════════════════════════════════════════════════════════════════
# Tasks
# ══════════════════════════════════════════════════════════════════════════════

class TaskConfig(BaseModel):
    name: str
    description: str = ""
    command: List[str]
    working_dir: str = "/opt/sdr-agent"
    env: Dict[str, str] = {}
    autostart: bool = False
    restart_on_crash: bool = False
    restart_delay_s: float = 3.0
    # RF-fault RECOVERY (Phase 3b, ../sdr-agent/docs/rf-fault-recovery.md §7.1/§14e). A STANDALONE
    # task's own "Auto-restart on fault" — when set, the agent relaunches the task with the same
    # parameters if it RF-faults AND is not owned by an active run (budget max_fault_restarts). Mirrors
    # the agent wire field-for-field; defaulted so an older agent that drops it just never auto-restarts.
    auto_restart_on_fault: bool = False
    max_fault_restarts: int = 2
    resumable: bool = False
    resume_offset_mode: str = "arg"
    resume_offset_flag: str = "--start-offset"
    resume_offset_env: str = "SDR_START_OFFSET"
    # Which unit types this task applies to (see UNIT_TYPES in config). Empty = shared
    # (all types) — the default, so existing tasks deploy everywhere exactly as before.
    types: List[str] = []


class ProcessStatus(BaseModel):
    name: str
    description: str
    state: ProcessState
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    restart_count: int = 0
    log_file: str = ""
    # RF / flowgraph health — a separate axis from `state` (§5.3). Defaulted so an older
    # agent (no health field) parses unchanged. The durable poll backstop for the fault pill;
    # the loud, instant path is the TaskHealthEvent over SSE.
    health: TaskHealth = TaskHealth.OK
    health_detail: str = ""            # short reason, e.g. the fault signature that matched
    last_output_at: Optional[str] = None   # ISO-8601 of the task's last log output (advisory)


class StartRequest(BaseModel):
    env_overrides: Dict[str, str] = {}
    args: List[str] = []
    # When False, args are APPENDED to the task's configured command. When True,
    # args REPLACE the task's trailing args — the launch becomes
    # [interpreter, script, *args] — so a form can fully specify the parameters
    # for one run without touching the deployed task definition. Mirrors the
    # agent's StartRequest.replace_args.
    replace_args: bool = False
    # RF-fault RECOVERY (Phase 3b): a per-launch override of the task's auto_restart_on_fault, so the
    # Run… form can turn Auto-restart-on-fault on/off for just this run. None = use the task default.
    auto_restart_on_fault: Optional[bool] = None


class ExitRecord(BaseModel):
    started_at: Optional[str]
    exited_at: str
    exit_code: Optional[int]
    was_crash: bool


# ══════════════════════════════════════════════════════════════════════════════
# Agent meta / health
# ══════════════════════════════════════════════════════════════════════════════

class AgentInfo(BaseModel):
    hostname: str
    unit_id: str
    machine_id: str = ""
    agent_version: str
    python_version: str
    tasks: List[str]
    previous_version: Optional[str] = None   # OTA rollback target, if any
    # Feature flags the agent advertises. Defaulted so an agent predating this field
    # (which simply omits it) parses fine and reads as "no advertised capabilities".
    capabilities: List[str] = []
    # Where this unit keeps scripts + the interpreter its tasks launch with — used
    # to default a new task's fields per unit (X410 differs from the Pi layout).
    scripts_dir: str = ""
    task_interpreter: str = "python3"


class UpdateResult(BaseModel):
    """Result of POST /admin/update or /admin/rollback."""
    ok: bool
    from_version: str = ""
    to_version: str = ""
    message: str = ""


class AgentRelease(BaseModel):
    version: str
    active: bool
    healthy: bool
    path: str


class SystemHealth(BaseModel):
    unit_id: str
    cpu_percent: float
    cpu_temp_c: Optional[float]
    cpu_throttled: Optional[bool]
    mem_percent: float
    mem_used_mb: float
    mem_total_mb: float
    disk_percent: float
    disk_free_gb: float
    uptime_s: float
    load_avg: List[float]
    utc_now: str = ""
    clock_synced: Optional[bool] = None   # True if NTP-synchronized (real internet time)
    clock_source: str = ""                # "chrony"/"systemd-timesyncd" (NTP), "manual"
                                          # (hand-set to the PC clock), or "" if unknown


class SdrDevice(BaseModel):
    type: str = ""
    serial: str = ""
    name: str = ""
    product: str = ""


class SdrStatus(BaseModel):
    detected: bool
    device_count: int
    devices: List[SdrDevice]
    raw_output: str = ""
    error: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# Events (SSE stream payloads)
# ══════════════════════════════════════════════════════════════════════════════

class CrashEvent(BaseModel):
    type: str = "crash"
    unit_id: str
    task_name: str
    task_description: str
    exit_code: Optional[int]
    started_at: Optional[str]
    crashed_at: str
    restart_count: int
    last_log_lines: List[str]


class FaultSnapshot(BaseModel):
    """Machine + per-task resource state captured at RF-fault detection (mirrors
    agent/models.py; docs/rf-fault-recovery.md §6.3) so the next vmcircbuf failure is
    self-diagnosing. All optional/best-effort — a field is blank when its source was
    unavailable on the unit. The client renders it on the fault log, never computes it."""
    captured_at: str = ""
    shm_used_bytes: Optional[int] = None
    shm_total_bytes: Optional[int] = None
    map_count: Optional[int] = None            # /proc/<pid>/maps line count (VMAs)
    map_max: Optional[int] = None              # /proc/sys/vm/max_map_count
    rss_bytes: Optional[int] = None
    nofile_soft: Optional[int] = None          # the task's RLIMIT_NOFILE
    nofile_hard: Optional[int] = None
    vmcircbuf_backend_env: str = ""            # the task's GR_CONF_VMCIRCBUF_DEFAULT_FACTORY env var (informational)
    vmcircbuf_backend_pref: str = ""           # the EFFECTIVE backend: GR's vmcircbuf_default_factory pref file
                                                # under the task HOME (agent >= 1.31.1 writes + reads it)
    vmcircbuf_backend_compiled: str = ""       # gnuradio-config-info [vmcircbuf] default_factory
    task_home: str = ""
    ipcs_summary: str = ""                     # only captured when a SysV backend is implicated
    uhd_log_tail: List[str] = []               # tail of the per-task UHD log
    snapshot_path: str = ""                    # where the full JSON was written on the unit
    log_path: str = ""                         # the task's current.log at detection
    notes: List[str] = []                      # 'pid gone' / 'gnuradio-config-info absent' / …


class TaskHealthEvent(BaseModel):
    """SSE event fired when a task's RF/flowgraph health turns to a fault (§5.3; mirrors
    agent/models.py). Same shape + dispatch as CrashEvent, but routed to a LOUD client alarm.
    `type` starts with 'task_', so classify() needs an explicit branch BEFORE its generic
    'task_' → TaskEvent rule."""
    type: str = "task_health"          # discriminator (classify() matches this exact value)
    unit_id: str
    task_name: str
    task_description: str = ""
    health: TaskHealth = TaskHealth.RF_FAULT
    detail: str = ""                   # the fault signature / reason
    at: str                            # ISO-8601
    last_log_lines: List[str] = []     # log tail at detection (immediate on-screen context)
    snapshot: Optional[FaultSnapshot] = None


class EventWebhook(BaseModel):
    type: str
    unit_id: str
    event_id: str
    task_name: str
    start_at: str
    stop_at: str
    state: str
    at: str
    detail: str = ""


class TaskEvent(BaseModel):
    type: str                          # task_started | task_stopped | task_restarted
    unit_id: str
    task_name: str
    state: str
    pid: Optional[int] = None
    at: str
    detail: str = ""


class SequenceWebhook(BaseModel):
    # sequence_started | sequence_on_air | sequence_step | sequence_off_air | sequence_stopped | sequence_aborted | sequence_modified | sequence_hold | sequence_proceed | sequence_hold_timeout | sequence_rf_fault
    type: str
    unit_id: str
    run_id: str
    sequence_name: str
    on_air_at: str
    on_air_end: Optional[str] = None
    state: str
    at: str
    detail: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# Scheduled events (simple, single-task)
# ══════════════════════════════════════════════════════════════════════════════

class ScheduledEvent(BaseModel):
    id: str
    task_name: str
    start_at: str
    stop_at: str
    state: EventState = EventState.ARMED
    created_at: str = ""
    started_actual: Optional[str] = None
    stopped_actual: Optional[str] = None
    already_running: bool = False
    note: str = ""


class CreateEventRequest(BaseModel):
    task_name: str
    start_at: str
    stop_at: Optional[str] = None
    duration_s: Optional[float] = None
    note: str = ""


class PatchEventRequest(BaseModel):
    stop_at: str


# ══════════════════════════════════════════════════════════════════════════════
# Sequences (per-unit choreography)
# ══════════════════════════════════════════════════════════════════════════════

class SequenceStep(BaseModel):
    anchor: str = "start"              # "start" | "stop" | "both" (ramp) | "hold" (post-Hold window B) | "step"
                                       #   | "enter" (from the Hold's ENTER instant — the pause's start,
                                       #     window A; a ramp tied by its END, offset_s ≤ 0; agent ≥ 1.26.0)
    offset_s: float
    # "both"-anchored ramp: off-air-side inset (≤ 0). Fills [on-air+offset_s, off-air+offset_end_s].
    offset_end_s: Optional[float] = None
    # Step-to-step anchoring (agent ≥ 1.24.0, capability "sequence-step-anchor"). A stable,
    # client-assigned id lets OTHER steps hang off this one; an anchor="step" step fires at
    # <target step's anchor_edge> + offset_s (offset >= 0 — a dependent never precedes its target).
    # Phase 1: tunes/ramps/duration tasks only (not the Hold), and not alongside a Hold.
    id: str = ""                       # stable id (client-assigned); blank = not referenced
    anchor_step_id: str = ""           # target step id (when anchor == "step")
    anchor_edge: str = "end"           # "start" | "end" of the target step's extent
    # Which of THIS step's edges is tied to that target — only a RAMP has two. "start" (default):
    # the ramp runs forward from `target edge + offset_s`. "end": the ramp's END sits at the tied
    # point and the ramp runs backward from it; offset_s is STILL the start's offset (the end's
    # offset − duration), so the agent places it unchanged — this is client authoring metadata the
    # agent carries through (≥ 1.25.3; an older agent drops it and the ramp reloads start-tied at
    # the same timing).
    anchor_own_edge: str = "start"
    action: StepAction
    task_name: str
    args: List[str] = []               # CLI args for this step's start/run
    replace_args: bool = False         # True: args are the complete set (replace task defaults)
    inject_resume_offset: bool = False
    # TUNE step: live-parameter values to apply to the running task, {name: value}.
    params: Dict[str, Any] = {}
    # RAMP step: the parametric sweep (expanded into tunes on the unit at arm time).
    ramp: Optional[RampSpec] = None
    # The calibrated --power CONTROL QUANTITY the operator authored this step in — a
    # CAL_POWER_LAWS view id (e.g. "psd_live" spectral density, "fbw_power" total power) or
    # None for the signal's default quantity. Client-only authoring metadata: --power is still
    # sent in the base quantity; this records which quantity is HELD from this step forward, so a
    # later --bw/--freq change re-derives base to keep THAT quantity constant (a chirp's live
    # density), matching the Run/Tune power card. The agent never reads it.
    power_view: Optional[str] = None
    # When set, the dest of a --power the client's hold precompute INJECTED into this step (not set
    # by the operator) — a --bw change that re-derives base to hold the standing control quantity.
    # The client strips this --power on load (by this dest) so the authored timeline stays clean (a
    # --bw step is edited as a --bw step) and re-derives it fresh on the next save. The agent just
    # runs the --power it's given, exactly as for an operator-set one.
    power_hold_dest: Optional[str] = None


class Sequence(BaseModel):
    id: str
    name: str
    description: str = ""
    steps: List[SequenceStep]
    # Unit types this sequence targets. Empty = shared/all. A sequence runs on one
    # unit, so its tasks must exist there — the plan editor offers a sequence only for
    # a unit whose type matches (or a shared sequence).
    types: List[str] = []
    # ── RF-fault RECOVERY Phase 3 (docs/rf-fault-recovery.md §11/§14d) — the sequence-authored
    # UNATTENDED auto-restart policy, carried to the arm as ArmSequenceRequest.restart_policy/mode.
    # "manual" (default) = an rf-fault leaves the run faulted for an operator Restart (Phase 2);
    # "auto" = the agent auto-restarts (budget-limited); "confirm" is reserved (treated as manual
    # by the runtime today). "resync" rejoins the schedule, "replay" restarts from the crash point.
    # Defaulted so a library from before this feature deserializes unchanged.
    recovery_policy: str = "manual"
    recovery_mode: str = "resync"


class CreateSequenceRequest(BaseModel):
    name: str
    description: str = ""
    steps: List[SequenceStep]
    # Unit types this sequence targets; empty = shared/all. Carried through the
    # library's create/update so a sequence keeps its scope. Only the library uses
    # it; a live unit's agent ignores the field (it holds only its own sequences).
    types: List[str] = []
    # ── RF-fault RECOVERY Phase 3 — the authored auto-restart policy (see Sequence). Carried
    # through the library's create/update so a sequence keeps its recovery choice.
    recovery_policy: str = "manual"
    recovery_mode: str = "resync"


class StepFire(BaseModel):
    anchor: str
    offset_s: float
    action: str
    task_name: str
    fire_at: str
    fired_actual: Optional[str] = None
    resume_offset_s: Optional[float] = None
    args: List[str] = []
    replace_args: bool = False
    params: Dict[str, Any] = {}


class SequenceRun(BaseModel):
    id: str
    sequence_id: str
    sequence_name: str
    state: SequenceState = SequenceState.ARMED
    on_air_at: str
    on_air_end: Optional[str] = None
    open_ended: bool = False
    created_at: str = ""
    started_actual: Optional[str] = None
    on_air_actual: Optional[str] = None    # when T0 was actually crossed (RF live)
    stopped_actual: Optional[str] = None
    resume_offset_s: float = 0.0
    note: str = ""
    steps: List[StepFire] = []
    plan_id: str = ""
    plan_name: str = ""
    # ── Hold step (docs/sequence-hold-step.md) — mirrors agent/models.py.
    # All defaulted so a run from an older agent (no Hold fields) deserializes unchanged.
    hold_at_offset_s: Optional[float] = None  # window-A end offset (the hold's position from T0)
    held_actual: Optional[str] = None         # wall-clock (UTC ISO) HOLDING was entered
    resumed_actual: Optional[str] = None       # wall-clock (UTC ISO) the operator proceeded
    hold_aware: bool = False                   # interactive (Library) arm only; False = no-op Hold
    max_hold_s: float = 1800.0                 # auto-abort deadman while HOLDING; 0 = unlimited
    # RF-fault coupling (docs/rf-fault-recovery.md §5.3; mirrors agent/models.py): when a task an
    # active run owns is detected dead-but-alive, the run is stamped here (an rf_fault FIELD on a
    # still-RUNNING run, not a terminal SequenceState). Defaulted so pre-feature runs deserialize
    # unchanged. The Phase-2 restart reads the crash-time state; Phase 1 only surfaces the pill.
    fault: str = ""                    # "" = healthy; else the faulted task + reason
    fault_task: str = ""               # the specific task name that faulted (a run may own several)
    fault_at: str = ""                 # ISO-8601 of detection
    # ── RF-fault RECOVERY Phase 3 (docs/rf-fault-recovery.md §11/§14d; mirrors agent/models.py):
    # the run's UNATTENDED auto-restart policy + the breaker counters, so the row can show
    # "auto-restarting (n/N)" vs a tripped RF FAULT. All defaulted (skew-safe: a run from a
    # pre-Phase-3 agent has restart_policy="manual", auto_restart_count=0).
    restart_policy: str = "manual"     # "auto" | "confirm" | "manual" — who triggers recovery
    restart_mode: str = "resync"       # "resync" | "replay" — the mode the auto-trigger uses
    auto_restart_count: int = 0        # auto-restart attempts consumed (the budget counter)
    auto_restart_task: str = ""        # the task the auto-restart relaunched (watched for the reset)
    auto_restart_healthy_since: str = ""  # ISO when that task was first observed healthy (settle)


class StepOverride(BaseModel):
    """A per-step parameter override applied when arming a sequence, addressed by
    the step's index in the sequence. Mirrors agent/models.py StepOverride."""
    index: int
    args: List[str] = []
    replace_args: bool = True


class ArmSequenceRequest(BaseModel):
    on_air_at: str
    on_air_end: Optional[str] = None
    on_air_duration_s: Optional[float] = None
    open_ended: bool = False
    resume_offset_s: float = 0.0
    note: str = ""
    plan_id: str = ""
    plan_name: str = ""
    step_overrides: List[StepOverride] = []
    steps: Optional[List[SequenceStep]] = None   # inline plan-local step list
    # ── Hold step (docs/sequence-hold-step.md) — mirrors agent/models.py.
    # hold_aware=True (interactive Library arm only) makes a Hold real; False (default)
    # is today's behavior. Phase 0 carries the fields; the arm surfaces are Phase 2.
    hold_aware: bool = False
    max_hold_s: float = 1800.0                    # auto-abort deadman while HOLDING; 0 = unlimited
    # ── RF-fault RECOVERY Phase 3 — the UNATTENDED auto-restart policy sent at arm (mirrors
    # agent/models.py ArmSequenceRequest). "manual" (default) = no unattended restart, so a
    # pre-Phase-3 client or a Hold-free arm never grants autonomy; the client sends "auto"
    # (with a resync/replay mode) explicitly only when the sequence authored it and the unit
    # advertises `sequence-auto-restart`. An older agent ignores the fields (defaulted → manual).
    restart_policy: str = "manual"
    restart_mode: str = "resync"


class PatchSequenceRunRequest(BaseModel):
    on_air_end: str


class ProceedRequest(BaseModel):
    """Body for the (Phase 1) POST /sequence-runs/{id}/proceed — resume a HOLDING run.
    proceed_at is the operator's chosen resume instant; steps, if given, is the edited
    window-B step list from edit-while-holding. Mirrors agent/models.py ProceedRequest."""
    proceed_at: str
    steps: Optional[List[SequenceStep]] = None


class RestartRunRequest(BaseModel):
    """Body for the (Phase 2) POST /sequence-runs/{id}/restart — recover a run whose task
    RF-faulted (docs/rf-fault-recovery.md §7). Mirrors agent/models.py RestartRequest.
    mode "resync" (default) rejoins the original schedule; "replay" delivers the whole
    remaining profile, shifting off-air later by the downtime. restart_at is server-set,
    so the client omits it."""
    mode: str = "resync"               # "resync" | "replay"


# ── Hold step helpers (docs/sequence-hold-step.md §7) ─────────────────────────
# Detect a Hold in a step list and compile it out for the unattended (scheduled /
# plan) path, where an operator-gated pause is a footgun. `has_hold` duck-types over
# SequenceStep or a dict; `collapse_hold` operates on SequenceStep objects (it uses
# model_copy) — the callers always hold typed step lists.

def _step_action(step) -> str:
    a = getattr(step, "action", None)
    if a is None and isinstance(step, dict):
        a = step.get("action")
    return a.value if hasattr(a, "value") else str(a)


def has_hold(steps) -> bool:
    """True if the step list contains a HOLD marker (an operator-gated pause)."""
    return any(_step_action(s) == StepAction.HOLD.value for s in (steps or []))


def collapse_hold(steps: List["SequenceStep"]) -> List["SequenceStep"]:
    """Compile a Hold OUT of a step list for the unattended (scheduled / plan) path:
    drop the HOLD marker and re-anchor every window-B (``anchor="hold"``) step to
    ``start`` at ``hold_offset + its own offset`` — a zero-length pass-through, so the
    run executes straight through without pausing (the down-ramp starts immediately
    after the up-ramp). A Hold-free list is returned unchanged (same objects), so the
    non-hold path is byte-identical. Operates on ``SequenceStep`` objects (uses
    ``model_copy``). See docs/sequence-hold-step.md §7."""
    steps = list(steps or [])
    hold_off = None
    for s in steps:
        if _step_action(s) == StepAction.HOLD.value:
            hold_off = float(s.offset_s)
            break
    if hold_off is None:
        return steps
    out: List["SequenceStep"] = []
    for s in steps:
        if _step_action(s) == StepAction.HOLD.value:
            continue                                     # the boundary marker is dropped
        if getattr(s, "anchor", None) == "hold":
            out.append(s.model_copy(update={
                "anchor": "start", "offset_s": hold_off + float(s.offset_s)}))
        elif getattr(s, "anchor", None) == "enter":
            # Measured from the pause's START, which with the Hold compiled out is just the on-air
            # instant hold_off: a point fires at hold_off + offset; a ramp (offset = its END's, like
            # a stop anchor) is re-expressed by its START so the agent's start layout runs it forward.
            off = hold_off + float(s.offset_s)
            if _step_action(s) == StepAction.RAMP.value and s.ramp is not None:
                from . import ramp as _ramp
                try:
                    r = s.ramp
                    off -= _ramp.resolve_ramp(r.start, r.stop, steps=r.steps, step=r.step,
                                              hold_s=r.hold_s, duration_s=r.duration_s,
                                              include_first=r.include_first,
                                              include_last=r.include_last).duration_s
                except (ValueError, TypeError):
                    pass
            out.append(s.model_copy(update={"anchor": "start", "offset_s": off}))
        else:
            out.append(s)
    return out


def split_ramps_at_hold(steps: List["SequenceStep"]) -> List["SequenceStep"]:
    """For EDIT-WHILE-HOLDING (docs/sequence-hold-step.md §5.7 / §6.4): present a ramp that
    CROSSES the Hold as two steps — the run-up to the pause (window A, already fired, kept exactly:
    the points at/before the pause) and the not-yet-fired REMAINDER as its own post-hold
    (``anchor="hold"``) ramp whose start / stop / timing default to what resuming would have
    produced (the first deferred level → the original stop, the original dwell, ``offset_s`` = the
    first deferred point's time after the pause). The operator can then retarget the remainder like
    any window-B step; left alone, it reproduces the agent's paused remainder exactly. A lone point
    on either side becomes the single tune (or, for a run-mode ramp, the one-shot run) it is.
    Non-crossing steps come back as the same objects; a Hold-free list is returned unchanged."""
    from . import ramp as _ramp
    steps = list(steps or [])
    hold_off = next((float(s.offset_s) for s in steps
                     if _step_action(s) == StepAction.HOLD.value), None)
    if hold_off is None:
        return steps
    out: List["SequenceStep"] = []
    for s in steps:
        r = s.ramp
        if (_step_action(s) != StepAction.RAMP.value or r is None
                or getattr(s, "anchor", "start") != "start"):
            out.append(s)
            continue
        try:
            res = _ramp.resolve_ramp(r.start, r.stop, steps=r.steps, step=r.step, hold_s=r.hold_s,
                                     duration_s=r.duration_s, include_first=r.include_first,
                                     include_last=r.include_last)
            pts = _ramp.place_ramp("start", float(s.offset_s), res)
        except (ValueError, TypeError):
            out.append(s)
            continue
        before = [(float(off), float(v)) for (_a, off, v) in pts if float(off) <= hold_off + 1e-6]
        after = [(float(off), float(v)) for (_a, off, v) in pts if float(off) > hold_off + 1e-6]
        if not before or not after:
            out.append(s)                                  # doesn't cross the pause
            continue
        out.append(_ramp_piece(s, "start", before[0][0], before, res.hold_s))
        out.append(_ramp_piece(s, "hold", after[0][0] - hold_off, after, res.hold_s, fresh=True))
    return out


def _fmt_ramp_value(v: float, integer: bool) -> str:
    return str(int(round(v))) if integer else f"{v:g}"


def _ramp_piece(s: "SequenceStep", anchor: str, offset_s: float, pts, hold_s: float,
                fresh: bool = False) -> "SequenceStep":
    """One piece of a ramp split at the Hold: a ramp over exactly ``pts`` (its levels, each held
    ``hold_s``), or — for a single point — the one tune / one-shot run that point is. ``fresh``
    marks the remainder as a NEW step (no stable id — it is not the original reference target)."""
    r = s.ramp
    values = [v for (_off, v) in pts]
    base: Dict[str, Any] = {"anchor": anchor, "offset_s": float(offset_s), "offset_end_s": None,
                            "anchor_step_id": "", "anchor_edge": "end", "anchor_own_edge": "start"}
    if fresh:
        base["id"] = ""
    if len(values) == 1:
        v = values[0]
        if getattr(r, "mode", "tune") == "run":
            vs = _fmt_ramp_value(v, bool(getattr(r, "integer", False)))
            args = list(s.args or []) + ([r.flag, vs] if r.flag else [vs])
            return s.model_copy(update={**base, "action": StepAction.RUN, "ramp": None,
                                        "args": args, "replace_args": True, "params": {}})
        return s.model_copy(update={**base, "action": StepAction.TUNE, "ramp": None,
                                    "params": {r.param: v}})
    piece = r.model_copy(update={"start": values[0], "stop": values[-1], "steps": len(values) - 1,
                                 "step": None, "hold_s": None,
                                 "duration_s": len(values) * float(hold_s),
                                 "include_first": True, "include_last": True})
    return s.model_copy(update={**base, "ramp": piece})


# ══════════════════════════════════════════════════════════════════════════════
# Panic
# ══════════════════════════════════════════════════════════════════════════════

class PanicResult(BaseModel):
    unit_id: str
    tasks_stopped: List[str]
    events_cancelled: List[str]
    runs_aborted: List[str]
    at: str


# ══════════════════════════════════════════════════════════════════════════════
# Plans (client-only: a cross-unit choreography)
# ══════════════════════════════════════════════════════════════════════════════
#
# A Plan groups sequences from several units so they can be armed together at one
# shared on-air time. Unlike sequences/tasks, plans live only in the GUI (there is
# no agent Plan store); arming a plan fans out one arm per item, stamped with the
# plan's id/name so the resulting runs can be regrouped. Each item may carry
# per-step parameter overrides (StepOverride) so a plan can run a unit's sequence
# with different task parameters than its stored definition.

class PlanItem(BaseModel):
    hostname: str                      # the unit this item arms (Fleet key)
    unit_label: str = ""               # unit_id for display (cached; may go stale)
    sequence_id: str                   # the source sequence this item was seeded from
    sequence_name: str = ""            # cached for display
    # The plan-local copy of the sequence's steps — its own task timing and
    # parameters, edited in the plan without touching the unit's stored sequence.
    # Armed via ArmSequenceRequest.steps. Empty means "use the stored sequence as
    # defined" (older plans, and the legacy per-arg overrides below).
    steps: List[SequenceStep] = []
    overrides: List[StepOverride] = []   # legacy per-arg overrides (steps-less items)
    # Placement on the plan timeline, relative to the plan's anchors (not absolute
    # times — those are set when a plan is scheduled). on_air_offset_s shifts this
    # sequence's on-air away from the plan's on-air (T0); off_air_offset_s shifts
    # its off-air away from the plan's off-air (T_end).
    on_air_offset_s: float = 0.0
    off_air_offset_s: float = 0.0
    # ── RF-fault RECOVERY Phase 3 — the item's auto-restart policy override. "" (default) =
    # INHERIT the seeded sequence's `recovery_policy`/`recovery_mode`; a non-empty value
    # overrides it (a plan may want one unit's sequence to auto-restart and another's not).
    # Blank-means-inherit keeps an older plan (no field) tracking its sequences' policy.
    recovery_policy: str = ""
    recovery_mode: str = ""


class Plan(BaseModel):
    id: str
    name: str
    description: str = ""
    items: List[PlanItem] = []


class ScheduledPlan(BaseModel):
    """A plan placed on the timeline at an absolute on-air window. start is the
    plan's on-air (T0), stop its off-air (T_end) — both absolute local ISO-8601.
    Client-only, like plans; execution (arming at the time) is a later step."""
    id: str
    plan_id: str = ""                   # the library plan this slot was seeded from
    plan_name: str = ""                 # cached for display if the plan is gone
    start: str                          # ISO-8601 local datetime — on-air (T0)
    stop: str                           # ISO-8601 local datetime — off-air (T_end)
    # An optional per-slot COPY of the plan. When set it is this slot's source of
    # truth — edited here without touching the library plan or any other slot that
    # scheduled the same plan. None means "follow the library plan by plan_id" (the
    # default, and every pre-existing entry).
    plan: Optional[Plan] = None
    # A REFERENCE (note) entry: an external test the team does NOT transmit for,
    # placed on the timeline for situational awareness only. It carries just a name
    # (plan_name), a description, and the time slot — no plan, no unit, never armable.
    # reference=False (the default) is an ordinary scheduled plan.
    reference: bool = False
    description: str = ""               # free-text note (reference entries only)


# ══════════════════════════════════════════════════════════════════════════════
# Library (the shared definition set, replicated identically to every unit)
# ══════════════════════════════════════════════════════════════════════════════
#
# The client keeps one canonical library of scripts, tasks, and sequences. It is
# the authoring source — the sequence/plan editors read from it, so a plan can be
# built with no unit connected — and (in a later phase) it is deployed to every
# unit so they all hold the same definitions. Per-unit differences are parameters,
# and those live in plans, not here.

class LibraryScript(BaseModel):
    name: str                          # script filename, e.g. "freq.py" — its identity
    content: str = ""                  # the script's source (for upload/edit/deploy)
    params: List[dict] = []            # argparse param schema (/scripts/{name}/params)
    types: List[str] = []              # unit types this script targets; empty = shared/all
    folder: str = ""                   # organizational folder (a real subdir on the unit at
                                       # deploy); "" = library root. The name stays the
                                       # identity, so moving folders never changes references.


class Library(BaseModel):
    scripts: List[LibraryScript] = []
    tasks: List[TaskConfig] = []
    sequences: List[Sequence] = []
    folders: List[str] = []            # declared folder paths, so an empty folder persists


class DeployLibraryResult(BaseModel):
    """What a unit's PUT /library changed. *_skipped fields hold definitions the
    deploy left in place because they were in use (a running task, a sequence with
    an active run) — nothing on air is ever stopped."""
    scripts_written: List[str] = []
    scripts_deleted: List[str] = []
    tasks_reload: dict = {}
    tasks_skipped: List[str] = []
    sequences_upserted: List[str] = []
    sequences_deleted: List[str] = []
    sequences_skipped: List[str] = []
    # Set client-side after the deploy (not part of the agent's PUT /library response):
    # the component-catalog outcome for this unit — see component_catalog.plan_unit_deploy.
    components: dict = {}
    # Absolute --power levels this unit can't produce (it clips them at transmit) — see
    # power_scan.power_out_of_range. Each: {where, dbm, limit, side}.
    power_warnings: list = []
    # --amplitude values that differ from what the calibration curve assumes (power scales
    # with amplitude) — see power_scan.amplitude_mismatch. Each: {where, amp, cal_amp}.
    amplitude_warnings: list = []


def scoped_library(library: "Library", unit_type: str) -> "Library":
    """The slice of `library` that applies to a unit of `unit_type`: scripts, tasks,
    and sequences whose `types` is empty (shared) or includes `unit_type`. This is
    what actually gets deployed to a unit, so a unit only ever holds definitions
    meant for its kind."""
    return Library(
        scripts=[s for s in library.scripts if applies_to_type(s.types, unit_type)],
        tasks=[t for t in library.tasks if applies_to_type(t.types, unit_type)],
        sequences=[q for q in library.sequences if applies_to_type(q.types, unit_type)],
        folders=list(library.folders),   # folders are cross-type; keep empty ones too
    )