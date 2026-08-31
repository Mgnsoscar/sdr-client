"""
library_sync — populate the shared library from a connected unit, and compare a
unit's library to the canonical one (drift detection).

pull_library snapshots a unit's scripts (+ their parameter schemas), tasks, and
sequences into a Library. It runs off the GUI thread (several sequential agent
calls) and returns the assembled Library; the caller stores it. Because the fleet
is meant to hold one identical library, pulling from any one unit is enough to
seed a fresh PC.

diff_library compares the canonical library against a unit's snapshot and reports
what would change if it were deployed — so the UI can flag drift and reconcile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import yaml

from api import models as m

logger = logging.getLogger(__name__)


# ── Drift detection ────────────────────────────────────────────────────────────

def _task_fingerprint(t: m.TaskConfig) -> tuple:
    """The meaningful, deploy-relevant fields of a task (ignores schema-default
    noise so two equivalent definitions compare equal)."""
    return (t.description, tuple(t.command), tuple(sorted(t.env.items())),
            bool(t.autostart), bool(t.restart_on_crash))


def _seq_fingerprint(s: m.Sequence) -> tuple:
    return (s.name, s.description,
            tuple(tuple(st.model_dump(mode="json").items()) for st in s.steps))


def _norm_script(content: str) -> str:
    """Compare script bodies ignoring cosmetic line-ending differences: a unit
    reads files back with universal newlines (CRLF→LF), so a Windows-edited script
    would otherwise always look 'changed' against the unit's copy. Normalize line
    endings to LF and drop a trailing blank line so equal code compares equal."""
    return (content or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _plan_cmp(p: "m.Plan") -> dict:
    d = p.model_dump(mode="json"); d.pop("id", None); return d


def _sched_cmp(s: "m.ScheduledPlan") -> dict:
    d = s.model_dump(mode="json"); d.pop("id", None); return d


@dataclass
class StateDiff:
    """What a unit holds vs. this PC's canonical state — library definitions AND
    the plan + schedule replicas. For each bucket: add = on the PC, missing on the
    unit; change = present on both but different; remove = ON THE UNIT, absent from
    the PC (i.e. the unit has something this PC doesn't — a deploy would delete it,
    a restore would bring it here). Scripts key by filename, tasks by name, and
    sequences / plans / schedule by id (with name maps for display)."""
    scripts_add: List[str] = field(default_factory=list)
    scripts_change: List[str] = field(default_factory=list)
    scripts_remove: List[str] = field(default_factory=list)
    tasks_add: List[str] = field(default_factory=list)
    tasks_change: List[str] = field(default_factory=list)
    tasks_remove: List[str] = field(default_factory=list)
    sequences_add: List[str] = field(default_factory=list)
    sequences_change: List[str] = field(default_factory=list)
    sequences_remove: List[str] = field(default_factory=list)
    seq_names: dict = field(default_factory=dict)
    plans_add: List[str] = field(default_factory=list)
    plans_change: List[str] = field(default_factory=list)
    plans_remove: List[str] = field(default_factory=list)
    plan_names: dict = field(default_factory=dict)
    schedule_add: List[str] = field(default_factory=list)
    schedule_change: List[str] = field(default_factory=list)
    schedule_remove: List[str] = field(default_factory=list)
    sched_names: dict = field(default_factory=dict)

    @property
    def library_in_sync(self) -> bool:
        return not any((self.scripts_add, self.scripts_change, self.scripts_remove,
                        self.tasks_add, self.tasks_change, self.tasks_remove,
                        self.sequences_add, self.sequences_change, self.sequences_remove))

    @property
    def in_sync(self) -> bool:
        return self.library_in_sync and not any((
            self.plans_add, self.plans_change, self.plans_remove,
            self.schedule_add, self.schedule_change, self.schedule_remove))

    @property
    def unit_has_extra(self) -> bool:
        """True when the unit holds plans/schedule this PC doesn't — the case where
        a Restore would recover something, and a Deploy would destroy it."""
        return bool(self.plans_remove or self.schedule_remove)

    def summary(self) -> str:
        """A compact 'why it's drifted' line, or 'in sync'."""
        if self.in_sync:
            return "in sync"
        def part(label, add, chg, rem):
            bits = []
            if add: bits.append(f"+{len(add)}")
            if chg: bits.append(f"~{len(chg)}")
            if rem: bits.append(f"-{len(rem)}")
            return f"{label} {'/'.join(bits)}" if bits else ""
        parts = [part("scripts", self.scripts_add, self.scripts_change, self.scripts_remove),
                 part("tasks", self.tasks_add, self.tasks_change, self.tasks_remove),
                 part("sequences", self.sequences_add, self.sequences_change,
                      self.sequences_remove),
                 part("plans", self.plans_add, self.plans_change, self.plans_remove),
                 part("schedule", self.schedule_add, self.schedule_change,
                      self.schedule_remove)]
        return "  ·  ".join(p for p in parts if p)


