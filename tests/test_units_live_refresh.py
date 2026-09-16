"""The Units tab reacts to pushed SSE lifecycle events (task start/stop/restart, crash) by
refreshing the affected unit NOW, instead of waiting up to a poll cycle — so an externally
caused change (a crash, a task finishing, another operator, a schedule/sequence launching a
task) shows immediately. A scoped refresh keeps it snappy with dead units in the fleet; the
poll stays the backstop, and unattributable events fall back to a full refresh.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from api import models as m
from ui.units_tab import UnitsTab

_app = QApplication.instance() or QApplication(sys.argv)


class _Client:
    def __init__(self, host, uid):
        self.hostname = host
        self.unit_id = uid


class _Fleet:
    def __init__(self, clients):
        self._c = list(clients)

    def units(self):
        return list(self._c)


class _Hub:
    def __init__(self):
        self.calls = []

    def refresh_now(self, host=None):
        self.calls.append(host)


def _tab(clients):
    # Skip the heavy __init__ (it loads units.yaml / the ledger / the address cache from
    # disk and builds the whole grid); on_event only needs the fleet + hub.
    tab = UnitsTab.__new__(UnitsTab)
    tab.fleet = _Fleet(clients)
    tab.hub = _Hub()
    return tab


def _task_ev(unit_id, kind="task_stopped"):
    return m.TaskEvent(type=kind, unit_id=unit_id, task_name="tx", state="stopped", at="now")


def _crash_ev(unit_id):
    return m.CrashEvent(type="crash", unit_id=unit_id, task_name="tx", task_description="",
                        exit_code=1, started_at=None, crashed_at="now", restart_count=0,
                        last_log_lines=[])


def test_task_event_refreshes_only_the_matching_unit():
    tab = _tab([_Client("pi-a.local", "unit-a"), _Client("pi-b.local", "unit-b")])
    tab.on_event(_task_ev("unit-b"))
    assert tab.hub.calls == ["pi-b.local"]                 # scoped to the one unit


def test_every_task_lifecycle_kind_nudges():
    tab = _tab([_Client("pi-a.local", "unit-a")])
    for kind in ("task_started", "task_stopped", "task_restarted"):
        tab.on_event(_task_ev("unit-a", kind))
    assert tab.hub.calls == ["pi-a.local", "pi-a.local", "pi-a.local"]


def test_crash_event_refreshes_the_matching_unit():
    tab = _tab([_Client("pi-a.local", "unit-a")])
    tab.on_event(_crash_ev("unit-a"))
    assert tab.hub.calls == ["pi-a.local"]


def test_unmatched_unit_id_falls_back_to_a_full_refresh():
    tab = _tab([_Client("pi-a.local", "unit-a")])
    tab.on_event(_task_ev("ghost"))
    assert tab.hub.calls == [None]                         # full refresh (backstop) when unattributable


def test_non_lifecycle_events_are_ignored():
    tab = _tab([_Client("pi-a.local", "unit-a")])
    tab.on_event(m.SequenceWebhook(type="sequence_step", unit_id="unit-a", run_id="r",
                                   sequence_name="s", on_air_at="now", state="running", at="now"))
    tab.on_event("not an event")
    assert tab.hub.calls == []                             # Sequences/Plans handle those; Units stays quiet
