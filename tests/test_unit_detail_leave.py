"""The unsaved-calibration leave guard: when the Calibration sub-tab holds unsaved
edits, navigating away (another sub-tab, Back, or the top-level app tab) prompts
Save / Don't save / Cancel. The modal decision is stubbed via _ask_unsaved_decision so
the branching is testable headlessly."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from ui.unit_detail import UnitDetail

_app = QApplication.instance() or QApplication([])

_DOC = {
    "schema_version": 1, "unit_id": "u1", "unit_type": "broadcaster",
    "chain": {
        "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
        "operating_plane": "sdr_output",
        "limits": [{"plane": "sdr_output", "max_dbm": -2.5}],
        "planes": {"sdr_output": {"type": "measured", "quantity": "total in-band power"}},
    },
    "defaults": {"amplitude": 0.5},
    "signals": {"mock": {"curves": {"sdr_output": {
        "points": [{"gain_db": 40, "power_dbm": -36}, {"gain_db": 74, "power_dbm": -2.5}]}}}},
}


class FakeClient:
    label = "unit-1"

    def __init__(self):
        self.uploaded = []

    def get_calibration(self):
        raise AgentHTTPError("u", 404, "Not Found")     # panel loads empty; we set a doc by hand

    def get_components(self):
        return ""

    def get_tasks_yaml(self):
        return "tasks: []"

    def list_sequences(self):
        return []

    def list_sequence_runs(self):
        return []

    def supports(self, cap):
        return cap in ("calibration",)

    def upload_components(self, wire):
        return {"saved": "components.yaml"}

    def upload_file(self, name, content):
        self.uploaded.append((name, content))
        return {"saved": name, "calibration": {}}


class FakeFleet:
    def __init__(self, client):
        self._c = client

    def get(self, host):
        return self._c


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)
    event_received = pyqtSignal(object)

    def __init__(self, client):
        super().__init__()
        self.fleet = FakeFleet(client)

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)

    def refresh_now(self, *a, **k):
        pass


def _detail_on_calibration():
    """A UnitDetail parked on the Calibration sub-tab with an unsaved edit pending."""
    client = FakeClient()
    back = {"n": 0}
    d = UnitDetail(FakeFleet(client), FakeHub(client), on_back=lambda: back.__setitem__("n", 1))
    d.set_unit("host-1")
    d._sub_stack.setCurrentIndex(d._CAL_SUBTAB)             # show Calibration
    panel = d._calibration_panel
    panel._set_doc(_DOC)                                    # a clean loaded doc
    panel._f["max_gain"].setText("50")                     # …then a real edit
    assert panel.has_unsaved_changes()
    return d, client, back


def test_leaving_subtab_cancel_keeps_you_on_calibration(monkeypatch):
    d, client, _ = _detail_on_calibration()
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "cancel")
    d._select_subtab(0)
    assert d._sub_stack.currentIndex() == d._CAL_SUBTAB     # stayed
    assert client.uploaded == []


def test_leaving_subtab_discard_switches_and_keeps_edits(monkeypatch):
    d, client, _ = _detail_on_calibration()
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "discard")
    d._select_subtab(0)
    assert d._sub_stack.currentIndex() == 0                 # left
    assert client.uploaded == []                            # nothing saved
    assert d._calibration_panel.has_unsaved_changes()       # edits still in the editor


def test_leaving_subtab_save_dispatches_then_switches(monkeypatch):
    d, client, _ = _detail_on_calibration()
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "save")
    d._select_subtab(0)
    assert client.uploaded                                  # the edit was pushed
    assert d._sub_stack.currentIndex() == 0                 # and we left


def test_back_button_prompts_and_cancel_stays(monkeypatch):
    d, client, back = _detail_on_calibration()
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "cancel")
    d._handle_back()
    assert back["n"] == 0                                   # did NOT navigate back
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "discard")
    d._handle_back()
    assert back["n"] == 1                                   # left on discard


def test_confirm_leave_public_delegates(monkeypatch):
    d, _, _ = _detail_on_calibration()
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "cancel")
    assert d.confirm_leave() is False                       # top-level tab switch blocked
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision", lambda self: "discard")
    assert d.confirm_leave() is True


def test_no_prompt_when_calibration_not_the_active_subtab(monkeypatch):
    # On the Tasks sub-tab, leaving never consults the calibration panel — even if it holds
    # edits, the user already left Calibration (and was warned then).
    d, _, _ = _detail_on_calibration()
    d._sub_stack.setCurrentIndex(0)                         # move to Tasks
    called = {"n": 0}
    monkeypatch.setattr(UnitDetail, "_ask_unsaved_decision",
                        lambda self: called.__setitem__("n", called["n"] + 1) or "cancel")
    assert d.confirm_leave() is True
    assert called["n"] == 0                                 # never prompted
