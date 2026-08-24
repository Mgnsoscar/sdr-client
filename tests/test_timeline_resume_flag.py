"""A bar step's `inject_resume_offset` flag must survive a load-edit-save round trip
through the timeline editor — otherwise opening a resume-enabled sequence and saving it
would silently clear the flag, breaking mid-ramp resume for that run."""
from ui.timeline_model import BarItem, item_to_steps, items_to_steps, steps_to_items


def test_bar_emits_inject_resume_offset_on_start_step():
    bar = BarItem(task_name="ramp", start_offset=-10.0, stop_offset=0.0,
                  inject_resume_offset=True)
    steps = item_to_steps(bar)
    start = next(s for s in steps if s["action"] == "start")
    stop = next(s for s in steps if s["action"] == "stop")
    assert start["inject_resume_offset"] is True
    assert "inject_resume_offset" not in stop or stop.get("inject_resume_offset") is False


def test_round_trip_preserves_flag():
    steps = [
        {"anchor": "start", "offset_s": -10.0, "action": "start",
         "task_name": "ramp", "args": [], "replace_args": False,
         "inject_resume_offset": True},
        {"anchor": "stop", "offset_s": 0.0, "action": "stop",
         "task_name": "ramp", "args": [], "replace_args": False},
    ]
    items = steps_to_items(steps)
    bar = next(it for it in items if getattr(it, "kind", None) == "bar")
    assert bar.inject_resume_offset is True         # loaded from YAML

    out = items_to_steps(items)                     # re-saved
    start = next(s for s in out if s["action"] == "start")
    assert start["inject_resume_offset"] is True    # not reset to False


def test_flag_defaults_false_when_absent():
    steps = [
        {"anchor": "start", "offset_s": 0.0, "action": "start", "task_name": "tx"},
        {"anchor": "stop", "offset_s": 0.0, "action": "stop", "task_name": "tx"},
    ]
    bar = next(it for it in steps_to_items(steps) if getattr(it, "kind", None) == "bar")
    assert bar.inject_resume_offset is False
