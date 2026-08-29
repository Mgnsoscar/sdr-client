"""Active components (client side): local_calibration_issues validates a plane's
``control`` block, and the calibration panel's active-stage editor creates, edits, renders
and round-trips it."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QInputDialog

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
    # Loading an active-component doc and reading the form back must not drop `control`.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control(engage_pct=25.0)))
    back = p._read_form(strict=False)
    ctrl = back["chain"]["planes"]["atten_out"].get("control")
    assert ctrl is not None
    assert ctrl["task"] == "atten_set" and ctrl["engage_pct"] == 25.0


# ── the dedicated active-stage editor ───────────────────────────────────────────────

def _active_row(p):
    return next(r for r in p._f["planes"] if r.get("role") == "active")


def test_loaded_active_plane_has_active_role_and_control():
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control(max_db=60.0)))
    row = _active_row(p)
    assert row["control"]["task"] == "atten_set"
    assert row["control"]["max_db"] == 60.0


def test_add_active_stage_seeds_a_control_block(monkeypatch):
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))                      # start from a valid chain
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("pad_out", True)))
    p._add_active_stage()
    doc = p._read_form(strict=False)
    plane = doc["chain"]["planes"]["pad_out"]
    assert plane["type"] == "derived" and plane.get("control") is not None
    assert plane["control"]["sense"] == "attenuation"


def test_editing_the_control_reads_back():
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))
    row = _active_row(p)
    row["control"]["max_db"] = 50.0                   # as the editor's spinbox would
    row["control"]["param"] = "att"
    back = p._read_form(strict=False)
    ctrl = back["chain"]["planes"]["atten_out"]["control"]
    assert ctrl["max_db"] == 50.0 and ctrl["param"] == "att"


def test_active_detail_renders_without_error():
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))
    p._select_plane("atten_out")                      # renders _detail_active
    assert p._selected_plane == "atten_out"


def test_doc_uses_active_components_detects_control():
    assert CalibrationPanel._doc_uses_active_components(_doc(_control())) is True
    passive = _doc(_control()); del passive["chain"]["planes"]["atten_out"]["control"]
    assert CalibrationPanel._doc_uses_active_components(passive) is False


def test_task_and_param_pickers_read_from_fetched_data():
    p = CalibrationPanel("u", FakeHub())
    p._tasks_yaml = (
        "tasks:\n"
        "  - name: atten_set\n"
        "    command: [python3, atten.py, --attenuation, \"0\"]\n"
        "  - name: chirp\n"
        "    command: [python3, chirp.py]\n")
    assert p._all_task_names() == ["atten_set", "chirp"]
    assert p._task_script("atten_set") == "atten.py"
    p._task_params["atten.py"] = [
        {"dest": "attenuation", "type": "float", "flags": ["--attenuation"]},
        {"dest": "label", "type": "str", "flags": ["--label"]}]
    assert p._numeric_params_for("atten_set") == ["attenuation"]   # numeric only
