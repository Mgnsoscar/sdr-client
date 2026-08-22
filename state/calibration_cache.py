"""
CalibrationCache — remember each unit's last-known /calibration on this PC.

A unit's calibration is only fetchable when it's reachable, but plans (and their
absolute-power steps) are authored offline against the library. So whenever we do
see a unit's calibration, we stash it here; when the unit is offline we fall back to
this copy, marked stale, so absolute power stays available with the last-known
bounds until the unit is back and we refresh.

GUI-only, keyed by hostname, persisted as JSON next to the client (same pattern as
PlanStore). Not Qt-aware. Only VALID calibrations are stored; a unit that is online
but uncalibrated (a 404) must NOT be served from cache, so callers distinguish
'offline' (use cache) from 'uncalibrated' (don't) themselves.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_FILE = Path(__file__).resolve().parent.parent / "calibration_cache.json"


class CalibrationCache:
    def __init__(self, path: Path = DEFAULT_CACHE_FILE):
        self._path = Path(path)
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._data = {}
            return
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = doc.get("units", {}) if isinstance(doc, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read calibration cache %s: %s", self._path, exc)
            self._data = {}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"units": self._data}, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.warning("Could not write calibration cache %s: %s", self._path, exc)

    def put(self, hostname: str, calibration: dict) -> None:
        """Store a unit's calibration result (only if it's a VALID one)."""
        if not hostname or not isinstance(calibration, dict) or not calibration.get("valid"):
            return
        self._data[hostname] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "calibration": calibration,
        }
        self._save()

    def get(self, hostname: str) -> Optional[dict]:
        """The last-known calibration result for a unit, or None."""
        entry = self._data.get(hostname)
        return entry.get("calibration") if entry else None

    def fetched_at(self, hostname: str) -> Optional[str]:
        entry = self._data.get(hostname)
        return entry.get("fetched_at") if entry else None


_CACHE: Optional[CalibrationCache] = None


def get_calibration_cache() -> CalibrationCache:
    """Process-wide singleton so every dialog shares (and persists) one cache."""
    global _CACHE
    if _CACHE is None:
        _CACHE = CalibrationCache()
    return _CACHE
