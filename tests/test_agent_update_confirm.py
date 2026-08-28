"""After an OTA version flip, the update dialog waits for the unit to confirm the new
release healthy (via /admin/update-status) and surfaces an auto-rollback — instead of
declaring success just before a silent revert."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui.agent_update_dialog import AgentUpdateDialog, CAP_OTA_STATUS

_app = QApplication.instance() or QApplication([])


class _Client:
    def __init__(self, caps=(CAP_OTA_STATUS,)):
        self.label = "unit"
        self._caps = list(caps)

    def supports(self, cap):
        return cap in self._caps

    def info(self):
        return m.AgentInfo(hostname="u", unit_id="u", agent_version="1.1.5",
                           python_version="3.11", tasks=[])


class _Hub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, client):
        super().__init__()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        pass                       # tests drive the handlers directly


def _dialog(client):
    dlg = AgentUpdateDialog(_Hub(client), "unit")
    dlg._target = "1.1.5"
    dlg._from_version = "1.1.0"
    return dlg


def test_version_flip_enters_confirm_phase_when_capable():
    dlg = _dialog(_Client())
    info = m.AgentInfo(hostname="u", unit_id="u", agent_version="1.1.5",
                       python_version="3.11", tasks=[])
    dlg._on_poll(info)
    assert dlg._phase == "confirm"
    assert "confirm it healthy" in dlg._status.text()


def test_confirm_reports_healthy():
    dlg = _dialog(_Client())
    dlg._phase = "confirm"
    dlg._on_confirm({"current_version": "1.1.5", "previous_version": "1.1.0",
                     "pending_version": None, "pending_confirmed": True})
    assert "confirmed healthy" in dlg._status.text()
    assert dlg._current == "1.1.5"


def test_confirm_detects_rollback():
    dlg = _dialog(_Client())
    dlg._phase = "confirm"
    # Reverted to the version we came from, pending cleared → rolled back.
    dlg._on_confirm({"current_version": "1.1.0", "previous_version": "1.0.0",
                     "pending_version": None, "pending_confirmed": False})
    assert "rolled back" in dlg._status.text()
    assert dlg._current == "1.1.0"


def test_restart_phase_detects_rollback_fast():
    # While still waiting for the new version to boot, an OTA-status poll showing the
    # unit back on the version we came from (pending cleared) means the new release
    # failed to start — report it now instead of hanging until the restart deadline.
    dlg = _dialog(_Client())
    dlg._phase = "restart"
    dlg._busy = True
    dlg._on_restart_status({"current_version": "1.1.0", "previous_version": "1.0.0",
                            "pending_version": None, "pending_confirmed": False})
    assert dlg._busy is False
    assert dlg._current == "1.1.0"
    assert "failed to start" in dlg._status.text()
    assert "journalctl" in dlg._status.text()


def test_restart_phase_status_keeps_waiting_before_flip():
    # Right after activation the symlink is on the target with a pending marker — this
    # is a normal mid-restart state, not a rollback, so keep waiting.
    dlg = _dialog(_Client())
    dlg._phase = "restart"
    dlg._busy = True
    dlg._on_restart_status({"current_version": "1.1.5", "previous_version": "1.1.0",
                            "pending_version": "1.1.5", "pending_confirmed": False})
    assert dlg._busy is True                 # still waiting
    assert "failed to start" not in dlg._status.text()


def test_restart_phase_ignores_unreachable_status():
    dlg = _dialog(_Client())
    dlg._phase = "restart"
    dlg._busy = True
    dlg._on_restart_status(ConnectionError("unit restarting"))
    assert dlg._busy is True                 # transient error mid-restart → keep waiting


def test_log_accumulates_phase_lines():
    dlg = _dialog(_Client())
    info = m.AgentInfo(hostname="u", unit_id="u", agent_version="1.1.5",
                       python_version="3.11", tasks=[])
    dlg._on_poll(info)          # version flip → back online + confirm phase
    dlg._on_confirm({"current_version": "1.1.5", "previous_version": "1.1.0",
                     "pending_version": None, "pending_confirmed": True})
    log = dlg._log.toPlainText()
    assert "back online on 1.1.5" in log
    assert "confirm the new release healthy" in log
    assert "now running 1.1.5" in log


def test_flip_without_capability_finishes_immediately():
    dlg = _dialog(_Client(caps=()))          # agent doesn't advertise ota-status
    info = m.AgentInfo(hostname="u", unit_id="u", agent_version="1.1.5",
                       python_version="3.11", tasks=[])
    dlg._on_poll(info)
    assert dlg._phase != "confirm"
    assert "now running 1.1.5" in dlg._status.text()


def _run(state):
    return m.SequenceRun(id="run_1", sequence_id="seq_1", sequence_name="broadcast",
                         state=state, on_air_at="2026-01-01T00:00:00+00:00")


def test_precheck_proceeds_when_no_active_run(monkeypatch):
    dlg = _dialog(_Client())
    called = []
    monkeypatch.setattr(dlg, "_do_update", lambda: called.append(True))
    dlg._confirm_and_update([_run(m.SequenceState.COMPLETED)])
    assert called == [True]              # no armed/running run → straight to update


def test_precheck_warns_and_cancels_when_running(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    dlg = _dialog(_Client())
    dlg._busy = True
    called = []
    monkeypatch.setattr(dlg, "_do_update", lambda: called.append(True))
    # Operator declines the "sequence on air — update anyway?" prompt.
    monkeypatch.setattr(QMessageBox, "exec",
                        lambda self: QMessageBox.StandardButton.Cancel)
    dlg._confirm_and_update([_run(m.SequenceState.RUNNING)])
    assert called == []                  # update NOT started
    assert dlg._busy is False            # re-enabled for another try
    assert "in progress" in dlg._status.text()


def test_precheck_proceeds_when_running_confirmed(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    dlg = _dialog(_Client())
    called = []
    monkeypatch.setattr(dlg, "_do_update", lambda: called.append(True))
    monkeypatch.setattr(QMessageBox, "exec",
                        lambda self: QMessageBox.StandardButton.Yes)
    dlg._confirm_and_update([_run(m.SequenceState.RUNNING)])
    assert called == [True]              # operator confirmed → update proceeds
