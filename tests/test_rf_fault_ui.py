"""
RF-fault detection — client alarm + pills (Phase 1 Stage C, docs/rf-fault-recovery.md §5).

The agent detects a dead-but-alive flowgraph (radio silent while the task reads RUNNING), fires a
TaskHealthEvent over SSE and stamps ProcessStatus.health / SequenceRun.fault on the poll. These pin
the CLIENT side: the SSE classifier routes the event to its own model (not the generic TaskEvent),
the alert feed styles it red + raises the loud alarm, the fault pill overrides the state pill on
the task and sequence rows, and the main-window alarm handlers are no-op-safe headless.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMainWindow

from api import models as m
from webhook.classify import classify
from ui.alert_feed import AlertFeed, _describe
from ui.theme import Palette, status_color
import ui.timeline_model as tlm
from ui.unit_detail import _TaskRow
import ui.sequences_panel as sp

_app = QApplication.instance() or QApplication([])


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fleet = SimpleNamespace(get=lambda h: None)

    def run_async(self, label, fn):
        self.calls.append(label)


def _health_event(**kw):
    base = dict(unit_id="u1", task_name="chirp", detail="vmcircbuf: boost::interprocess",
                at="2026-09-18T00:00:00Z", last_log_lines=["HEALTH state=faulted"])
    base.update(kw)
    return m.TaskHealthEvent(**base)


# ── Models ───────────────────────────────────────────────────────────────────

def test_process_status_health_defaults_ok_and_parses_fault():
    ps = m.ProcessStatus(name="chirp", description="", state="running")
    assert ps.health == m.TaskHealth.OK and ps.health_detail == "" and ps.last_output_at is None
    ps2 = m.ProcessStatus(name="chirp", description="", state="running",
                          health="rf_fault", health_detail="vmcircbuf")
    assert ps2.health == m.TaskHealth.RF_FAULT and ps2.health_detail == "vmcircbuf"


def test_sequence_run_fault_fields_default_and_parse():
    r = m.SequenceRun(id="r1", sequence_id="s", sequence_name="S", on_air_at="t")
    assert r.fault == "" and r.fault_task == "" and r.fault_at == ""
    r2 = m.SequenceRun(id="r1", sequence_id="s", sequence_name="S", on_air_at="t",
                       fault="chirp: vmcircbuf", fault_task="chirp", fault_at="t2")
    assert r2.fault_task == "chirp"


def test_fault_snapshot_all_optional_and_nests_on_event():
    snap = m.FaultSnapshot(captured_at="t", shm_used_bytes=10, vmcircbuf_backend_env="mmap_shm_open",
                           notes=["pid gone"])
    ev = _health_event(snapshot=snap)
    assert isinstance(ev.snapshot, m.FaultSnapshot)
    assert ev.snapshot.vmcircbuf_backend_env == "mmap_shm_open" and ev.snapshot.notes == ["pid gone"]
    # a bare snapshot (everything defaulted) still validates
    assert m.FaultSnapshot().captured_at == ""


# ── SSE classification ─────────────────────────────────────────────────────────

def test_classify_routes_task_health_to_its_own_model_not_taskevent():
    ev = classify({"type": "task_health", "unit_id": "u1", "task_name": "chirp",
                   "detail": "vmcircbuf", "at": "t", "last_log_lines": ["boom"]})
    assert isinstance(ev, m.TaskHealthEvent) and not isinstance(ev, m.TaskEvent)
    assert ev.health == m.TaskHealth.RF_FAULT and ev.detail == "vmcircbuf"


def test_classify_plain_task_event_still_taskevent():
    ev = classify({"type": "task_started", "unit_id": "u", "task_name": "c",
                   "state": "running", "at": "t"})
    assert isinstance(ev, m.TaskEvent)


def test_classify_sequence_rf_fault_is_sequencewebhook():
    ev = classify({"type": "sequence_rf_fault", "unit_id": "u", "run_id": "r",
                   "sequence_name": "S", "on_air_at": "t", "state": "running", "at": "t",
                   "detail": "chirp: vmcircbuf"})
    assert isinstance(ev, m.SequenceWebhook) and ev.type == "sequence_rf_fault"


# ── Theme ──────────────────────────────────────────────────────────────────────

def test_rf_fault_status_is_red():
    for key in ("rf_fault", "rf fault", "fault"):
        assert status_color(key)[0] == Palette.CRASH


# ── Alert feed ─────────────────────────────────────────────────────────────────

def test_alert_feed_describes_rf_fault_red_and_alert():
    line, is_alert = _describe(_health_event())
    assert is_alert is True and "RF FAULT" in line and "chirp" in line


def test_alert_feed_emits_fault_raised_and_alert_for_a_health_event():
    fe = AlertFeed()
    faults, alerts = [], []
    fe.fault_raised.connect(faults.append)
    fe.alert_raised.connect(alerts.append)
    fe.add_event(_health_event())
    assert len(faults) == 1 and isinstance(faults[0], m.TaskHealthEvent)
    assert len(alerts) == 1 and "RF FAULT" in alerts[0]


def test_alert_feed_plain_task_event_raises_nothing():
    fe = AlertFeed()
    faults, alerts = [], []
    fe.fault_raised.connect(faults.append)
    fe.alert_raised.connect(alerts.append)
    fe.add_event(m.TaskEvent(type="task_started", unit_id="u", task_name="c",
                             state="running", at="t"))
    assert faults == [] and alerts == []


def test_alert_feed_sequence_rf_fault_is_alert_level():
    ev = m.SequenceWebhook(type="sequence_rf_fault", unit_id="u", run_id="r", sequence_name="S",
                           on_air_at="t", state="running", at="t", detail="chirp: vmcircbuf")
    line, is_alert = _describe(ev)
    assert is_alert is True and "RF FAULT" in line


# ── Task row pill (unit detail) ─────────────────────────────────────────────────

def _status(name="tx", state=m.ProcessState.RUNNING, **kw):
    return m.ProcessStatus(name=name, description="", state=state, **kw)


def test_task_row_shows_fault_pill_over_state():
    hub = _Hub()
    # Healthy → the ordinary state pill, "Log" button.
    row = _TaskRow("u", _status(pid=123), hub)
    assert row._pill.text() == "RUNNING" and row._logs.text() == "Log"
    assert row._faulted is False
    # A fault on the SAME running task overrides the pill to red RF FAULT + relabels the log.
    row.update_status(_status(pid=123, health="rf_fault", health_detail="vmcircbuf"))
    assert row._faulted is True
    assert row._pill.text() == "RF FAULT"
    assert status_color("rf_fault")[0] == Palette.CRASH
    assert row._logs.text() == "Fault log"
    assert "vmcircbuf" in row._info.text()
    # Recovering (health back to OK) restores the ordinary pill + button.
    row.update_status(_status(pid=123))
    assert row._faulted is False and row._pill.text() == "RUNNING" and row._logs.text() == "Log"


# ── Sequence row pill ───────────────────────────────────────────────────────────

def _seq():
    return m.Sequence(id="s1", name="LoL test", steps=[])


def _kw():
    return dict(on_start=lambda s: None, on_stop=lambda s: None, on_edit=lambda s: None,
                on_delete=lambda s: None, on_log=lambda s: None, can_run=True, can_edit=False)


def test_sequence_row_shows_rf_fault_pill_when_run_faulted():
    run = m.SequenceRun(id="r1", sequence_id="s1", sequence_name="LoL test",
                        state=m.SequenceState.RUNNING, on_air_at="2030-01-01T00:00:00+00:00",
                        fault="chirp: vmcircbuf", fault_task="chirp", fault_at="t")
    row = sp._SequenceRow(_seq(), run, **_kw())
    assert row._pill.text() == "RF FAULT"

    # A healthy running run shows the ordinary state.
    ok = m.SequenceRun(id="r1", sequence_id="s1", sequence_name="LoL test",
                       state=m.SequenceState.RUNNING, on_air_at="2030-01-01T00:00:00+00:00")
    row2 = sp._SequenceRow(_seq(), ok, **_kw())
    assert row2._pill.text() == "RUNNING"


# ── Fleet card (Units grid) ─────────────────────────────────────────────────────

def test_unit_card_task_line_calls_out_rf_fault_loudest():
    from ui.unit_card import UnitCard
    card = UnitCard("u1")
    # Healthy: plain running count.
    card.update_tasks([_status("tx", pid=1), _status("rx", state=m.ProcessState.STOPPED)])
    assert card._tasks.text() == "1 running"
    # A fault wins over a crash on the same card.
    card.update_tasks([_status("tx", pid=1, health="rf_fault"),
                       _status("rx", state=m.ProcessState.CRASHED)])
    assert "RF FAULT" in card._tasks.text() and Palette.CRASH in card._tasks.styleSheet()
    # Crash-only (no fault) keeps the crash line.
    card.update_tasks([_status("tx", pid=1), _status("rx", state=m.ProcessState.CRASHED)])
    assert "crash" in card._tasks.text() and "RF FAULT" not in card._tasks.text()


# ── Capability gate (Phase-2 restart, not the Phase-1 pill/alarm) ───────────────

class _Client:
    def __init__(self, caps, ver="1.28.0"):
        self._caps = caps
        self.agent_version = ver

    def supports(self, cap):
        return cap in self._caps


def test_task_rf_health_supported_gate():
    assert tlm.task_rf_health_supported(_Client(["task-rf-health"])) is True
    assert tlm.task_rf_health_supported(_Client([])) is False


# ── Fault diagnosis dialog ──────────────────────────────────────────────────────

def test_diagnosis_rows_flag_the_vmcircbuf_suspects():
    from ui.fault_detail_dialog import _diagnosis_rows
    # A SysV fallback backend + a nearly-full /dev/shm + maps near the ceiling — all suspect.
    snap = m.FaultSnapshot(
        vmcircbuf_backend_env="sysv_shm", vmcircbuf_backend_compiled="mmap_shm_open",
        shm_used_bytes=95, shm_total_bytes=100,
        map_count=250000, map_max=262144, nofile_soft=1024, nofile_hard=4096,
        task_home="/root", notes=["pid gone"])
    rows = _diagnosis_rows(snap)
    by = {r.label: r for r in rows}
    assert by["GR buffer backend"].suspect is True
    assert "compiled default" in by["GR buffer backend"].value
    assert by["/dev/shm"].suspect is True
    assert by["VMA maps (vm.max_map_count)"].suspect is True
    assert by["Open-file limit (soft/hard)"].suspect is False


def test_diagnosis_rows_flag_a_leaky_compiled_default_when_env_unset():
    from ui.fault_detail_dialog import _diagnosis_rows
    # The P0 GR pin knob is disabled → the task's GR_CONF_* env is unset (env ""), so the EFFECTIVE
    # backend is the compiled default. A sysv_shm compiled default is the leaky suspect and must be
    # flagged even though env is empty (the env-only check would have missed it — the confirmed bug).
    snap = m.FaultSnapshot(vmcircbuf_backend_env="", vmcircbuf_backend_compiled="sysv_shm")
    row = {r.label: r for r in _diagnosis_rows(snap)}["GR buffer backend"]
    assert row.suspect is True and "sysv_shm" in row.value and row.hint
    # env "" + a mmap compiled default is NOT suspect.
    snap2 = m.FaultSnapshot(vmcircbuf_backend_env="", vmcircbuf_backend_compiled="mmap_shm_open")
    assert {r.label: r for r in _diagnosis_rows(snap2)}["GR buffer backend"].suspect is False


def test_diagnosis_rows_healthy_backend_not_flagged():
    from ui.fault_detail_dialog import _diagnosis_rows
    snap = m.FaultSnapshot(vmcircbuf_backend_env="mmap_shm_open",
                           vmcircbuf_backend_compiled="mmap_shm_open",
                           shm_used_bytes=10, shm_total_bytes=1000,
                           map_count=1000, map_max=262144)
    by = {r.label: r for r in _diagnosis_rows(snap)}
    assert by["GR buffer backend"].suspect is False
    assert by["/dev/shm"].suspect is False
    assert by["VMA maps (vm.max_map_count)"].suspect is False


def test_fault_dialog_builds_with_and_without_snapshot():
    from ui.fault_detail_dialog import FaultDetailDialog
    # With a snapshot + log lines.
    ev = _health_event(snapshot=m.FaultSnapshot(vmcircbuf_backend_env="sysv_shm",
                                                map_count=260000, map_max=262144))
    dlg = FaultDetailDialog(ev)
    assert "chirp" in dlg.windowTitle()
    dlg.deleteLater()
    # A snapshot-less event still builds (just the banner + log).
    dlg2 = FaultDetailDialog(m.TaskHealthEvent(unit_id="u", task_name="t", at="t"))
    dlg2.deleteLater()


def test_alert_feed_double_click_opens_fault_dialog(monkeypatch):
    import ui.fault_detail_dialog as fdd
    opened = []

    class _Stub:
        def __init__(self, ev, parent=None):
            opened.append(ev)

        def exec(self):
            return 0

    monkeypatch.setattr(fdd, "FaultDetailDialog", _Stub)
    fe = AlertFeed()
    fe.add_event(_health_event())               # fault row at index 0
    fe.add_event(m.TaskEvent(type="task_started", unit_id="u", task_name="c",
                             state="running", at="t"))   # a plain row
    # Double-click the fault row → opens the dialog with the event.
    fe._on_item_activated(fe._list.item(1))
    assert len(opened) == 1 and isinstance(opened[0], m.TaskHealthEvent)
    # Double-click the plain row → nothing.
    fe._on_item_activated(fe._list.item(0))
    assert len(opened) == 1


# ── Main-window alarm handlers (no-op safe headless) ────────────────────────────

def test_alarm_handlers_are_headless_safe():
    from ui.main_window import MainWindow
    # Bind the unbound handlers onto a bare QMainWindow with just the fields they touch — the full
    # window needs a live DataHub, but the alarm logic only needs alert_feed + _tray + the window.
    win = QMainWindow()
    win.alert_feed = AlertFeed()
    win._tray = None
    win._notify_tray = MainWindow._notify_tray.__get__(win, QMainWindow)   # the real helper
    # A general alert: beep + taskbar flash + expand feed — must not raise headless.
    MainWindow._on_alert(win, "u1 · chirp RF FAULT (radio silent)")
    # The loud fault alarm: raise/activate + a guarded tray balloon (no tray offscreen → no-op).
    MainWindow._on_fault(win, _health_event())
    # _notify_tray early-returns where no system tray exists; it never builds one here.
    assert win._tray is None
    win.deleteLater()
