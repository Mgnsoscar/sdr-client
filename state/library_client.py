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
from api.argspec import extract_params
from .library_store import LibraryStore, _script_of_command


class LibraryError(Exception):
    """Raised for a library operation the panel should surface (e.g. a bad delete)."""


class LibraryClient:
    def __init__(self, store: LibraryStore):
        self._store = store
        self.unit_id = "Library"
        self.hostname = "__library__"

    @property
    def store(self) -> LibraryStore:
        return self._store

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
                         description=request.description, steps=list(request.steps),
                         types=list(request.types))
        self._store.upsert_sequence(seq)
        return seq

    def update_sequence(self, seq_id: str, request: m.CreateSequenceRequest) -> m.Sequence:
        self._validate_steps(request.steps)
        seq = m.Sequence(id=seq_id, name=request.name, description=request.description,
                         steps=list(request.steps), types=list(request.types))
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
        # Store LF-normalized so the PC's copy matches what a unit reports back
        # (units read scripts with universal newlines) — otherwise a Windows-edited
        # script would always show as drifted right after a deploy.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Derive the parameter schema right here (same static, no-execution
        # introspection the agent uses, vendored into the client), so parameter
        # forms auto-generate offline the moment a script is added or edited.
        try:
            params = (extract_params(text) or {}).get("params", [])
        except Exception:  # noqa: BLE001 — a script we can't parse still uploads
            params = []
        # Preserve an existing script's unit-type scope AND folder across a content
        # edit — re-uploading (Save) would otherwise reset them.
        prev = self._store.get_script(filename)
        types = list(prev.types) if prev is not None else []
        folder = prev.folder if prev is not None else ""
        self._store.upsert_script(
            m.LibraryScript(name=filename, content=text, params=params,
                            types=types, folder=folder))
        return {"uploaded": filename}

    def get_script_types(self, name: str) -> List[str]:
        s = self._store.get_script(name)
        return list(s.types) if s is not None else []

    def set_script_types(self, name: str, types: List[str]) -> dict:
        s = self._store.get_script(name)
        if s is None:
            raise LibraryError(f"unknown script: {name}")
        s.types = list(types)
        self._store.upsert_script(s)
        return {"name": name, "types": list(types)}

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

    # ── Folders (organizational; a real subdir on the unit at deploy) ───────────

    def list_folders(self) -> List[str]:
        return self._store.folders()

    def get_script_folder(self, name: str) -> str:
        s = self._store.get_script(name)
        return s.folder if s is not None else ""

    def set_script_folder(self, name: str, folder: str) -> dict:
        if self._store.get_script(name) is None:
            raise LibraryError(f"unknown script: {name}")
        self._store.set_script_folder(name, folder)
        return {"name": name, "folder": folder.strip().strip("/")}

    def create_folder(self, path: str) -> dict:
        self._store.add_folder(path)
        return {"folder": path.strip().strip("/")}

    def rename_folder(self, old: str, new: str) -> dict:
        self._store.rename_folder(old, new)
        return {"old": old, "new": new.strip().strip("/")}

    def delete_folder(self, path: str, move_to: str = "") -> dict:
        self._store.delete_folder(path, move_to)
        return {"deleted": path}

    def get_script_params(self, name: str) -> dict:
        # Re-extract from the stored source so the schema always reflects the
        # current extractor — a script added before a new field was understood
        # (e.g. `live`) still reports it, without needing a re-import. Fall back to
        # the params captured at upload time if the source can't be parsed.
        s = self._store.get_script(name)
        if s is not None and s.content:
            try:
                spec = extract_params(s.content) or {}
                out = {"params": spec.get("params", [])}
                if spec.get("calibration_signal"):     # opt-in signal for the task's env
                    out["calibration_signal"] = spec["calibration_signal"]
                if spec.get("calibration_freq_param"):  # freq field the --power range folds at
                    out["calibration_freq_param"] = spec["calibration_freq_param"]
                return out
            except Exception:  # noqa: BLE001 — a script we can't parse: use stored
                pass
        return {"params": self._store.script_params(name)}

    # ── Not applicable to a library ────────────────────────────────────────────

    def list_sequence_runs(self) -> list:
        return []

    def start_task(self, *a, **k):
        raise LibraryError("the library isn't a running unit — start tasks from a unit")

    def stop_task(self, *a, **k):
        raise LibraryError("the library isn't a running unit — stop tasks from a unit")
