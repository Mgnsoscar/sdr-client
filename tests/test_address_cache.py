"""
AddressCache tests — the last-known-IP store, and the warmup machine-id guard that
keeps a stale cached IP from adopting a different unit's identity.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state.address_cache import AddressCache, MAX_AGE_S
from api.client import AgentClient
from api import models as m


# ── AddressCache ────────────────────────────────────────────────────────────

def test_record_and_lookup(tmp_path):
    c = AddressCache(tmp_path / "ac.json")
    assert c.ip_for("mid-1") is None
    assert c.record("mid-1", "192.168.1.87", host="broadcaster-1.local") is True
    assert c.ip_for("mid-1") == "192.168.1.87"


def test_persists_across_instances(tmp_path):
    p = tmp_path / "ac.json"
    AddressCache(p).record("mid-1", "10.0.0.5", port=8765)
    assert AddressCache(p).ip_for("mid-1") == "10.0.0.5"


def test_same_ip_is_not_a_rewrite_but_moves_are(tmp_path):
    c = AddressCache(tmp_path / "ac.json")
    c.record("mid-1", "192.168.1.87")
    assert c.record("mid-1", "192.168.1.87") is False   # unchanged
    assert c.record("mid-1", "192.168.1.99") is True    # moved → persisted
    assert c.ip_for("mid-1") == "192.168.1.99"


def test_stale_entries_are_ignored(tmp_path):
    c = AddressCache(tmp_path / "ac.json")
    c.record("mid-1", "192.168.1.87")
    c._map["mid-1"]["ts"] = time.time() - MAX_AGE_S - 1   # age it out
    assert c.ip_for("mid-1") is None


def test_forget(tmp_path):
    c = AddressCache(tmp_path / "ac.json")
    c.record("mid-1", "192.168.1.87")
    c.forget("mid-1")
    assert c.ip_for("mid-1") is None
    assert AddressCache(tmp_path / "ac.json").ip_for("mid-1") is None


def test_no_machine_id_or_ip_is_noop(tmp_path):
    c = AddressCache(tmp_path / "ac.json")
    assert c.record("", "192.168.1.1") is False
    assert c.record("mid-1", "") is False


def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "ac.json"
    p.write_text("{ not json")
    assert AddressCache(p).ip_for("anything") is None


# ── warmup machine-id guard ─────────────────────────────────────────────────

def _info(unit_id, machine_id):
    return {"hostname": unit_id, "unit_id": unit_id, "machine_id": machine_id,
            "agent_version": "1.0.1", "python_version": "3.11", "tasks": []}


def test_warmup_skips_an_address_that_is_a_different_unit(monkeypatch):
    """The cached IP was reassigned by DHCP to ANOTHER agent; warmup must skip it and
    connect via the real address, keeping the expected identity."""
    client = AgentClient("unit_x", addresses=["10.0.0.99", "broadcaster-1.local"],
                         api_key="")
    client.machine_id = "REAL-MID"          # what we expect this unit to be

    # Which /info each address returns, keyed by the active address at request time.
    responses = {
        "10.0.0.99": _info("someone-else", "OTHER-MID"),   # stale cached IP → wrong Pi
        "broadcaster-1.local": _info("broadcaster-1", "REAL-MID"),
    }
    monkeypatch.setattr(AgentClient, "_connect_to",
                        lambda self, addr: setattr(self, "_active_addr", addr))
    monkeypatch.setattr(AgentClient, "_resolve_and_pin_ip", lambda self: None)
    monkeypatch.setattr(AgentClient, "_request",
                        lambda self, method, path, **kw: responses[self._active_addr])

    state = client.warmup()
    assert state == client.state
    assert client.active_address() == "broadcaster-1.local"   # skipped the wrong one
    assert client.machine_id == "REAL-MID"
    assert client.unit_id == "broadcaster-1"


def test_warmup_accepts_matching_fingerprint(monkeypatch):
    client = AgentClient("unit_x", addresses=["10.0.0.5"], api_key="")
    client.machine_id = "REAL-MID"
    monkeypatch.setattr(AgentClient, "_connect_to",
                        lambda self, addr: setattr(self, "_active_addr", addr))
    monkeypatch.setattr(AgentClient, "_resolve_and_pin_ip", lambda self: None)
    monkeypatch.setattr(AgentClient, "_request",
                        lambda self, method, path, **kw: _info("broadcaster-1", "REAL-MID"))
    client.warmup()
    assert client.active_address() == "10.0.0.5"
    assert client.unit_id == "broadcaster-1"
