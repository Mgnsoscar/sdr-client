"""
AddressCache — remember each unit's last-known IP so a fresh launch connects fast.

The client resolves a unit's ``.local`` name to an IP and pins it for the session
(``AgentClient._resolve_and_pin_ip``), but that knowledge is lost when the app closes.
On the next launch — and on every *other* PC — the first contact pays the mDNS lookup
again, which is the "connecting by name is slow" complaint.

This persists ``machine_id → {ip, host, port, ts}`` to a tiny JSON file beside
units.yaml (mirrors ``unit_ledger.py``). On startup the Units tab seeds each client's
address list with the cached IP FIRST, so ``warmup()`` tries the fast direct-IP path
before mDNS. Keyed by the stable hardware fingerprint (``/etc/machine-id``), not the
address, so it survives the unit moving between networks.

Safety: a cached IP is only a hint. DHCP may have handed that IP to a different device
by now, so the caller must verify the ``machine_id`` reported over it matches before
trusting it (``AgentClient.warmup`` skips an address whose fingerprint doesn't match).
A wrong or stale entry just falls back to mDNS and is overwritten on the next success.

Nothing here is Qt-aware.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from paths import data_file

logger = logging.getLogger(__name__)

DEFAULT_CACHE_FILE = data_file("address_cache.json")

# Ignore entries older than this — a unit not seen in a month has very likely moved.
MAX_AGE_S = 30 * 24 * 3600


class AddressCache:
    def __init__(self, path: Path = DEFAULT_CACHE_FILE):
        self._path = Path(path)
        self._map: Dict[str, dict] = {}   # machine_id → {ip, host, port, ts}
        self.load()

    def load(self) -> None:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._map = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("addresses", {}) if isinstance(data, dict) else {}
            self._map = {str(k): dict(v) for k, v in raw.items()
                         if k and isinstance(v, dict) and v.get("ip")}
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Could not read address cache from %s: %s", self._path, exc)
            self._map = {}

    def _save(self) -> None:
        data = {"addresses": self._map}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def ip_for(self, machine_id: str) -> Optional[str]:
        """The last-known IP for this Pi, or None if unknown/stale."""
        if not machine_id:
            return None
        entry = self._map.get(machine_id)
        if not entry:
            return None
        if time.time() - float(entry.get("ts", 0)) > MAX_AGE_S:
            return None
        return entry.get("ip") or None

    def record(self, machine_id: str, ip: str, host: str = "", port: int = 8765) -> bool:
        """Remember ``machine_id`` is currently reachable at ``ip``. Returns True if
        the stored IP changed (worth persisting). No-op without a machine_id or ip."""
        if not machine_id or not ip:
            return False
        prev = self._map.get(machine_id, {})
        if prev.get("ip") == ip and prev.get("port") == port:
            # Same location — just refresh the timestamp, cheaply, without rewriting.
            prev["ts"] = time.time()
            self._map[machine_id] = prev
            return False
        self._map[machine_id] = {"ip": ip, "host": host, "port": int(port), "ts": time.time()}
        try:
            self._save()
        except OSError as exc:
            logger.warning("Could not persist address cache: %s", exc)
            return False
        return True

    def forget(self, machine_id: str) -> None:
        """Drop a stale/wrong entry (e.g. the cached IP answered as a different Pi)."""
        if machine_id in self._map:
            self._map.pop(machine_id, None)
            try:
                self._save()
            except OSError:
                pass
