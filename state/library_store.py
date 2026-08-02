"""
LibraryStore — the client's canonical shared definition library.

One library of scripts, tasks, and sequences, held identically for the whole
fleet (per-unit differences are parameters, and those live in plans). It is the
authoring source for the sequence and plan editors, so a plan can be built with
no unit connected, and — in a later phase — the set that gets deployed to every
unit.

Persisted to library.json next to the client. Atomic writes (temp file +
replace). Within-library integrity is checked here: a sequence step may only
reference a task the library holds, and a task's script (if it names a .py) must
be a known script — so the library can never describe something a unit couldn't
run once deployed. Nothing here is Qt-aware.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from api import models as m

logger = logging.getLogger(__name__)

DEFAULT_LIBRARY_FILE = Path(__file__).resolve().parent.parent / "library.json"


def _script_of_command(command: List[str]) -> str:
    """The basename of the first .py element of a task command (its script), or ''."""
    for a in command:
        if isinstance(a, str) and a.endswith(".py"):
            return a.rsplit("/", 1)[-1]
    return ""


class LibraryStore:
    def __init__(self, path: Path = DEFAULT_LIBRARY_FILE):
        self._path = Path(path)
        self._lib = m.Library()
        self.load()

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> m.Library:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._lib = m.Library()
            return self._lib
        try:
            self._lib = m.Library(**json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read library from %s: %s", self._path, exc)
            self._lib = m.Library()
        return self._lib

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._lib.model_dump(), indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ── Whole-library access ───────────────────────────────────────────────────

    def library(self) -> m.Library:
        return self._lib.model_copy(deep=True)

    def replace(self, lib: m.Library) -> None:
        """Swap the whole library (e.g. after a pull from a unit) and persist."""
        self._lib = lib.model_copy(deep=True)
        self._save()

    # ── Accessors ──────────────────────────────────────────────────────────────

    def scripts(self) -> List[m.LibraryScript]:
        return list(self._lib.scripts)

    def tasks(self) -> List[m.TaskConfig]:
        return list(self._lib.tasks)

    def sequences(self) -> List[m.Sequence]:
        return list(self._lib.sequences)

    def task_names(self) -> List[str]:
        return [t.name for t in self._lib.tasks]

    def get_script(self, name: str) -> Optional[m.LibraryScript]:
        return next((s for s in self._lib.scripts if s.name == name), None)

    def get_task(self, name: str) -> Optional[m.TaskConfig]:
        return next((t for t in self._lib.tasks if t.name == name), None)

    def get_sequence(self, seq_id: str) -> Optional[m.Sequence]:
        return next((s for s in self._lib.sequences if s.id == seq_id), None)

    def script_params(self, script_name: str) -> List[dict]:
        s = self.get_script(script_name)
        return list(s.params) if s is not None else []

    def params_for_task(self, task_name: str) -> List[dict]:
        """Parameter schema for a task, via its command's script (offline lookup)."""
        t = self.get_task(task_name)
        if t is None:
            return []
        return self.script_params(_script_of_command(t.command))

    # ── Mutations (keyed by name / id) ─────────────────────────────────────────

    def upsert_script(self, script: m.LibraryScript) -> None:
        self._lib.scripts = [s for s in self._lib.scripts if s.name != script.name]
        self._lib.scripts.append(script)
        self._save()

    def upsert_task(self, task: m.TaskConfig) -> None:
        self._lib.tasks = [t for t in self._lib.tasks if t.name != task.name]
        self._lib.tasks.append(task)
        self._save()

    def upsert_sequence(self, seq: m.Sequence) -> None:
        self._lib.sequences = [s for s in self._lib.sequences if s.id != seq.id]
        self._lib.sequences.append(seq)
        self._save()

    def delete_task(self, name: str) -> bool:
        before = len(self._lib.tasks)
        self._lib.tasks = [t for t in self._lib.tasks if t.name != name]
        if len(self._lib.tasks) != before:
            self._save()
            return True
        return False

    def delete_sequence(self, seq_id: str) -> bool:
        before = len(self._lib.sequences)
        self._lib.sequences = [s for s in self._lib.sequences if s.id != seq_id]
        if len(self._lib.sequences) != before:
            self._save()
            return True
        return False

    # ── Within-library integrity ───────────────────────────────────────────────

    def sequences_using_task(self, task_name: str) -> List[str]:
        """Names of sequences whose steps reference a task (so a delete can warn)."""
        out = []
        for s in self._lib.sequences:
            if any(step.task_name == task_name for step in s.steps):
                out.append(s.name or s.id)
        return out

    def check_integrity(self) -> List[str]:
        """Return human-readable problems: sequence steps pointing at unknown tasks,
        or tasks whose script isn't in the library. Empty list == consistent."""
        problems: List[str] = []
        task_names = set(self.task_names())
        script_names = {s.name for s in self._lib.scripts}
        for t in self._lib.tasks:
            script = _script_of_command(t.command)
            if script and script not in script_names:
                problems.append(f"task '{t.name}' uses script '{script}', which isn't in the library")
        for s in self._lib.sequences:
            for step in s.steps:
                if step.task_name not in task_names:
                    problems.append(
                        f"sequence '{s.name or s.id}' references unknown task '{step.task_name}'")
        return problems
