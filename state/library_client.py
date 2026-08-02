"""
LibraryClient — an AgentClient-shaped adapter over the local LibraryStore.

The unit-card panels and editors (tasks, sequences, scripts) call a small surface
of AgentClient methods. This adapter implements that same surface against the
shared library on disk instead of a live unit, so those exact panels/editors can
author the library offline. It's duck-typed (no inheritance) and returns the same
api.models types the real client returns.

Within-library integrity is enforced here: you can't create a sequence that
references an unknown task, delete a task a sequence still uses, or delete a
script a task still uses — the panel surfaces the error just as it would an
agent 400.

Run-time methods (start/stop/runs) don't apply to a library; the Library panels
hide those controls, and the methods raise if called anyway.
"""
from __future__ import annotations

import secrets
from typing import List

import yaml

from api import models as m
from .library_store import LibraryStore, _script_of_command


class LibraryError(Exception):
    """Raised for a library operation the panel should surface (e.g. a bad delete)."""


class LibraryClient:
    def __init__(self, store: LibraryStore):
        self._store = store
        self.unit_id = "Library"
        self.hostname = "__library__"

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def list_tasks(self) -> List[m.ProcessStatus]:
        return [m.ProcessStatus(name=t.name, description=t.description,
                                state=m.ProcessState.STOPPED)
                for t in self._store.tasks()]

    def get_tasks_yaml(self) -> str:
        return yaml.safe_dump({"tasks": [t.model_dump() for t in self._store.tasks()]},
                              sort_keys=False)

    def create_task(self, spec: dict) -> dict:
        task = m.TaskConfig(**spec)
        if self._store.get_task(task.name) is not None:
            raise LibraryError(f"a task named '{task.name}' already exists in the library")
        self._store.upsert_task(task)
        return task.model_dump()

    def update_task(self, name: str, spec: dict) -> dict:
        task = m.TaskConfig(**spec)
        if task.name != name:                       # a rename
            if self._store.get_task(task.name) is not None:
                raise LibraryError(f"a task named '{task.name}' already exists")
            self._store.delete_task(name)
            # any sequence step still pointing at the old name would now dangle —
            # but the editors don't rename in place, so this stays simple.
        self._store.upsert_task(task)
        return task.model_dump()

    def delete_task(self, name: str) -> dict:
        used = self._store.sequences_using_task(name)
        if used:
            raise LibraryError(
                f"task '{name}' is used by sequence(s): {', '.join(used)} — "
                f"remove it from them first")
        self._store.delete_task(name)
        return {"deleted": name}

    # ── Sequences ──────────────────────────────────────────────────────────────

    def list_sequences(self) -> List[m.Sequence]:
        return self._store.sequences()

    def get_sequence(self, seq_id: str) -> m.Sequence:
        seq = self._store.get_sequence(seq_id)
        if seq is None:
            raise LibraryError(f"unknown sequence: {seq_id}")
        return seq

    def _validate_steps(self, steps: List[m.SequenceStep]) -> None:
        known = set(self._store.task_names())
        unknown = sorted({s.task_name for s in steps if s.task_name not in known})
        if unknown:
            raise LibraryError("sequence references task(s) not in the library: "
                               + ", ".join(unknown))

    def create_sequence(self, request: m.CreateSequenceRequest) -> m.Sequence:
        self._validate_steps(request.steps)
        seq = m.Sequence(id="seq_" + secrets.token_hex(4), name=request.name,
                         description=request.description, steps=list(request.steps))
        self._store.upsert_sequence(seq)
        return seq

    def update_sequence(self, seq_id: str, request: m.CreateSequenceRequest) -> m.Sequence:
        self._validate_steps(request.steps)
        seq = m.Sequence(id=seq_id, name=request.name, description=request.description,
                         steps=list(request.steps))
        self._store.upsert_sequence(seq)
        return seq

    def delete_sequence(self, seq_id: str) -> dict:
        self._store.delete_sequence(seq_id)
        return {"deleted": seq_id}

    # ── Scripts ────────────────────────────────────────────────────────────────

    def list_scripts(self) -> List[str]:
        return [s.name for s in self._store.scripts()]

    def get_script(self, name: str) -> str:
        s = self._store.get_script(name)
        if s is None:
            raise LibraryError(f"unknown script: {name}")
        return s.content

    def upload_script(self, filename: str, content: bytes) -> dict:
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
        existing = self._store.get_script(filename)
        # Keep any known parameter schema on re-upload; offline we can't re-derive
        # it (that's the agent's static introspection). A pull/deploy refreshes it.
        params = existing.params if existing is not None else []
        self._store.upsert_script(m.LibraryScript(name=filename, content=text, params=params))
        return {"uploaded": filename}

    def delete_script(self, name: str) -> dict:
        users = [t.name for t in self._store.tasks()
                 if _script_of_command(t.command) == name]
        if users:
            raise LibraryError(
                f"script '{name}' is used by task(s): {', '.join(users)} — "
                f"change or remove them first")
        # LibraryStore has no delete_script helper on purpose (scripts are small);
        # replace the list directly.
        lib = self._store.library()
        lib.scripts = [s for s in lib.scripts if s.name != name]
        self._store.replace(lib)
        return {"deleted": name}

    def get_script_params(self, name: str) -> dict:
        return {"params": self._store.script_params(name)}

    # ── Not applicable to a library ────────────────────────────────────────────

    def list_sequence_runs(self) -> list:
        return []

    def start_task(self, *a, **k):
        raise LibraryError("the library isn't a running unit — start tasks from a unit")

    def stop_task(self, *a, **k):
        raise LibraryError("the library isn't a running unit — stop tasks from a unit")
