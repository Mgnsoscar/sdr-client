"""Pin the client → agent URL contract for the read-only task sub-resources. They moved
to their own /task-* prefixes (agent 1.1.8) with the task name as the terminal path
segment, so a name containing '/' (even one ending in "logs"/"history") is never
misrouted. If either side changes a path, this test must change with it."""
from api.client import AgentClient


def _capture():
    c = AgentClient("unit_x", port=8080)
    calls = []
    c._request = lambda method, path, **kw: calls.append((method, path, kw)) or []
    return c, calls


def test_task_subresource_paths():
    c, calls = _capture()
    c.task_logs("GPS/L1", lines=50)
    c.task_history("GPS/L1")
    c.get_task_params("GPS/L1")
    paths = [p for _, p, _ in calls]
    assert paths[0] == "/task-logs/GPS/L1"
    assert paths[1] == "/task-history/GPS/L1"
    assert paths[2] == "/task-live-params/GPS/L1"


def test_log_stream_url_uses_terminal_name():
    c, _ = _capture()
    url = c.log_stream_url("GPS/L1", lines=10)
    # '/' is percent-encoded in the stream URL (safe='') → single terminal segment.
    assert "/task-log-stream/GPS%2FL1?" in url
    assert "/logs/stream" not in url