# Back-compat alias: the diff type used to be library-only.
LibraryDiff = StateDiff


def diff_library(canonical: m.Library, unit: m.Library) -> StateDiff:
    """Compare the canonical library to a unit's library snapshot (definitions
    only). Pure — no I/O."""
    d = StateDiff()

    # Scripts — compare by filename on content (line-ending noise ignored) AND
    # folder, so filing a script into a folder shows as drift until it's deployed.
    can_s = {s.name: (_norm_script(s.content), (s.folder or "")) for s in canonical.scripts}
    unit_s = {s.name: (_norm_script(s.content), (s.folder or "")) for s in unit.scripts}
    for name, fp in can_s.items():
        if name not in unit_s:
            d.scripts_add.append(name)
        elif unit_s[name] != fp:
            d.scripts_change.append(name)
    d.scripts_remove = [n for n in unit_s if n not in can_s]

    # Tasks — compare by name on the deploy-relevant fingerprint.
    can_t = {t.name: _task_fingerprint(t) for t in canonical.tasks}
    unit_t = {t.name: _task_fingerprint(t) for t in unit.tasks}
    for name, fp in can_t.items():
        if name not in unit_t:
            d.tasks_add.append(name)
        elif unit_t[name] != fp:
            d.tasks_change.append(name)
    d.tasks_remove = [n for n in unit_t if n not in can_t]

    # Sequences — compare by id (the shared reference) on name+steps.
    can_seq = {s.id: s for s in canonical.sequences}
    unit_seq = {s.id: s for s in unit.sequences}
    for sid, s in can_seq.items():
        d.seq_names[sid] = s.name or sid
        if sid not in unit_seq:
            d.sequences_add.append(sid)
        elif _seq_fingerprint(unit_seq[sid]) != _seq_fingerprint(s):
            d.sequences_change.append(sid)
    for sid, s in unit_seq.items():
        if sid not in can_seq:
            d.sequences_remove.append(sid)
            d.seq_names.setdefault(sid, s.name or sid)
    return d


def diff_state(canonical_library: m.Library, canonical_plans: List["m.Plan"],
               canonical_schedule: List["m.ScheduledPlan"],
               unit: "UnitSnapshot") -> StateDiff:
    """Full comparison of a unit's snapshot to this PC's canonical state — library
    definitions plus the plan and schedule replicas. Pure — no I/O."""
    d = diff_library(canonical_library, unit.library)

    can_p = {p.id: p for p in canonical_plans}
    unit_p = {p.id: p for p in unit.plans}
    for pid, p in can_p.items():
        d.plan_names[pid] = p.name or pid
        if pid not in unit_p:
            d.plans_add.append(pid)
        elif _plan_cmp(unit_p[pid]) != _plan_cmp(p):
            d.plans_change.append(pid)
    for pid, p in unit_p.items():
        if pid not in can_p:
            d.plans_remove.append(pid)
            d.plan_names.setdefault(pid, p.name or pid)

    can_s = {s.id: s for s in canonical_schedule}
    unit_s = {s.id: s for s in unit.schedule}
    for sid, s in can_s.items():
        d.sched_names[sid] = s.plan_name or sid
        if sid not in unit_s:
            d.schedule_add.append(sid)
        elif _sched_cmp(unit_s[sid]) != _sched_cmp(s):
            d.schedule_change.append(sid)
    for sid, s in unit_s.items():
        if sid not in can_s:
            d.schedule_remove.append(sid)
            d.sched_names.setdefault(sid, s.plan_name or sid)
    return d


