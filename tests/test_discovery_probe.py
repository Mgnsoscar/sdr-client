"""
Subnet-probe tests — the active-sweep fallback for networks that filter mDNS.

Fakes the socket connect + httpx so no real network is touched: verifies /health
gates detection, /info supplies identity (and its absence still surfaces the address),
and that a swept hit merges into the discovered set and fires the change callback.
"""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import discovery as D
from state.discovery import Discovery, DiscoveredUnit


# ── _identify (fake httpx) ──────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class _FakeHTTP:
    """Scripted httpx.Client: routes by URL suffix."""
    def __init__(self, health=None, info=None):
        self._health, self._info = health, info
    def __call__(self, *a, **k):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get(self, url, headers=None):
        if url.endswith("/health"):
            if self._health is None:
                raise OSError("refused")
            return self._health
        return self._info


def _install_http(monkeypatch, health, info):
    import httpx
    monkeypatch.setattr(httpx, "Client", _FakeHTTP(health, info))


def test_identify_reads_info(monkeypatch):
    _install_http(monkeypatch,
                  health=_Resp(200, {"status": "ok"}),
                  info=_Resp(200, {"unit_id": "broadcaster-1", "machine_id": "MID-1"}))
    u = Discovery._identify("192.168.1.87", 8765, api_key="k")
    assert u and u.unit_id == "broadcaster-1" and u.machine_id == "MID-1"
    assert u.addresses == ["192.168.1.87"]


def test_identify_health_only_still_surfaces_address(monkeypatch):
    # Agent present but /info is gated (401) → we still return the address.
    _install_http(monkeypatch,
                  health=_Resp(200, {"status": "ok"}), info=_Resp(401, {}))
    u = Discovery._identify("192.168.1.87", 8765, api_key="")
    assert u and u.addresses == ["192.168.1.87"] and u.machine_id == ""


def test_identify_not_an_agent(monkeypatch):
    _install_http(monkeypatch, health=_Resp(200, {"status": "weird"}), info=None)
    assert Discovery._identify("192.168.1.5", 8765, api_key="") is None


# ── probe_subnet (fake socket + _identify) ──────────────────────────────────

def test_probe_subnet_finds_and_merges(monkeypatch):
    monkeypatch.setattr(D, "local_ip", lambda: "192.168.1.50", raising=False)
    # local_ip is imported inside the method via `from .netutil import local_ip`,
    # so patch the source module too.
    import state.netutil as netutil
    monkeypatch.setattr(netutil, "local_ip", lambda: "192.168.1.50")

    live = "192.168.1.87"

    class _Sock:
        def close(self): pass

    def fake_connect(addr, timeout=None):
        if addr[0] == live:
            return _Sock()
        raise OSError("no route")
    monkeypatch.setattr(D.socket, "create_connection", fake_connect)
    monkeypatch.setattr(Discovery, "_identify",
                        staticmethod(lambda host, port, api_key:
                                     DiscoveredUnit(unit_id="broadcaster-1", addresses=[host],
                                                    machine_id="MID-1") if host == live else None))

    disc = Discovery()
    fired = []
    disc.set_callback(lambda: fired.append(True))
    found = disc.probe_subnet(api_key="k", workers=16, timeout=0.05)

    assert len(found) == 1 and found[0].addresses == [live]
    # merged into the discovered set + change callback fired
    assert any(u.machine_id == "MID-1" for u in disc.discovered())
    assert fired


def test_probe_subnet_no_local_ip_returns_empty(monkeypatch):
    import state.netutil as netutil
    monkeypatch.setattr(netutil, "local_ip", lambda: None)
    assert Discovery().probe_subnet() == []
