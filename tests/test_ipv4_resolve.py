"""
Warmup resolves an mDNS .local name to IPv4 BEFORE connecting, so the client never
chases the unit's IPv6 link-local (fe80::) record and fail — the "reachable by
`ping -4` but the app says offline over a direct cable" bug.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.client as C
from api.client import AgentClient, ConnectionState, AgentConnectionError


def _info(mid="MID-1"):
    return {"hostname": "broadcaster-1", "unit_id": "broadcaster-1", "machine_id": mid,
            "agent_version": "1.0.1", "python_version": "3.13", "tasks": []}


def test_warmup_pins_ipv4_before_first_request(monkeypatch):
    c = AgentClient("unit_x", addresses=["broadcaster-1.local"])
    monkeypatch.setattr(AgentClient, "_new_client", lambda self: object())
    # The .local name resolves to an IPv4 A record.
    monkeypatch.setattr(C.socket, "gethostbyname",
                        lambda h: "169.254.145.222" if h == "broadcaster-1.local" else None)

    # Simulate reality: connecting to the raw .local name resolves to fe80:: and fails;
    # only once we've pinned the IPv4 does /info succeed.
    def fake_request(self, method, path, **kw):
        if self._resolved_ip == "169.254.145.222":
            return _info()
        raise AgentConnectionError(self.unit_id, "cannot reach fe80:: (IPv6)")
    monkeypatch.setattr(AgentClient, "_request", fake_request)

    assert c.warmup() == ConnectionState.ONLINE
    assert c._resolved_ip == "169.254.145.222"   # resolved to IPv4 before connecting


def test_warmup_bare_ip_is_not_resolved(monkeypatch):
    c = AgentClient("unit_x", addresses=["169.254.10.5"])
    monkeypatch.setattr(AgentClient, "_new_client", lambda self: object())
    calls = {"n": 0}

    def counting_gethostbyname(h):
        calls["n"] += 1
        return "1.2.3.4"
    monkeypatch.setattr(C.socket, "gethostbyname", counting_gethostbyname)
    monkeypatch.setattr(AgentClient, "_request", lambda self, *a, **k: _info())

    assert c.warmup() == ConnectionState.ONLINE
    assert c._resolved_ip is None      # a bare IP needs no resolution
    assert calls["n"] == 0             # gethostbyname never called for a bare IP


def test_resolve_falls_back_to_getaddrinfo_ipv4(monkeypatch):
    c = AgentClient("unit_x", addresses=["broadcaster-1.local"])
    monkeypatch.setattr(AgentClient, "_new_client", lambda self: object())
    # gethostbyname fails, but an explicit AF_INET getaddrinfo answers.
    monkeypatch.setattr(C.socket, "gethostbyname",
                        lambda h: (_ for _ in ()).throw(OSError("no A via gethostbyname")))
    monkeypatch.setattr(C.socket, "getaddrinfo",
                        lambda host, port, family, *a, **k: [(2, 1, 6, "", ("169.254.7.7", port))])
    c._resolve_and_pin_ip()
    assert c._resolved_ip == "169.254.7.7"
