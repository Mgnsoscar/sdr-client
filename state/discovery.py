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
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
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
    machine_id: str = ""           # the Pi's /etc/machine-id (stable fingerprint)

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
    # Keep only usable IPv4:
    #   - drop loopback (e.g. 127.0.1.1, which Debian maps a hostname to) — the
    #     client would "connect" to itself;
    #   - drop IPv6 (anything with a ':') — including link-local fe80:: addresses,
    #     which need a zone index and which this IPv4-only stack can't use.
    addresses = [a for a in addresses
                 if a and ":" not in a and not a.startswith("127.")]
    props = getattr(info, "properties", {}) or {}

    def _prop(key: str) -> str:
        raw = props.get(key.encode()) or props.get(key)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return raw if isinstance(raw, str) else ""

    unit_id = _prop("unit_id")
    machine_id = _prop("machine_id")
    if not unit_id:
        # Fall back to the instance name: "<unit_id>._sdragent._tcp.local."
        unit_id = name.split("." + SERVICE_TYPE.split(".", 1)[0], 1)[0].rstrip(".")
    server = (getattr(info, "server", "") or "").rstrip(".")
    port = int(getattr(info, "port", 0) or 8765)
    return DiscoveredUnit(unit_id=unit_id, hostname=server, addresses=addresses,
                          port=port, machine_id=machine_id)


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
            from zeroconf import Zeroconf, ServiceBrowser, InterfaceChoice
        except ImportError:
            logger.warning("zeroconf not installed — unit auto-discovery disabled "
                           "(you can still add units by address)")
            return
        try:
            # InterfaceChoice.All makes zeroconf bind every interface, including a
            # direct-ethernet link with only a link-local (169.254.x) address —
            # otherwise such an interface can be skipped and the unit on it missed.
            self._zc = Zeroconf(interfaces=InterfaceChoice.All)
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

    def rescan(self) -> None:
        """Force a fresh scan AND re-enumerate network interfaces (e.g. the user
        clicked Refresh). This fully restarts zeroconf so an interface that came up
        AFTER launch — like a just-plugged direct-ethernet link with a link-local
        169.254.x address — is included; a browser-only restart would keep using the
        interfaces present at startup and never see it. Clears the cache so the list
        reflects the new scan."""
        self.stop()
        with self._lock:
            self._found.clear()
        self.start()
        logger.info("Discovery: re-scanning (re-enumerated interfaces)")

    def probe_subnet(self, api_key: str = "", port: int = 8765,
                     timeout: float = 0.35, workers: int = 64) -> List[DiscoveredUnit]:
        """Actively sweep this PC's local /24 for agents — the fallback for a network
        that filters mDNS multicast (e.g. a long-range WiFi→Ethernet bridge), where a
        unit is on a routable IP but never shows up via zeroconf.

        For each host: a fast TCP check on `port`, then GET /health (unauthenticated)
        to confirm it's an agent, then GET /info (with `api_key`) for its identity.
        Hits are merged into the discovered set so the picker and the machine-id
        auto-learn treat them exactly like an mDNS find. Blocking (~1-3 s); call it
        from a worker thread. Best-effort — returns [] if the local IP is unknown."""
        from .netutil import local_ip
        ip = local_ip()
        if not ip or ip.count(".") != 3:
            return []
        base = ip.rsplit(".", 1)[0]
        targets = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != ip]

        def probe(host: str) -> Optional[DiscoveredUnit]:
            try:
                socket.create_connection((host, port), timeout=timeout).close()
            except OSError:
                return None   # nothing listening — the common case, fails fast
            return self._identify(host, port, api_key)

        found: List[DiscoveredUnit] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for unit in pool.map(probe, targets):
                if unit is not None:
                    found.append(unit)
        if found:
            with self._lock:
                for u in found:
                    self._found[f"probe:{u.addresses[0] if u.addresses else u.unit_id}"] = u
            logger.info("Discovery: subnet probe found %d agent(s) on %s.0/24",
                        len(found), base)
            if self._on_change is not None:
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001
                    logger.debug("Discovery on_change callback raised", exc_info=True)
        return found

    @staticmethod
    def _identify(host: str, port: int, api_key: str) -> Optional[DiscoveredUnit]:
        """Confirm an agent at host:port via /health, then read /info for identity.
        Returns a DiscoveredUnit (with machine_id when /info is readable), or None."""
        import httpx
        base = f"http://{host}:{port}"
        headers = {"X-API-Key": api_key} if api_key else {}
        try:
            with httpx.Client(timeout=2.0) as c:
                h = c.get(f"{base}/health")
                if h.status_code != 200 or (h.json() or {}).get("status") != "ok":
                    return None
                try:
                    r = c.get(f"{base}/info", headers=headers)
                    if r.status_code == 200:
                        d = r.json()
                        return DiscoveredUnit(
                            unit_id=d.get("unit_id") or host, hostname="",
                            addresses=[host], port=port,
                            machine_id=d.get("machine_id", "") or "")
                except Exception:  # noqa: BLE001 — identity is gated; still a hit
                    pass
                # An agent is here but /info needs a key we don't have — surface the
                # address anyway so the operator can add it by IP.
                return DiscoveredUnit(unit_id=host, hostname="", addresses=[host], port=port)
        except Exception:  # noqa: BLE001 — not an agent / not HTTP
            return None

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
