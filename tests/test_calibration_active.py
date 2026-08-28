"""Active components (client side): local_calibration_issues validates a plane's
``control`` block, and an active stage's control survives a form round-trip (before the
dedicated editor lands)."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from state import ComponentCatalog
from ui.calibration_panel import CalibrationPanel, local_calibration_issues

_app = QApplication.instance() or QApplication([])


class FakeFleet:
    def __init__(self, client):
        self._c = client
        self._catalog = ComponentCatalog()
    def get(self, host):
        return self._c
    def component_catalog(self):
        return self._catalog


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)
    def __init__(self, client=None):
        super().__init__()
        self.fleet = FakeFleet(client)
    def run_async(self, label, fn):
        pass


def _control(**over):
    c = {"task": "atten_set", "param": "attenuation", "sense": "attenuation",
         "min_db": 0.0, "max_db": 95.0, "step_db": 0.25, "engage_pct": 0.0}
    c.update(over)
    return c


def _doc(control):
    return {
        "schema_version": 1, "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 40.0, "gain_step_db": 1.0},
            "operating_plane": "atten_out",
            "planes": {
                "sdr_output": {"type": "measured"},
                "atten_out": {"type": "derived", "from": "sdr_output", "delta_db": 0.0,
                              "control": control},
            },
        },
        "signals": {},
    }


def test_valid_active_control_has_no_issues():
    assert local_calibration_issues(_doc(_control())) == []


@pytest.mark.parametrize("bad,frag", [
    (_control(task=""), "task"),
    (_control(param=""), "parameter"),
    (_control(sense="weird"), "sense"),
    (_control(min_db=5.0, max_db=5.0), "max"),
    (_control(step_db=0.0), "step"),
    (_control(engage_pct=150.0), "engage"),
])
def test_bad_active_control_is_flagged(bad, frag):
    issues = local_calibration_issues(_doc(bad))
    assert any(frag in i for i in issues), issues


def test_control_survives_form_round_trip():
    # Loading an active-component doc and reading the form back must not drop `control`
    # (the dedicated editor lands later; until then, don't lose the data).
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control(engage_pct=25.0)))
    back = p._read_form(strict=False)
    ctrl = back["chain"]["planes"]["atten_out"].get("control")
    assert ctrl is not None
    assert ctrl["task"] == "atten_set" and ctrl["engage_pct"] == 25.0
