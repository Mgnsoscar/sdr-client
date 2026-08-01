"""
PlanStore — local persistence for cross-unit plans.

Plans are a GUI-only concept (no agent Plan store), so they live in a JSON file
next to the client (plans.json by default). This is a tiny CRUD wrapper over that
file: load returns the plans, and every mutation writes the whole list back
atomically (via a temp file + replace) so a crash mid-write can't corrupt it.

Nothing here is Qt-aware; the UI wraps saves in its own flow.
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import List, Optional

from api import models as m

logger = logging.getLogger(__name__)

DEFAULT_PLANS_FILE = Path(__file__).resolve().parent.parent / "plans.json"


def new_plan_id() -> str:
    return "plan_" + secrets.token_hex(4)


class PlanStore:
    def __init__(self, path: Path = DEFAULT_PLANS_FILE):
        self._path = Path(path)
        self._plans: List[m.Plan] = []
        self.load()

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> List[m.Plan]:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._plans = []
            return self._plans
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("plans", []) if isinstance(data, dict) else data
            self._plans = [m.Plan(**p) for p in raw]
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read plans from %s: %s", self._path, exc)
            self._plans = []
        return self._plans

    def _save(self) -> None:
        data = {"plans": [p.model_dump() for p in self._plans]}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ── Accessors / mutations ────────────────────────────────────────────────

    def plans(self) -> List[m.Plan]:
        return list(self._plans)

    def get(self, plan_id: str) -> Optional[m.Plan]:
        return next((p for p in self._plans if p.id == plan_id), None)

    def upsert(self, plan: m.Plan) -> m.Plan:
        """Insert a new plan or replace an existing one with the same id."""
        for i, p in enumerate(self._plans):
            if p.id == plan.id:
                self._plans[i] = plan
                self._save()
                return plan
        self._plans.append(plan)
        self._save()
        return plan

    def delete(self, plan_id: str) -> bool:
        before = len(self._plans)
        self._plans = [p for p in self._plans if p.id != plan_id]
        if len(self._plans) != before:
            self._save()
            return True
        return False
