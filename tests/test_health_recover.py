"""health(recover=…) governs the offline path: the default self-heals a moved unit
by re-probing its other addresses (warmup), but a bulk reachability gate passes
recover=False so one offline unit costs a single connect-timeout, not N of them."""
from api.client import AgentClient, AgentConnectionError, ConnectionState


def _offline_client(addresses):
    c = AgentClient("unit_x", addresses=addresses)
    # /health always fails to connect (unit is offline on the active address).
    def _boom(method, path, **kw):
        raise AgentConnectionError("unit_x", "connect refused")
    c._request = _boom
    return c


def test_recover_false_skips_multi_address_warmup(monkeypatch):
    c = _offline_client(["10.0.0.5", "broadcaster-1.local"])   # 2 addresses
    called = {"warmup": 0}
    monkeypatch.setattr(c, "warmup", lambda: called.__setitem__("warmup", called["warmup"] + 1)
                        or ConnectionState.OFFLINE)
    assert c.health(recover=False) is False
    assert called["warmup"] == 0                                # no slow recovery probe
    assert c.state == ConnectionState.OFFLINE


def test_recover_true_still_self_heals(monkeypatch):
    c = _offline_client(["10.0.0.5", "broadcaster-1.local"])
    called = {"warmup": 0}

    def _fake_warmup():
        called["warmup"] += 1
        return ConnectionState.ONLINE
    monkeypatch.setattr(c, "warmup", _fake_warmup)
    assert c.health(recover=True) is True                      # recovered via warmup
    assert called["warmup"] == 1


def test_single_address_never_warms_up(monkeypatch):
    c = _offline_client(["10.0.0.5"])                          # only one address
    monkeypatch.setattr(c, "warmup", lambda: (_ for _ in ()).throw(AssertionError("warmup called")))
    assert c.health(recover=True) is False                     # nothing to recover to
    assert c.state == ConnectionState.OFFLINE
