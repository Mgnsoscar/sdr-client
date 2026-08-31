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


def _doc_table(control, table):
    # The active component's OWN inline Δ dB(f) baseline table (not a library reference).
    d = _doc(control)
    d["chain"]["planes"]["atten_out"] = {
        "type": "derived", "from": "sdr_output", "delta_db_by_freq": table, "control": control}
    return d


def test_active_inline_table_baseline_round_trips():
    # A frequency-dependent insertion loss is the active component's OWN inline Δ dB(f) table,
    # kept on round-trip and written as `delta_db_by_freq` (not a component ref, not a
    # constant) with the control on top.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc_table(_control(), [[1.0e9, -4.0], [2.0e9, -6.0]]))
    row = _active_row(p)
    assert row["baseline_table"] == [[1.0e9, -4.0], [2.0e9, -6.0]]
    plane = p._read_form(strict=False)["chain"]["planes"]["atten_out"]
    assert plane.get("delta_db_by_freq") == [[1.0e9, -4.0], [2.0e9, -6.0]]
    assert "component" not in plane and "delta_db" not in plane
    assert plane["control"]["task"] == "atten_set"


def test_active_baseline_switch_to_table_reads_back():
    # The baseline-kind picker seeds a table; reading back then emits delta_db_by_freq.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))                      # starts flat (constant)
    row = _active_row(p)
    assert not row.get("baseline_table")
    row["baseline_table"] = [[1.0e9, -4.0], [2.0e9, -6.0]]   # as the picker/grid would set it
    plane = p._read_form(strict=False)["chain"]["planes"]["atten_out"]
    assert plane.get("delta_db_by_freq") == [[1.0e9, -4.0], [2.0e9, -6.0]]
    assert "delta_db" not in plane


def test_active_detail_renders_with_inline_table_baseline():
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc_table(_control(), [[1.0e9, -4.0], [2.0e9, -6.0]]))
    p._select_plane("atten_out")                      # renders _detail_active + the Δ dB(f) grid/plot
    assert p._selected_plane == "atten_out"


def test_active_constant_baseline_still_supported():
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))                      # delta_db 0.0, no table
    plane = p._read_form(strict=False)["chain"]["planes"]["atten_out"]
    assert plane.get("delta_db") == 0.0 and "delta_db_by_freq" not in plane


# ── the detail editor survives a re-render (widget-lifetime regressions) ──────────────

def _flush_deletes():
    # deleteLater() posts a DeferredDelete event that a plain processEvents() may not flush;
    # force it so a widget the re-render orphaned is actually destroyed (as it is in the app).
    from PyQt6.QtCore import QCoreApplication, QEvent
    _app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _app.processEvents()


def test_active_constant_baseline_survives_a_detail_rerender():
    # Regression (crash: "set the task parameter after choosing a task"). Fetching a task's
    # params triggers a direct _render_detail() of the active editor. The constant Δ dB field
    # is a PERSISTENT model widget (row["delta"], read by _read_planes); when it was placed in
    # a bare sub-layout, _clear_layout deleteLater()'d it on the re-render, and the next form
    # read raised "wrapped C/C++ object ... deleted" inside a Qt slot — which aborts PyQt6.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control()))                       # constant baseline (delta_db 0.0)
    p._select_plane("atten_out")                       # renders _detail_active, places row["delta"]
    p._handle_taskparams("atten.py", {"params": []})   # a fetched task's params → re-renders detail
    _flush_deletes()                                   # actually destroy any orphaned widget
    plane = p._read_form(strict=False)["chain"]["planes"]["atten_out"]
    assert plane.get("delta_db") == 0.0                # row["delta"] still alive & readable


def test_active_inline_table_edit_updates_the_row():
    # Regression (crash: "add values to the frequency-gain-delta table"). Editing a cell of the
    # Δ dB(f) baseline grid fires its on-change callback; it must update the row's table, not
    # raise — the callback must bind the grid, not a later local of the same name.
    from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc_table(_control(), [[1.0e9, -4.0], [2.0e9, -6.0]]))
    p._select_plane("atten_out")                       # renders the Δ dB(f) grid
    tbl = next(t for t in p.findChildren(QTableWidget) if t.columnCount() == 2)
    tbl.setItem(0, 1, QTableWidgetItem("-5.0"))         # live edit → cellChanged → on-change
    _flush_deletes()
    row = _active_row(p)
    assert any(abs(v[1] + 5.0) < 1e-9 for v in row["baseline_table"]), row["baseline_table"]


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


# ── constant params (e.g. a serial port passed on every set) ─────────────────────────

def _seed_atten_params(p):
    p._tasks_yaml = ("tasks:\n  - name: atten_set\n"
                     "    command: [python3, atten.py, --attenuation, \"0\"]\n")
    p._task_params["atten.py"] = [
        {"dest": "attenuation", "type": "float", "flags": ["--attenuation"]},
        {"dest": "port", "type": "str", "flags": ["--port"]}]


def test_control_consts_round_trip():
    # A control block with constant params (port) loads and reads back with them intact.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control(consts={"port": "/dev/ttyACM0"})))
    row = _active_row(p)
    assert row["control"]["consts"] == {"port": "/dev/ttyACM0"}
    ctrl = p._read_form(strict=False)["chain"]["planes"]["atten_out"]["control"]
    assert ctrl["consts"] == {"port": "/dev/ttyACM0"} and ctrl["param"] == "attenuation"


def test_active_param_form_sets_a_constant():
    # With the task's params fetched, the form lists port; giving it a value writes consts.
    p = CalibrationPanel("u", FakeHub())
    p._set_doc(_doc(_control(task="atten_set", param="attenuation")))
    _seed_atten_params(p)
    p._select_plane("atten_out")                       # renders the per-parameter form
    from PyQt6.QtWidgets import QLineEdit, QRadioButton
    # attenuation (numeric) → an enabled driver radio; port (str) → a disabled radio.
    radios = [r for r in p.findChildren(QRadioButton)]
    assert any(r.isEnabled() and r.isChecked() for r in radios)     # a driver is selected
    assert any(not r.isEnabled() for r in radios)                   # the str param can't drive
    port_field = next(le for le in p.findChildren(QLineEdit)
                      if le.isEnabled() and le.placeholderText() == "constant value")
    port_field.setText("/dev/ttyACM0")
    _app.processEvents()
    ctrl = p._read_form(strict=False)["chain"]["planes"]["atten_out"]["control"]
    assert ctrl["consts"] == {"port": "/dev/ttyACM0"}


def test_control_issues_flags_the_driver_as_a_constant():
    from ui.calibration_panel import _control_issues
    bad = _control(consts={"attenuation": "10"})       # driver duplicated as a constant
    assert any("constant" in i for i in _control_issues("atten_out", bad))
    ok = _control(consts={"port": "/dev/ttyACM0"})
    assert _control_issues("atten_out", ok) == []
