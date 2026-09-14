"""Headless render of the Hold-edit dialog in its LOCKED mode (the elapsed window frosted).

    QT_QPA_PLATFORM=offscreen python3 tools/hold_edit_shot.py /tmp/hold_edit_locked.png
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from api import models as m  # noqa: E402

app = QApplication.instance() or QApplication([])


class _EditHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        client = type("C", (), {
            "list_tasks": lambda self_: [type("T", (), {"name": "gps_ca"})()],
            "get_tasks_yaml": lambda self_: ("tasks:\n  - name: gps_ca\n"
                                             "    command: [python3, gps_ca.py]\n"),
            "get_calibration": lambda self_: {"unit_type": "broadcaster", "valid": True,
                                              "signals": {}},
        })()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _seq():
    up = m.RampSpec(param="power", start=-90.0, stop=-50.0, steps=6, duration_s=105.0)  # crosses
    down = m.RampSpec(param="power", start=-50.0, stop=-90.0, steps=3, duration_s=60.0)
    return m.Sequence(id="s1", name="LoL test", steps=[
        m.SequenceStep(anchor="start", offset_s=-10.0, action="start", task_name="gps_ca",
                       args=["--rf", "off"]),
        m.SequenceStep(anchor="start", offset_s=0.0, action="tune", task_name="gps_ca",
                       params={"rf": "on"}),
        m.SequenceStep(anchor="start", offset_s=40.0, action="tune", task_name="gps_ca",
                       params={"sidelobes": 7}),
        m.SequenceStep(anchor="start", offset_s=45.0, action="ramp", task_name="gps_ca",
                       ramp=up, id="up"),
        m.SequenceStep(anchor="start", offset_s=105.0, action="hold", task_name=""),
        m.SequenceStep(anchor="hold", offset_s=60.0, action="ramp", task_name="gps_ca",
                       ramp=down, id="down"),
        m.SequenceStep(anchor="stop", offset_s=-10.0, action="tune", task_name="gps_ca",
                       params={"rf": "off"}),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="gps_ca"),
    ])


from ui.hold_edit_dialog import HoldEditDialog  # noqa: E402

dlg = HoldEditDialog(_EditHub(), "rpi-gnss-07", _seq())
dlg.resize(1180, 640)
dlg.show()
app.processEvents()
dlg._timeline._fit()
app.processEvents()
cv = dlg._timeline._canvas
for it in cv.items():
    print(f"{getattr(it, 'action', 'bar'):5} anchor={getattr(it, 'anchor', getattr(it, 'start_anchor', '-')):6} "
          f"off={getattr(it, 'offset', getattr(it, 'start_offset', 0)):>7} elapsed={cv.elapsed_kind(it)}")
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/hold_edit_locked.png"
dlg.grab().save(out)
print("saved", out)
