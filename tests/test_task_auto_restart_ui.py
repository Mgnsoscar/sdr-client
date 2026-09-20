"""
RF-fault RECOVERY Phase 3b (client) — the STANDALONE task "Auto-restart on fault" checkbox.

The agent (1.31.0, capability `task-auto-restart`) relaunches a standalone (non-run-owned) task that
RF-faults when its TaskConfig.auto_restart_on_fault is set; the Run… form may override it per launch via
StartRequest. This authors the flag in the task editor + the Run form, gated on the capability so a task
can't claim an auto-restart an older agent would silently drop.

Covered: the model mirror; the capability constant + gate; the task-editor checkbox (library always
offers it; a live unit gates + seeds + saves it); the Run-form checkbox (hidden when unsupported, sends
the per-launch override when supported).
"""
import os
import types

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api import models as m
from api.fleet import LIBRARY_HOST
from ui.timeline_model import TASK_AUTO_RESTART_CAPABILITY, task_auto_restart_supported

_app = QApplication.instance() or QApplication([])

PARAMS = {"params": [{"dest": "freq", "flags": ["-f", "--freq"], "type": "float"}]}


# ── the model mirror ─────────────────────────────────────────────────────────────

def test_taskconfig_mirrors_the_agent_fields():
    tc = m.TaskConfig(name="tx", command=["python3", "tx.py"])
    assert tc.auto_restart_on_fault is False
    assert tc.max_fault_restarts == 2
    tc2 = m.TaskConfig(**{**tc.model_dump(), "auto_restart_on_fault": True, "max_fault_restarts": 3})
    assert tc2.auto_restart_on_fault is True and tc2.max_fault_restarts == 3
    # skew-safe: an older agent that never sends the field parses fine.
    assert m.TaskConfig(name="x", command=["a"]).auto_restart_on_fault is False


def test_startrequest_override_defaults_to_none():
    assert m.StartRequest().auto_restart_on_fault is None
    assert m.StartRequest(auto_restart_on_fault=True).auto_restart_on_fault is True


# ── the capability gate ──────────────────────────────────────────────────────────

class _Caps:
    def __init__(self, caps):
        self._caps = list(caps)
    def supports(self, cap):
        return cap in self._caps


def test_capability_constant_and_gate():
    assert TASK_AUTO_RESTART_CAPABILITY == "task-auto-restart"
    assert task_auto_restart_supported(_Caps(["task-auto-restart"])) is True
    assert task_auto_restart_supported(_Caps([])) is False
    assert task_auto_restart_supported(None) is False   # tolerant of a bad client


# ── the task editor checkbox ─────────────────────────────────────────────────────

class _EditorClient:
    def __init__(self, caps=(), yaml="tasks: []\n", info_fails=False):
        self._caps = list(caps)
        self._yaml = yaml
        self._info_fails = info_fails
        self.saved = None
    def list_scripts(self):
        return ["mock_tx.py"]
    def get_script_params(self, name):
        return PARAMS
    def get_tasks_yaml(self):
        return self._yaml
    def info(self):
        if self._info_fails:
            raise RuntimeError("info timed out")
        return types.SimpleNamespace(capabilities=list(self._caps),
                                     scripts_dir="", task_interpreter="")
    def create_task(self, spec):
        self.saved = spec
        return {"created": spec.get("name")}
    def update_task(self, name, spec):
        self.saved = spec
        return {"updated": name}


class _Hub(QObject):
    task_done = pyqtSignal(str, object)
    def __init__(self, client):
        super().__init__()
        self.fleet = type("F", (), {"get": lambda self_, h: client,
                                    "__contains__": lambda self_, h: True})()
    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)
    def refresh_now(self, *a, **k):
        pass


def _editor(client, host="u", existing=None):
    from ui.task_editor import TaskEditorDialog
    return TaskEditorDialog(_Hub(client), host, existing_name=existing)


def test_library_always_offers_the_checkbox():
    dlg = _editor(_EditorClient(), host=LIBRARY_HOST)
    assert dlg._auto_restart.isEnabled() is True


def test_live_unit_with_capability_enables_the_checkbox():
    dlg = _editor(_EditorClient(caps=["task-auto-restart"]))
    assert dlg._auto_restart.isEnabled() is True


def test_live_unit_without_capability_disables_and_unchecks():
    dlg = _editor(_EditorClient(caps=[]))
    assert dlg._auto_restart.isEnabled() is False
    assert dlg._auto_restart.isChecked() is False


