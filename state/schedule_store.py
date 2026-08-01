"""
ScheduleStore — local persistence for the timeline (plans placed at absolute
start/stop times).

Like plans, the schedule is a GUI-only concept for now (no agent schedule store),
so it lives in a JSON file next to the client (schedule.json). This is a tiny CRUD
wrapper: load returns the entries, and every mutation writes the whole list back
atomically (temp file + replace) so a crash mid-write can't corrupt it.

Nothing here is Qt-aware.
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import List, Optional

from api import models as m

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_FILE = Path(__file__).resolve().parent.parent / "schedule.json"


def new_scheduled_id() -> str:
    return "sched_" + secrets.token_hex(4)


class ScheduleStore:
    def __init__(self, path: Path = DEFAULT_SCHEDULE_FILE):
        self._path = Path(path)
        self._entries: List[m.ScheduledPlan] = []
        self.load()

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> List[m.ScheduledPlan]:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._entries = []
            return self._entries
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("schedule", []) if isinstance(data, dict) else data
            self._entries = [m.ScheduledPlan(**e) for e in raw]
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read schedule from %s: %s", self._path, exc)
            self._entries = []
        return self._entries

    def _save(self) -> None:
        data = {"schedule": [e.model_dump() for e in self._entries]}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ── Accessors / mutations ────────────────────────────────────────────────

    def entries(self) -> List[m.ScheduledPlan]:
        return list(self._entries)

    def get(self, entry_id: str) -> Optional[m.ScheduledPlan]:
        return next((e for e in self._entries if e.id == entry_id), None)

    def upsert(self, entry: m.ScheduledPlan) -> m.ScheduledPlan:
        for i, e in enumerate(self._entries):
            if e.id == entry.id:
                self._entries[i] = entry
                self._save()
                return entry
        self._entries.append(entry)
        self._save()
        return entry

    def delete(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        if len(self._entries) != before:
            self._save()
            return True
        return False
