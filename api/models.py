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
# Enums
# ══════════════════════════════════════════════════════════════════════════════

class ProcessState(str, Enum):
    STOPPED  = "stopped"
    STARTING = "starting"
    RUNNING  = "running"
    STOPPING = "stopping"
    CRASHED  = "crashed"


class EventState(str, Enum):
    ARMED     = "armed"
    RUNNING   = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED   = "aborted"


class SequenceState(str, Enum):
    ARMED     = "armed"
    RUNNING   = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED   = "aborted"


class StepAction(str, Enum):
    START = "start"   # launch a long-running task (paired with a STOP)
    STOP  = "stop"    # stop a long-running task
    RUN   = "run"     # fire-and-exit one-shot: launch, self-terminates, no stop
    TUNE  = "tune"    # retune a running task's live parameters (see SequenceStep.params)
    RAMP  = "ramp"    # sweep one live parameter over time (expands to tunes on the unit)


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
    hold_s: Optional[float] = None
    duration_s: Optional[float] = None
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
    resumable: bool = False
    resume_offset_mode: str = "arg"
    resume_offset_flag: str = "--start-offset"
    resume_offset_env: str = "SDR_START_OFFSET"


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


class StartRequest(BaseModel):
    env_overrides: Dict[str, str] = {}
    args: List[str] = []
    # When False, args are APPENDED to the task's configured command. When True,
    # args REPLACE the task's trailing args — the launch becomes
    # [interpreter, script, *args] — so a form can fully specify the parameters
    # for one run without touching the deployed task definition. Mirrors the
    # agent's StartRequest.replace_args.
    replace_args: bool = False


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
    clock_synced: Optional[bool] = None
    clock_source: str = ""


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
    # sequence_started | sequence_on_air | sequence_step | sequence_off_air | sequence_stopped | sequence_aborted | sequence_modified
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
    anchor: str = "start"              # "start" | "stop" | "both" (ramp)
    offset_s: float
    # "both"-anchored ramp: off-air-side inset (≤ 0). Fills [on-air+offset_s, off-air+offset_end_s].
    offset_end_s: Optional[float] = None
    action: StepAction
    task_name: str
    args: List[str] = []               # CLI args for this step's start/run
    replace_args: bool = False         # True: args are the complete set (replace task defaults)
    inject_resume_offset: bool = False
    # TUNE step: live-parameter values to apply to the running task, {name: value}.
    params: Dict[str, Any] = {}
    # RAMP step: the parametric sweep (expanded into tunes on the unit at arm time).
    ramp: Optional[RampSpec] = None


class Sequence(BaseModel):
    id: str
    name: str
    description: str = ""
    steps: List[SequenceStep]


class CreateSequenceRequest(BaseModel):
    name: str
    description: str = ""
    steps: List[SequenceStep]


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


class PatchSequenceRunRequest(BaseModel):
    on_air_end: str


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
    plan_id: str                        # the library plan this slot was seeded from
    plan_name: str = ""                 # cached for display if the plan is gone
    start: str                          # ISO-8601 local datetime — on-air (T0)
    stop: str                           # ISO-8601 local datetime — off-air (T_end)
    # An optional per-slot COPY of the plan. When set it is this slot's source of
    # truth — edited here without touching the library plan or any other slot that
    # scheduled the same plan. None means "follow the library plan by plan_id" (the
    # default, and every pre-existing entry).
    plan: Optional[Plan] = None


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
    name: str                          # script filename, e.g. "freq.py"
    content: str = ""                  # the script's source (for upload/edit/deploy)
    params: List[dict] = []            # argparse param schema (/scripts/{name}/params)


class Library(BaseModel):
    scripts: List[LibraryScript] = []
    tasks: List[TaskConfig] = []
    sequences: List[Sequence] = []


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