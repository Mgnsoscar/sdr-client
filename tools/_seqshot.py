"""Dev-only: render the TimelineEditor offscreen to a PNG, to eyeball the redesign.
Usage: QT_QPA_PLATFORM=offscreen python3 tools/_seqshot.py /tmp/out.png"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QSize
import api.models as m
from ui.theme import apply_theme
from ui.timeline_editor import TimelineEditor

app = QApplication.instance() or QApplication([])
apply_theme(app)

def step(**k):
    k.setdefault("offset_s", 0.0)
    return m.SequenceStep(**k)

RAMP_UP = m.RampSpec(start=-90.0, stop=-50.0, steps=6, duration_s=120.0, param="power")
RAMP_DN = m.RampSpec(start=-50.0, stop=-90.0, steps=6, duration_s=120.0, param="power")

steps = [
    step(anchor="start", offset_s=0.0,   action="start", task_name="gps_ca", args=["--freq","1575.42"]),
    step(anchor="stop",  offset_s=0.0,   action="stop",  task_name="gps_ca"),
    step(anchor="start", offset_s=0.0,   action="ramp",  task_name="gps_ca", ramp=RAMP_UP, id="up"),
    step(anchor="step",  offset_s=180.0, action="ramp",  task_name="gps_ca", ramp=RAMP_DN,
         anchor_step_id="up", anchor_edge="end"),
    step(anchor="start", offset_s=30.0,  action="start", task_name="chirp", args=["--freq","1227.6"]),
    step(anchor="stop",  offset_s=0.0,   action="stop",  task_name="chirp"),
    step(anchor="start", offset_s=30.0,  action="tune",  task_name="chirp", params={"rf": 1}),
    step(anchor="start", offset_s=360.0, action="run",   task_name="marker", args=[]),
]

ed = TimelineEditor()
ed._tasks = ["gps_ca", "chirp", "marker"]
ed.set_task_commands({t: ["python3", f"/x/{t}.py"] for t in ed._tasks})
ed.set_steps(steps)
ed.resize(1180, 480)
ed.show()
app.processEvents()
# fit-to-view so the whole 7-min defined region + hatch + off-air are visible (like the mockup)
ed._fit()
app.processEvents()
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/seq_qt.png"
ed.grab().save(out)
print("saved", out, ed.size())
