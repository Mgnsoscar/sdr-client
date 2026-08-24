"""AgentClient wrappers for the per-unit data store / calibration endpoints map to
the right HTTP calls and parse responses. _request is stubbed (no real httpx)."""
from api.client import AgentClient


def _client(monkeypatch, canned):
    monkeypatch.setattr(AgentClient, "_new_client", lambda self: object())
    c = AgentClient("unit_x", addresses=["10.0.0.5"])
    calls = []

    def fake_request(self, method, path, *, json=None, params=None, files=None, **kw):
        calls.append({"method": method, "path": path, "files": files})
        return canned
    monkeypatch.setattr(AgentClient, "_request", fake_request)
    return c, calls


def test_list_files(monkeypatch):
    c, calls = _client(monkeypatch, [{"name": "calibration.json", "size": 12, "modified": "t"}])
    out = c.list_files()
    assert calls[0]["method"] == "GET" and calls[0]["path"] == "/files"
    assert out[0]["name"] == "calibration.json"


def test_upload_file_posts_multipart(monkeypatch):
    c, calls = _client(monkeypatch, {"saved": "calibration.json", "calibration": {"gps": {}}})
    resp = c.upload_file("calibration.json", b'{"x":1}')
    assert calls[0]["method"] == "POST" and calls[0]["path"] == "/files"
    name, content, ctype = calls[0]["files"]["file"]
    assert name == "calibration.json" and content == b'{"x":1}'
    assert "calibration" in resp


def test_get_file_returns_content(monkeypatch):
    c, calls = _client(monkeypatch, {"name": "notes.txt", "content": "hello", "size": 5})
    assert c.get_file("notes.txt") == "hello"
    assert calls[0]["path"] == "/files/notes.txt"


def test_delete_file(monkeypatch):
    c, calls = _client(monkeypatch, {"deleted": "notes.txt"})
    c.delete_file("notes.txt")
    assert calls[0]["method"] == "DELETE" and calls[0]["path"] == "/files/notes.txt"


def test_get_calibration(monkeypatch):
    c, calls = _client(monkeypatch, {"unit_type": "broadcaster", "valid": True, "signals": {}})
    view = c.get_calibration()
    assert calls[0]["method"] == "GET" and calls[0]["path"] == "/calibration"
    assert view["valid"] is True
