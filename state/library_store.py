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
from paths import data_file

logger = logging.getLogger(__name__)

DEFAULT_LIBRARY_FILE = data_file("library.json")


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

    def merge(self, other: m.Library) -> dict:
        """Add definitions from `other` that this library doesn't already have
        (scripts by name, tasks by name, sequences by id) — keeping everything
        local. Used by a non-destructive Restore. As a special case, a local script
        with an EMPTY body is a hole, not a real value: if `other` has content for
        it, adopt that content (this recovers script bodies lost to an earlier bug
        without overwriting anything real). Returns added + refreshed counts."""
        added = {"scripts": 0, "tasks": 0, "sequences": 0, "scripts_refreshed": 0}
        by_name = {s.name: s for s in self._lib.scripts}
        for s in other.scripts:
            local = by_name.get(s.name)
            if local is None:
                self._lib.scripts.append(s.model_copy(deep=True))
                added["scripts"] += 1
            elif not (local.content or "").strip() and (s.content or "").strip():
                local.content = s.content
                local.params = list(s.params)
                added["scripts_refreshed"] += 1
        have_t = {t.name for t in self._lib.tasks}
        for t in other.tasks:
            if t.name not in have_t:
                self._lib.tasks.append(t.model_copy(deep=True))
                added["tasks"] += 1
        have_q = {q.id for q in self._lib.sequences}
        for q in other.sequences:
            if q.id not in have_q:
                self._lib.sequences.append(q.model_copy(deep=True))
                added["sequences"] += 1
        # Union declared folders so an empty folder from either side survives a merge.
        self._lib.folders = sorted({f for f in (self._lib.folders + list(other.folders)) if f})
        self._save()
        return added

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

    # ── Folders (organizational; a real subdir on the unit at deploy) ───────────

    def folders(self) -> List[str]:
        """Every folder that exists: those declared (so an empty one persists) plus
        any a script sits in. Sorted, blanks dropped (root has no folder name)."""
        used = {s.folder for s in self._lib.scripts if s.folder}
        return sorted(used | {f for f in self._lib.folders if f})

    def add_folder(self, path: str) -> None:
        path = path.strip().strip("/")
        if path and path not in self._lib.folders:
            self._lib.folders.append(path)
            self._save()

    def rename_folder(self, old: str, new: str) -> None:
        new = new.strip().strip("/")
        if not new or new == old:
            return
        for s in self._lib.scripts:
            if s.folder == old:
                s.folder = new
        self._lib.folders = sorted({new if f == old else f
                                    for f in self._lib.folders if f} | {new})
        self._save()

    def delete_folder(self, path: str, move_to: str = "") -> None:
        """Remove a folder; its scripts move to `move_to` (root by default) — the
        scripts themselves are never deleted here."""
        for s in self._lib.scripts:
            if s.folder == path:
                s.folder = move_to
        self._lib.folders = [f for f in self._lib.folders if f and f != path]
        self._save()

    def set_script_folder(self, name: str, folder: str) -> None:
        folder = folder.strip().strip("/")
        for s in self._lib.scripts:
            if s.name == name:
                s.folder = folder
        if folder and folder not in self._lib.folders:
            self._lib.folders.append(folder)
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
        empty = [s.name for s in self._lib.scripts if not (s.content or "").strip()]
        if empty:
            problems.append(
                f"{len(empty)} script(s) have no content ({', '.join(empty[:3])}"
                f"{'…' if len(empty) > 3 else ''}) — re-upload them or Restore→Merge "
                f"from a unit that has them")
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
