"""
Client configuration: loads the known-units list and a few settings.

units.yaml format:
    webhook_port: 8766          # optional; port the GUI listens on for events
    api_key: ""                 # optional; shared secret if agents require it
    units:
      - label: Broadcaster 1    # a stable name you choose
        addresses:              # every address to try; first that answers wins
          - broadcaster-1.local #   (an mDNS .local name, resolves on any network)
          - 192.168.1.42        #   home wifi IP
          - 169.254.61.247      #   work ethernet IP
        api_key: ""             # optional per-unit override

The older format (a bare hostname, or `- hostname: …`) still loads: the hostname
becomes both the label and the single address.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

DEFAULT_UNITS_FILE = Path(__file__).parent / "units.yaml"


def new_unit_uid() -> str:
    """A permanent internal id for a unit — the stable key that plans/schedule and
    the fleet reference, independent of the (renamable) label or its addresses."""
    return "unit_" + secrets.token_hex(4)


@dataclass
class UnitEntry:
    label: str                                      # display name (freely renamable)
    addresses: List[str] = field(default_factory=list)   # hosts/IPs to try, in order
    api_key: str = ""
    uid: str = ""                                   # permanent identity (fleet key)
    machine_id: str = ""                            # learned /etc/machine-id fingerprint

    def __post_init__(self):
        if not self.uid:
            self.uid = new_unit_uid()

    @property
    def primary(self) -> str:
        """The first address (or the label if none) — a reasonable default target."""
        return self.addresses[0] if self.addresses else self.label


@dataclass
class ClientConfig:
    units: List[UnitEntry] = field(default_factory=list)
    webhook_port: int = 8766
    api_key: str = ""               # fleet-wide default; per-unit can override

    @classmethod
    def load(cls, path: Path = DEFAULT_UNITS_FILE) -> "ClientConfig":
        if not path.exists():
            logger.warning("units file not found at %s — starting with no units", path)
            return cls()
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}

        api_key = raw.get("api_key", "")
        units = []
        migrated = False
        for entry in raw.get("units", []):
            u = cls._parse_unit(entry, api_key)
            if u is None:
                logger.warning("Skipping malformed unit entry: %s", entry)
                continue
            # A unit with no stored uid is pre-identity — it just got one; persist it
            # so every load agrees on the same permanent id.
            if not (isinstance(entry, dict) and entry.get("uid")):
                migrated = True
            units.append(u)

        cfg = cls(
            units=units,
            webhook_port=int(raw.get("webhook_port", 8766)),
            api_key=api_key,
        )
        logger.info("Loaded %d unit(s) from %s", len(cfg.units), path)
        if migrated:
            try:
                cfg.save(path)
                logger.info("Assigned permanent ids to unit(s) and saved %s", path)
            except OSError as exc:
                logger.warning("Could not persist unit ids: %s", exc)
        return cfg

    @staticmethod
    def _parse_unit(entry, default_key: str):
        # Bare string → a single-address unit named after the host.
        if isinstance(entry, str):
            return UnitEntry(label=entry, addresses=[entry], api_key=default_key)
        if not isinstance(entry, dict):
            return None
        api_key = entry.get("api_key", default_key)
        uid = entry.get("uid", "") or ""
        machine_id = entry.get("machine_id", "") or ""
        # New format: label + addresses.
        if entry.get("label") or entry.get("addresses"):
            addrs = [a for a in (entry.get("addresses") or []) if a]
            # Tolerate a stray legacy hostname alongside the new fields.
            if entry.get("hostname") and entry["hostname"] not in addrs:
                addrs.append(entry["hostname"])
            label = entry.get("label") or (addrs[0] if addrs else "")
            if not label:
                return None
            return UnitEntry(label=label, addresses=addrs, api_key=api_key,
                             uid=uid, machine_id=machine_id)
        # Legacy format: a single hostname is both the label and the address.
        if entry.get("hostname"):
            h = entry["hostname"]
            return UnitEntry(label=h, addresses=[h], api_key=api_key,
                             uid=uid, machine_id=machine_id)
        return None

    def save(self, path: Path = DEFAULT_UNITS_FILE) -> None:
        data = {
            "webhook_port": self.webhook_port,
            "api_key": self.api_key,
            "units": [{"uid": u.uid, "label": u.label, "addresses": list(u.addresses),
                       "api_key": u.api_key, "machine_id": u.machine_id}
                      for u in self.units],
        }
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(path)