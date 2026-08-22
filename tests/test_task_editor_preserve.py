"""Editing a task must preserve stored fields the form doesn't model (restart tuning,
resume config) instead of resetting them to defaults — otherwise editing a resumable
ramp task via the UI would silently break sequence-resume."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.fleet import LIBRARY_HOST
from ui.task_editor import TaskEditorDialog

_app = QApplication.instance() or QApplication([])

PARAMS = {"params": [{"dest": "freq", "flags": ["-f", "--freq"], "type": "float"}]}


class Client:
    def __init__(self):
        self.saved = {}

    def list_scripts(self):
        return ["mock_tx.py"]

    def get_script_params(self, name):
        return PARAMS

    def get_tasks_yaml(self):
        return "tasks: []\n"

    def update_task(self, name, spec):
        self.saved = spec
        return {"updated": name}


class Hub(QObject):
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

    def refresh_now(self, host):
        pass


def test_edit_preserves_restart_and_resume_fields():
    client = Client()
    dlg = TaskEditorDialog(Hub(client), LIBRARY_HOST, existing_name="ramp")
    # Simulate the stored entry the prefill would have captured.
    dlg._orig_entry = {
        "name": "ramp", "command": ["python3", "mock_tx.py"],
        "resumable": True, "max_restarts": 10, "restart_window_s": 120.0,
        "resume_offset_flag": "--resume-offset",
    }
    dlg._name.setText("ramp")
    dlg._select_script("mock_tx.py")
    dlg._on_save()

    spec = client.saved
    assert spec["name"] == "ramp"
    assert spec["resumable"] is True                 # preserved
    assert spec["max_restarts"] == 10                # preserved
    assert spec["restart_window_s"] == 120.0         # preserved
    assert spec["resume_offset_flag"] == "--resume-offset"
    assert spec["command"][-1].endswith("mock_tx.py")  # still edited/rebuilt
