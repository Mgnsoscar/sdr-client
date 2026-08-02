"""
Discovery — find broadcaster units that advertise themselves on the local network.

Each agent advertises an `_sdragent._tcp` mDNS service (with its unit_id, port, IP,
and .local hostname). This runs a zeroconf ServiceBrowser on a background thread
and keeps a live list of what it has seen, so the "Add unit" dialog can offer
units to add with their addresses pre-filled — no typing IPs, and it finds the
current address whether the unit is on wifi or a direct ethernet link.

Discovery is a convenience: if zeroconf isn't installed or the network blocks
mDNS, this stays empty and units can still be added by hand. Nothing here is
Qt-aware.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_sdragent._tcp.local."


@dataclass
class DiscoveredUnit:
    unit_id: str
    hostname: str = ""              # the unit's .local name (no trailing dot), if any
    addresses: List[str] = field(default_factory=list)   # IPs seen
    port: int = 8765

    @property
    def suggested_addresses(self) -> List[str]:
        """Addresses to pre-fill when adding: the .local name first (it resolves on
        any network), then the concrete IPs."""
        out: List[str] = []
        if self.hostname:
            out.append(self.hostname)
        for a in self.addresses:
            if a not in out:
                out.append(a)
        return out


def _unit_from_info(name: str, info) -> Optional[DiscoveredUnit]:
    """Turn a zeroconf ServiceInfo into a DiscoveredUnit. Pure/testable — `info`
    just needs parsed_addresses(), port, properties, and server."""
    if info is None:
        return None
    try:
        addresses = list(info.parsed_addresses())
    except Exception:  # noqa: BLE001
        addresses = []
    props = getattr(info, "properties", {}) or {}
    unit_id = ""
    raw = props.get(b"unit_id") or props.get("unit_id")
    if isinstance(raw, bytes):
        unit_id = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        unit_id = raw
    if not unit_id:
        # Fall back to the instance name: "<unit_id>._sdragent._tcp.local."
        unit_id = name.split("." + SERVICE_TYPE.split(".", 1)[0], 1)[0].rstrip(".")
    server = (getattr(info, "server", "") or "").rstrip(".")
    port = int(getattr(info, "port", 0) or 8765)
    return DiscoveredUnit(unit_id=unit_id, hostname=server, addresses=addresses, port=port)


class Discovery:
    def __init__(self):
        self._zc = None
        self._browser = None
        self._found: Dict[str, DiscoveredUnit] = {}   # keyed by mDNS service name
        self._lock = threading.Lock()
        self._on_change: Optional[Callable[[], None]] = None
        self._started = False

    def set_callback(self, cb: Callable[[], None]) -> None:
        """Optional: called (from a background thread) whenever the set changes."""
        self._on_change = cb

    def start(self) -> None:
        if self._started:
            return
        try:
            from zeroconf import Zeroconf, ServiceBrowser
        except ImportError:
            logger.warning("zeroconf not installed — unit auto-discovery disabled "
                           "(you can still add units by address)")
            return
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE,
                                           handlers=[self._on_service])
            self._started = True
            logger.info("Discovery: browsing for %s", SERVICE_TYPE)
        except Exception as exc:  # noqa: BLE001 — mDNS is best-effort
            logger.warning("Discovery could not start: %s", exc)
            self._cleanup()

    def _on_service(self, zeroconf, service_type, name, state_change) -> None:
        try:
            from zeroconf import ServiceStateChange
        except ImportError:
            return
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self._found.pop(name, None)
            logger.info("Discovery: unit went away (%s)", name)
        else:
            try:
                info = zeroconf.get_service_info(service_type, name, timeout=2000)
            except Exception:  # noqa: BLE001
                info = None
            unit = _unit_from_info(name, info)
            if unit is None:
                logger.info("Discovery: saw %s but couldn't resolve its address info", name)
                return
            with self._lock:
                self._found[name] = unit
            logger.info("Discovery: found '%s' at %s", unit.unit_id,
                        ", ".join(unit.suggested_addresses) or "?")
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 — never let a UI callback kill the browser
                logger.debug("Discovery on_change callback raised", exc_info=True)

    def discovered(self) -> List[DiscoveredUnit]:
        """A snapshot of currently-advertised units, sorted by name."""
        with self._lock:
            return sorted(self._found.values(), key=lambda u: u.unit_id.lower())

    def stop(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            if self._browser is not None:
                self._browser.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._zc is not None:
                self._zc.close()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._zc = None
        self._started = False
