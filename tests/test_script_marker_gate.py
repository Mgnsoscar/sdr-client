"""The paramkit-marker deploy gate (api/script_markers.py + AgentClient.upload_script/deploy_library).

A script declaring a paramkit builder kwarg the unit's paramkit does not know (`is_elapsed=True`,
agent 1.32.0) CRASHES at build_script() on every launch on an older unit — the agent's static upload
validator accepts the file. The client refuses to ship it to a unit that does not advertise the
matching capability, exactly like the CAL_*/SEQUENCE_* gates, and says why.
"""
import pytest

from api import models as m
from api import script_markers as sm
from api.client import AgentClient, AgentError

MARKED = '''\
from paramkit import Script
s = (Script("d")
     .number("-Elapsed", "--elapsed", unit="s", min=0.0, default=0.0, is_elapsed=True)
     .number("--power", default=-50, live=True))
args = s.parse()
'''
PLAIN = '''\
from paramkit import Script
s = Script("d").number("--power", default=-50, live=True)
args = s.parse()
'''


def _client(caps, version="1.31.1", *, info_caps=None):
    c = AgentClient("unit-a")
    c.capabilities = list(caps)
    c.agent_version = version
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path))
        if path == "/info":
            return {"hostname": "unit-a", "unit_id": "u", "agent_version": version,
                    "python_version": "3.11", "tasks": [],
                    "capabilities": list(info_caps if info_caps is not None else caps)}
        if path == "/scripts/upload":
            return {"ok": True}
        if path == "/library":
            return {}
        raise AssertionError(path)
    c._request = fake_request
    return c, calls


def test_script_marker_capabilities_reads_the_static_argspec():
    assert sm.script_marker_capabilities(MARKED) == {"paramkit-is-elapsed"}
    assert sm.script_marker_capabilities(MARKED.encode()) == {"paramkit-is-elapsed"}
    assert sm.script_marker_capabilities(PLAIN) == set()
    assert sm.script_marker_capabilities("this is not python (") == set()
    assert sm.missing_marker_capabilities(MARKED, []) == [("is_elapsed", "paramkit-is-elapsed")]
    assert sm.missing_marker_capabilities(MARKED, ["paramkit-is-elapsed"]) == []
    assert sm.missing_marker_capabilities(PLAIN, []) == []


def test_upload_refuses_a_marked_script_on_a_unit_without_the_capability():
    c, calls = _client(["calibration"])
    with pytest.raises(AgentError) as ei:
        c.upload_script("cw_drift_tx.py", MARKED.encode())
    msg = str(ei.value)
    assert "is_elapsed" in msg and "paramkit-is-elapsed" in msg and "1.31.1" in msg
    assert "Update the unit's agent first" in msg
    assert ("POST", "/scripts/upload") not in calls          # nothing shipped


def test_upload_ships_a_marked_script_when_the_unit_advertises_the_capability():
    c, calls = _client(["calibration", "paramkit-is-elapsed"], version="1.32.0")
    assert c.upload_script("cw_drift_tx.py", MARKED.encode()) == {"ok": True}
    assert ("POST", "/scripts/upload") in calls


def test_upload_of_a_plain_script_is_never_gated():
    c, calls = _client([])
    assert c.upload_script("cw_tx.py", PLAIN.encode()) == {"ok": True}
    assert ("GET", "/info") not in calls                      # no fetch needed for a plain script


def test_upload_fetches_info_once_when_capabilities_were_never_read():
    c, calls = _client([], version="", info_caps=["paramkit-is-elapsed"])
    assert c.upload_script("cw_drift_tx.py", MARKED.encode()) == {"ok": True}
    assert calls.count(("GET", "/info")) == 1
    c2, calls2 = _client([], version="", info_caps=["calibration"])
    with pytest.raises(AgentError):
        c2.upload_script("cw_drift_tx.py", MARKED.encode())
    assert calls2.count(("GET", "/info")) == 1 and ("POST", "/scripts/upload") not in calls2


def test_deploy_library_refuses_before_the_put_when_any_script_is_marked():
    lib = m.Library(scripts=[m.LibraryScript(name="cw_tx.py", content=PLAIN),
                             m.LibraryScript(name="cw_drift_tx.py", content=MARKED)])
    c, calls = _client(["calibration"])
    with pytest.raises(AgentError) as ei:
        c.deploy_library(lib)
    assert "cw_drift_tx.py" in str(ei.value)
    assert ("PUT", "/library") not in calls
    c2, calls2 = _client(["paramkit-is-elapsed"], version="1.32.0")
    c2.deploy_library(lib)
    assert ("PUT", "/library") in calls2
    c3, calls3 = _client([])
    c3.deploy_library(m.Library(scripts=[m.LibraryScript(name="cw_tx.py", content=PLAIN)]))
    assert ("PUT", "/library") in calls3
