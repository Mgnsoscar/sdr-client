"""
Client configuration: loads the known-units list and a few settings.

units.yaml format:
    webhook_port: 8766          # optional; port the GUI listens on for events
    api_key: ""                 # optional; shared secret if agents require it
    units:
      - hostname: hostname-1.local
      - hostname: hostname-2.local
      - hostname: 192.168.1.42   # IPs work too (skip mDNS for this one)
        api_key: ""              # optional per-unit override
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

DEFAULT_UNITS_FILE = Path(__file__).parent / "units.yaml"


@dataclass
class UnitEntry:
    hostname: str
    api_key: str = ""


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
        for entry in raw.get("units", []):
            if isinstance(entry, str):
                units.append(UnitEntry(hostname=entry, api_key=api_key))
            elif isinstance(entry, dict) and "hostname" in entry:
                units.append(UnitEntry(
                    hostname=entry["hostname"],
                    api_key=entry.get("api_key", api_key),
                ))
            else:
                logger.warning("Skipping malformed unit entry: %s", entry)

        cfg = cls(
            units=units,
            webhook_port=int(raw.get("webhook_port", 8766)),
            api_key=api_key,
        )
        logger.info("Loaded %d unit(s) from %s", len(cfg.units), path)
        return cfg

    def save(self, path: Path = DEFAULT_UNITS_FILE) -> None:
        data = {
            "webhook_port": self.webhook_port,
            "api_key": self.api_key,
            "units": [{"hostname": u.hostname, "api_key": u.api_key} for u in self.units],
        }
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False))
        tmp.replace(path)