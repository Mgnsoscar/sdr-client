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


@dataclass
class LibraryDiff:
    """What deploying the canonical library to a unit would change. in_sync is True
    when every bucket is empty. add = on canonical, missing on the unit; change =
    present on both but different; remove = on the unit, absent from canonical
    (only pruned away on a prune deploy). Scripts are keyed by filename, tasks by
    name, sequences by id (with a name map for display)."""
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

    @property
    def in_sync(self) -> bool:
        return not any((self.scripts_add, self.scripts_change, self.scripts_remove,
                        self.tasks_add, self.tasks_change, self.tasks_remove,
                        self.sequences_add, self.sequences_change, self.sequences_remove))

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
                      self.sequences_remove)]
        return "  ·  ".join(p for p in parts if p)


def diff_library(canonical: m.Library, unit: m.Library) -> LibraryDiff:
    """Compare the canonical library to a unit's snapshot. Pure — no I/O."""
    d = LibraryDiff()

    # Scripts — compare by filename on content.
    can_s = {s.name: s.content for s in canonical.scripts}
    unit_s = {s.name: s.content for s in unit.scripts}
    for name, content in can_s.items():
        if name not in unit_s:
            d.scripts_add.append(name)
        elif unit_s[name] != content:
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
