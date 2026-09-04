"""
UnitLedger — a permanent machine_id → uid map that OUTLIVES unit deletion.

A unit's permanent ``uid`` is the key that plans and the schedule reference. It is
minted randomly when a unit is added, which is normally fine. The problem is
re-adding the SAME physical Pi after deleting it: a fresh random uid would be
minted, and every plan that referenced the old uid would show a "missing unit".

This ledger fixes that. Whenever we know both a Pi's hardware fingerprint
(``/etc/machine-id``) and the uid we gave it, we record ``machine_id → uid`` here —
and, crucially, we NEVER erase an entry when the unit is removed. So when the same
Pi comes back (recognised by its machine_id, whether picked from discovery or
learned on first connect), we hand it back its original uid and its plans light up
again.

Mappings are first-wins: the first uid a machine_id is tied to is the canonical
one plans reference, so :meth:`record` never overwrites an existing entry — a unit
that turns up under a newer uid is re-keyed back to the original instead.

The file is a tiny JSON map next to units.yaml. Nothing here is Qt-aware.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from paths import data_file

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_FILE = data_file("unit_ledger.json")


class UnitLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER_FILE):
        self._path = Path(path)
        self._map: Dict[str, str] = {}   # machine_id → uid
        self.load()

    def load(self) -> None:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._map = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("machine_ids", {}) if isinstance(data, dict) else {}
            self._map = {str(k): str(v) for k, v in raw.items() if k and v}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read unit ledger from %s: %s", self._path, exc)
            self._map = {}

    def _save(self) -> None:
        data = {"machine_ids": self._map}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def uid_for(self, machine_id: str) -> Optional[str]:
        """The permanent uid previously tied to this Pi's machine_id, or None."""
        if not machine_id:
            return None
        return self._map.get(machine_id)

    def record(self, machine_id: str, uid: str) -> bool:
        """Remember ``machine_id → uid`` if this Pi isn't already on the ledger.
        First-wins: an existing mapping is left untouched (it is the id plans
        reference). Returns True if a new mapping was written."""
        if not machine_id or not uid:
            return False
        if self._map.get(machine_id) == uid:
            return False
        if machine_id in self._map:
            # Already tied to an earlier uid — that one stays canonical.
            return False
        self._map[machine_id] = uid
        try:
            self._save()
        except OSError as exc:
            logger.warning("Could not persist unit ledger: %s", exc)
            return False
        logger.info("Ledger: tied machine-id to permanent id %s", uid)
        return True
