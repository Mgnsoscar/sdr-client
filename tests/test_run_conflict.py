"""Pure conflict-guard helpers (ui/run_conflict.py) — no Qt needed."""
from types import SimpleNamespace

import api.models as m
from ui import run_conflict as rc


def _step(task):
    return SimpleNamespace(task_name=task)


def _run(state, tasks, sequence_name="Seq", plan_name=""):
    return SimpleNamespace(state=state, steps=[_step(t) for t in tasks],
                           sequence_id="s1", sequence_name=sequence_name,
                           plan_id="p1" if plan_name else "", plan_name=plan_name)


def _status(name, state):
    return SimpleNamespace(name=name, state=state)


def test_sequence_task_names_distinct_first_seen():
    steps = [_step("a"), _step("b"), _step("a"), _step(None), _step("c")]
    assert rc.sequence_task_names(steps) == ["a", "b", "c"]
    assert rc.sequence_task_names([]) == []


def test_running_task_names_intersects_running_only():
    statuses = [_status("a", m.ProcessState.RUNNING), _status("b", m.ProcessState.STOPPED),
                _status("c", m.ProcessState.STARTING), _status("d", m.ProcessState.CRASHED)]
    # order follows `wanted`, only running/starting count
    assert rc.running_task_names(statuses, ["d", "c", "b", "a"]) == ["c", "a"]
    assert rc.running_task_names(statuses, ["b", "d"]) == []
    assert rc.running_task_names([], ["a"]) == []


def test_active_runs_using_task_filters_state_and_task():
    runs = [
        _run(m.SequenceState.RUNNING, ["a", "b"], sequence_name="R1"),
        _run(m.SequenceState.ARMED, ["b"], sequence_name="R2"),
        _run(m.SequenceState.HOLDING, ["a"], sequence_name="R3"),
        _run(m.SequenceState.COMPLETED, ["a"], sequence_name="Done"),   # inactive → ignored
        _run(m.SequenceState.ABORTED, ["a"], sequence_name="Dead"),     # inactive → ignored
    ]
    got = [r.sequence_name for r in rc.active_runs_using_task(runs, "a")]
    assert got == ["R1", "R3"]                        # only active runs that use 'a'
    assert [r.sequence_name for r in rc.active_runs_using_task(runs, "b")] == ["R1", "R2"]
    assert rc.active_runs_using_task(runs, "zzz") == []
    assert rc.active_runs_using_task([], "a") == []


def test_run_label_plan_vs_sequence():
    assert rc.run_label(_run(m.SequenceState.RUNNING, ["a"], sequence_name="MySeq")) == 'sequence “MySeq”'
    assert rc.run_label(_run(m.SequenceState.RUNNING, ["a"], plan_name="MyPlan")) == 'plan “MyPlan”'
