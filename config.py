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

from paths import data_file

logger = logging.getLogger(__name__)

# Writable state lives in the per-user data dir when frozen, or the repo root in
# a source/dev checkout (see paths.py). units.yaml is seeded there on first run.
DEFAULT_UNITS_FILE = data_file("units.yaml")

# Unit types + library-scope helpers live in api.models (the dependency-free layer);
# re-exported here so `config.UNIT_TYPES` etc. keep working for the UI.
from api.models import (  # noqa: E402
    UNIT_TYPES, UNIT_TYPE_LABELS, DEFAULT_UNIT_TYPE, applies_to_type,
)


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
    type: str = DEFAULT_UNIT_TYPE                   # unit kind → which library it gets

    def __post_init__(self):
        if not self.uid:
            self.uid = new_unit_uid()
        if not self.type:
            self.type = DEFAULT_UNIT_TYPE

    @property
    def primary(self) -> str:
        """The first address (or the label if none) — a reasonable default target."""
        return self.addresses[0] if self.addresses else self.label


@dataclass
class ProvisionScheme:
    """Deterministic hostname/IP scheme for provisioning a new Pi from its unit
    number N. All fields are editable in the Provision dialog and persisted to
    units.yaml so a site's addressing plan is set once. The client computes a
    unit's hostname + static IPs from N and shows them for confirmation before
    anything is written to the Pi (see docs/provisioning-and-ota.md §4.3)."""
    hostname_prefix: str = "broadcaster"   # hostname = <prefix>-<N>
    eth_subnet: str = "10.0.0"             # eth IP  = <eth_subnet>.<N>
    wlan_subnet: str = "10.0.1"            # wlan IP = <wlan_subnet>.<N>
    prefix_len: int = 24                   # CIDR mask for both
    # Gateways default to .254, NOT .1 — with a "unit N → <subnet>.N" scheme, a .1
    # gateway collides with unit 1's own address (a host can't be its own gateway;
    # NetworkManager rejects it and the interface comes up with no IP). .254 keeps
    # every unit number 1..253 free. Leave blank for an isolated switch with no router.
    eth_gateway: str = "10.0.0.254"
    wlan_gateway: str = "10.0.1.254"
    dns: str = "10.0.0.254 1.1.1.1"        # space-separated resolvers
    ssh_user: str = "pi"                   # default SSH login on a fresh Pi
    wifi_ssid: str = ""                    # default WiFi SSID (PSK is never stored)

    def hostname_for(self, n: int) -> str:
        return f"{self.hostname_prefix}-{n}"

    def eth_ip_for(self, n: int) -> str:
        return f"{self.eth_subnet}.{n}"

    def wlan_ip_for(self, n: int) -> str:
        return f"{self.wlan_subnet}.{n}"

    def to_dict(self) -> dict:
        return {
            "hostname_prefix": self.hostname_prefix,
            "eth_subnet": self.eth_subnet, "wlan_subnet": self.wlan_subnet,
            "prefix_len": self.prefix_len,
            "eth_gateway": self.eth_gateway, "wlan_gateway": self.wlan_gateway,
            "dns": self.dns, "ssh_user": self.ssh_user, "wifi_ssid": self.wifi_ssid,
        }

    @classmethod
    def from_dict(cls, raw) -> "ProvisionScheme":
        if not isinstance(raw, dict):
            return cls()
        d = cls()
        for f in cls().to_dict():
            if f in raw and raw[f] is not None:
                setattr(d, f, type(getattr(d, f))(raw[f]))
        return d


@dataclass
class ClientConfig:
    units: List[UnitEntry] = field(default_factory=list)
    webhook_port: int = 8766
    api_key: str = ""               # fleet-wide default; per-unit can override
    provision: ProvisionScheme = field(default_factory=ProvisionScheme)

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
            # Drop unusable addresses (IPv6 / loopback that may have been auto-learned
            # before we filtered them) and persist the cleanup.
            usable = [a for a in u.addresses if ":" not in a and not a.startswith("127.")]
            if usable != u.addresses:
                u.addresses = usable
                migrated = True
            units.append(u)

        cfg = cls(
            units=units,
            webhook_port=int(raw.get("webhook_port", 8766)),
            api_key=api_key,
            provision=ProvisionScheme.from_dict(raw.get("provision")),
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
        unit_type = entry.get("type") or DEFAULT_UNIT_TYPE
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
                             uid=uid, machine_id=machine_id, type=unit_type)
        # Legacy format: a single hostname is both the label and the address.
        if entry.get("hostname"):
            h = entry["hostname"]
            return UnitEntry(label=h, addresses=[h], api_key=api_key,
                             uid=uid, machine_id=machine_id, type=unit_type)
        return None

    def save(self, path: Path = DEFAULT_UNITS_FILE) -> None:
        data = {
            "webhook_port": self.webhook_port,
            "api_key": self.api_key,
            "provision": self.provision.to_dict(),
            "units": [{"uid": u.uid, "label": u.label, "addresses": list(u.addresses),
                       "api_key": u.api_key, "machine_id": u.machine_id, "type": u.type}
                      for u in self.units],
        }
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        tmp.replace(path)