def test_save_round_trips_the_flag():
    client = _EditorClient(caps=["task-auto-restart"])
    dlg = _editor(client)
    dlg._name.setText("tx")
    dlg._select_script("mock_tx.py")
    dlg._auto_restart.setChecked(True)
    dlg._on_save()
    assert client.saved is not None
    assert client.saved["auto_restart_on_fault"] is True


def test_edit_seeds_the_checkbox_from_the_stored_task():
    yaml = ("tasks:\n"
            "  - name: tx\n"
            "    command: [python3, mock_tx.py]\n"
            "    auto_restart_on_fault: true\n")
    dlg = _editor(_EditorClient(caps=["task-auto-restart"], yaml=yaml), existing="tx")
    assert dlg._auto_restart.isChecked() is True


def test_edit_on_an_old_unit_leaves_the_stored_flag_unchecked_and_locked():
    yaml = ("tasks:\n"
            "  - name: tx\n"
            "    command: [python3, mock_tx.py]\n"
            "    auto_restart_on_fault: true\n")
    dlg = _editor(_EditorClient(caps=[], yaml=yaml), existing="tx")
    assert dlg._auto_restart.isEnabled() is False
    assert dlg._auto_restart.isChecked() is False


_YAML_ON = ("tasks:\n"
            "  - name: tx\n"
            "    command: [python3, mock_tx.py]\n"
            "    auto_restart_on_fault: true\n")


def test_save_on_an_unsupported_unit_preserves_the_stored_flag():
    """Editing an unrelated field on an old unit must NOT strip an auto-restart set elsewhere."""
    client = _EditorClient(caps=[], yaml=_YAML_ON)
    dlg = _editor(client, existing="tx")
    dlg._name.setText("tx")
    dlg._on_save()
    assert client.saved is not None
    assert client.saved["auto_restart_on_fault"] is True   # preserved from the stored entry


def test_save_when_info_never_arrives_preserves_the_stored_flag():
    """If /info fails, support is unconfirmed → the disabled box must not clobber the stored True."""
    client = _EditorClient(caps=["task-auto-restart"], yaml=_YAML_ON, info_fails=True)
    dlg = _editor(client, existing="tx")
    dlg._name.setText("tx")
    dlg._on_save()
    assert client.saved is not None
    assert client.saved["auto_restart_on_fault"] is True   # preserved (not coerced to False)


# ── the Run form checkbox ────────────────────────────────────────────────────────

RUN_PARAMS = {"params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float",
     "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "help": "power"},
]}


class _RunClient:
    def __init__(self, caps=(), auto=False):
        self._caps = list(caps)
        self._yaml = (
            "tasks:\n"
            "  - name: mocktask\n"
            "    command: [python3, mock_tx.py, --power, \"-30\"]\n"
            f"    auto_restart_on_fault: {'true' if auto else 'false'}\n"
        )
        self.started = []
    def supports(self, cap):
        return cap in self._caps
    def get_tasks_yaml(self):
        return self._yaml
    def get_script_params(self, name):
        return RUN_PARAMS
    def get_calibration(self):
        from api.client import AgentHTTPError
        raise AgentHTTPError("u", 404, "none")       # uncalibrated → schema range
    def start_task(self, name, req=None):
        self.started.append((name, req))
        return {}


def _run_dialog(client):
    from ui.run_task_dialog import RunTaskDialog
    return RunTaskDialog(_Hub(client), "u", "mocktask")


def test_run_form_hides_the_checkbox_on_an_old_unit():
    # isHidden(), not isVisible(): a child of an unshown top-level is never "visible", so the old
    # assertion held whatever the gate did (tests-as-spec critic).
    dlg = _run_dialog(_RunClient(caps=[]))
    assert dlg._auto_restart.isHidden() is True
    assert _run_dialog(_RunClient(caps=["task-auto-restart"]))._auto_restart.isHidden() is False


def test_run_form_shows_and_seeds_the_checkbox_when_supported():
    dlg = _run_dialog(_RunClient(caps=["task-auto-restart"], auto=True))
    assert dlg._auto_restart_supported is True
    assert dlg._auto_restart.isChecked() is True         # seeded from the stored default


def test_run_form_sends_the_override_when_supported():
    client = _RunClient(caps=["task-auto-restart"], auto=False)
    dlg = _run_dialog(client)
    dlg._auto_restart.setChecked(True)
    dlg._on_run()
    assert client.started, "start_task was not called"
    name, req = client.started[-1]
    assert req.auto_restart_on_fault is True


def test_run_form_leaves_override_none_on_an_old_unit():
    client = _RunClient(caps=[])
    dlg = _run_dialog(client)
    dlg._on_run()
    assert client.started
    _, req = client.started[-1]
    assert req.auto_restart_on_fault is None             # never claims an unsupported feature