def parse_tasks_yaml(text) -> List[m.TaskConfig]:
    """tasks.yaml document → TaskConfig list (unknown keys ignored)."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    out: List[m.TaskConfig] = []
    for entry in (doc.get("tasks") or []):
        if isinstance(entry, dict) and entry.get("name") and entry.get("command"):
            try:
                out.append(m.TaskConfig(**entry))
            except Exception as exc:  # noqa: BLE001 — skip a malformed task
                logger.warning("Skipping malformed task '%s': %s", entry.get("name"), exc)
    return out


def pull_library(client) -> m.Library:
    """Snapshot one unit's scripts/tasks/sequences into a Library. Worker thread —
    performs several agent calls; individual failures degrade gracefully."""
    scripts: List[m.LibraryScript] = []
    try:
        names = client.list_scripts()
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: list_scripts failed: %s", exc)
        names = []
    for name in names:
        try:
            content = client.get_script(name)
        except Exception as exc:  # noqa: BLE001 — keep the script even without its body
            logger.warning("pull_library: get_script('%s') failed: %s", name, exc)
            content = ""
        try:
            params = (client.get_script_params(name) or {}).get("params", [])
        except Exception:  # noqa: BLE001 — a script without a schema still belongs
            params = []
        scripts.append(m.LibraryScript(
            name=name, content=content if isinstance(content, str) else "", params=params))

    try:
        tasks = parse_tasks_yaml(client.get_tasks_yaml())
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: get_tasks_yaml failed: %s", exc)
        tasks = []

    try:
        sequences = client.list_sequences()
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: list_sequences failed: %s", exc)
        sequences = []

    return m.Library(scripts=scripts, tasks=tasks, sequences=list(sequences))


@dataclass
class UnitSnapshot:
    """Everything a fresh PC needs from one unit to rebuild: the shared library
    plus the plan + schedule replicas the unit was holding."""
    library: m.Library
    plans: List[m.Plan] = field(default_factory=list)
    schedule: List[m.ScheduledPlan] = field(default_factory=list)


def snapshot_unit(client) -> UnitSnapshot:
    """A unit's current library + plan + schedule replicas, via the single-call
    endpoints (GET /library, /plans, /schedule). Used for drift checks. Worker
    thread; any part that fails degrades to empty so one bad call doesn't sink the
    check."""
    try:
        library = client.get_library()
    except Exception as exc:  # noqa: BLE001
        logger.error("snapshot_unit: get_library failed: %s", exc)
        library = m.Library()
    try:
        plans = list(client.get_plans())
    except Exception as exc:  # noqa: BLE001
        logger.error("snapshot_unit: get_plans failed: %s", exc)
        plans = []
    try:
        schedule = list(client.get_schedule())
    except Exception as exc:  # noqa: BLE001
        logger.error("snapshot_unit: get_schedule failed: %s", exc)
        schedule = []
    return UnitSnapshot(library=library, plans=plans, schedule=schedule)


def pull_everything(client) -> UnitSnapshot:
    """Snapshot a unit's library AND its plan/schedule replicas. Worker thread —
    used to restore a fresh PC from any one reachable unit (all units hold the
    same replicas, so one is enough). Each part degrades to empty on failure."""
    library = pull_library(client)
    try:
        plans = list(client.get_plans())
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_everything: get_plans failed: %s", exc)
        plans = []
    try:
        schedule = list(client.get_schedule())
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_everything: get_schedule failed: %s", exc)
        schedule = []
    return UnitSnapshot(library=library, plans=plans, schedule=schedule)
