"""Pure helpers for the run/task conflict guards on a single-TX unit.

Two symmetric guards, both grounded in the same facts (which tasks are running, which
sequence/plan runs are active and what tasks they use):

* **Arming over a running task** — a sequence/plan you arm must not collide with a task
  already transmitting on the unit (the agent refuses it anyway; this lets the client offer
  to stop the task and arm instead of just failing).
* **Stopping a task that a run owns** — stopping a task from the Tasks tab while it is part
  of a running sequence/plan should let the operator choose: just that task, or the whole run.

Kept pure (no Qt, no I/O) so the dialogs in the panels wrap these and they test headlessly.
"""
from __future__ import annotations

from typing import List

import api.models as m

# A run is "active" (owns its tasks / conflicts with a new arm) while armed, running, or
# parked at a Hold. Completed/cancelled/aborted runs own nothing.
ACTIVE_RUN_STATES = (m.SequenceState.ARMED, m.SequenceState.RUNNING, m.SequenceState.HOLDING)
# A task occupies the single TX chain while running or spinning up.
RUNNING_TASK_STATES = (m.ProcessState.RUNNING, m.ProcessState.STARTING)


def sequence_task_names(steps) -> List[str]:
    """The distinct task names a sequence's steps reference, in first-seen order."""
    out: List[str] = []
    for s in steps or []:
        t = getattr(s, "task_name", None)
        if t and t not in out:
            out.append(t)
    return out


def running_task_names(statuses, wanted) -> List[str]:
    """Which of ``wanted`` are currently running/starting on the unit, from its ProcessStatus
    list (the poller's fast snapshot). Order follows ``wanted`` (stable for messaging)."""
    running = {getattr(s, "name", None) for s in (statuses or [])
               if getattr(s, "state", None) in RUNNING_TASK_STATES}
    return [t for t in (wanted or []) if t in running]


def active_runs_using_task(runs, task_name: str) -> List["m.SequenceRun"]:
    """Active runs (armed/running/holding) whose steps reference ``task_name`` — the runs that
    would be disrupted by stopping that task."""
    out: List["m.SequenceRun"] = []
    for r in runs or []:
        if getattr(r, "state", None) not in ACTIVE_RUN_STATES:
            continue
        if any(getattr(st, "task_name", None) == task_name
               for st in (getattr(r, "steps", None) or [])):
            out.append(r)
    return out


def run_label(run) -> str:
    """A short human label for a run — ``plan “X”`` when it came from a plan, else
    ``sequence “Y”`` — for the conflict dialogs."""
    plan = getattr(run, "plan_name", "") or getattr(run, "plan_id", "") or ""
    if plan:
        return f"plan “{plan}”"
    seq = getattr(run, "sequence_name", "") or getattr(run, "sequence_id", "") or "?"
    return f"sequence “{seq}”"
