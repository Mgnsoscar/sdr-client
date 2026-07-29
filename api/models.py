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
from typing import Optional, Dict, List
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
    START = "start"
    STOP  = "stop"


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
    agent_version: str
    python_version: str
    tasks: List[str]


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
    anchor: str = "start"              # "start" | "stop"
    offset_s: float
    action: StepAction
    task_name: str
    inject_resume_offset: bool = False


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
    stopped_actual: Optional[str] = None
    resume_offset_s: float = 0.0
    note: str = ""
    steps: List[StepFire] = []
    plan_id: str = ""
    plan_name: str = ""


class ArmSequenceRequest(BaseModel):
    on_air_at: str
    on_air_end: Optional[str] = None
    on_air_duration_s: Optional[float] = None
    open_ended: bool = False
    resume_offset_s: float = 0.0
    note: str = ""
    plan_id: str = ""
    plan_name: str = ""


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