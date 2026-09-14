"""Dev-only: render SequenceEditorDialog offscreen to a PNG (redesign eyeball)."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import QApplication
import api.models as m
from ui.theme import apply_theme
from ui.sequence_editor import SequenceEditorDialog

_TASKS_YAML = """
tasks:
  - name: gps_ca
    command: [python3, /x/gps_ca.py]
  - name: chirp
    command: [python3, /x/chirp.py]
  - name: marker
    command: [python3, /x/marker.py]
"""

class FakeUnit:
    def list_tasks(self):
        return [SimpleNamespace(name=n) for n in ("gps_ca", "chirp", "marker")]
    def get_tasks_yaml(self):
        return _TASKS_YAML
    def get_calibration(self):
        raise Exception("uncalibrated")   # relative power is fine for a visual check

class FakeHub(QObject):
    task_done = pyqtSignal(str, object)
    def __init__(self):
        super().__init__()
        self.fleet = SimpleNamespace(get=lambda h: FakeUnit())
    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)

app = QApplication.instance() or QApplication([])
apply_theme(app)

RAMP_UP = m.RampSpec(start=-90.0, stop=-50.0, steps=6, duration_s=120.0, param="power")
RAMP_DN = m.RampSpec(start=-50.0, stop=-90.0, steps=6, duration_s=120.0, param="power")
def st(**k): k.setdefault("offset_s", 0.0); return m.SequenceStep(**k)
steps = [
    st(anchor="start", offset_s=0.0, action="start", task_name="gps_ca", args=["--freq","1575.42"]),
    st(anchor="stop", offset_s=0.0, action="stop", task_name="gps_ca"),
    st(anchor="start", offset_s=0.0, action="ramp", task_name="gps_ca", ramp=RAMP_UP, id="up"),
    st(anchor="step", offset_s=180.0, action="ramp", task_name="gps_ca", ramp=RAMP_DN,
       anchor_step_id="up", anchor_edge="end"),
    st(anchor="start", offset_s=30.0, action="start", task_name="chirp", args=["--freq","1227.6"]),
    st(anchor="stop", offset_s=0.0, action="stop", task_name="chirp"),
    st(anchor="start", offset_s=30.0, action="tune", task_name="chirp", params={"rf": 1}),
    st(anchor="start", offset_s=360.0, action="run", task_name="marker", args=[]),
]
seq = m.Sequence(id="s1", name="GPS L1 C/A — reacquisition sweep", description="", steps=steps)

dlg = SequenceEditorDialog(FakeHub(), "broadcaster-2", sequence=seq)
dlg.resize(1180, 620)
dlg.show()
app.processEvents()
dlg._timeline._fit()
app.processEvents()
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dlg.png"
dlg.grab().save(out)
print("saved", out, dlg.size())
