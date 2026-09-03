"""TimelineEditor calibration context: task→signal parsing, per-task resolved
bounds from the target unit's /calibration, and absolute_allowed gating. This is
what lets a plan's / sequence's tune steps offer absolute (calibrated) power."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from api.client import AgentHTTPError
from ui.timeline_editor import TimelineEditor, task_signals_from_yaml

_app = QApplication.instance() or QApplication([])

YAML = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
    "  - name: plain\n"
    "    command: [python3, other.py]\n"
)
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, cal=CAL):
        super().__init__()
        client = type("C", (), {"get_calibration": lambda self_: (
            (_ for _ in ()).throw(cal) if isinstance(cal, Exception) else cal)})()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def test_task_signals_from_yaml():
    assert task_signals_from_yaml(YAML) == {"mocktask": "mock"}
    assert task_signals_from_yaml("") == {}


def test_calibrated_bounds_for_task():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(), "unit-1")      # fetch resolves synchronously here
    assert t.absolute_allowed() is True
    assert t.cal_bounds_for_task("mocktask") == CAL["signals"]["mock"]
    assert t.cal_bounds_for_task("plain") is None      # no signal → no bounds


def test_uncalibrated_unit_no_bounds():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(cal=AgentHTTPError("u", 404, "none")), "unit-1")
    assert t.absolute_allowed() is True                # a unit is targeted…
    assert t.cal_bounds_for_task("mocktask") is None   # …but it isn't calibrated


def test_no_unit_means_free_form_absolute():
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(), "")                    # library: no target unit
    assert t.absolute_allowed() is False               # → absolute is offered free-form
    assert t.cal_bounds_for_task("mocktask") is None


def test_library_host_is_not_a_target_unit():
    # The reserved LIBRARY_HOST is offline authoring, not a real unit — it must NOT count
    # as a targeted unit (that made absolute unavailable and mis-fired the "uncalibrated
    # unit" caution when authoring Library sequences).
    from api.fleet import LIBRARY_HOST
    t = TimelineEditor()
    t.set_task_signals(task_signals_from_yaml(YAML))
    t.set_calibration(FakeHub(), LIBRARY_HOST)
    assert t.absolute_allowed() is False               # not a target → absolute free-form
    assert t.cal_bounds_for_task("mocktask") is None
    assert t.has_cal_signal("mocktask") is True         # the task still opts into calibration
    assert t.has_cal_signal("plain") is False


def test_script_calibratable_reflects_declared_signal():
    # A task's SCRIPT declaring a calibration signal is what makes a missing task signal a
    # real gap; a script that declares none takes raw power by design (no caution).
    t = TimelineEditor()
    t.set_task_commands({"tx": ["python3", "tx.py"]})
    assert t.script_calibratable("tx") is True           # unknown (params unfetched) → assume yes
    t._script_cal_signals["tx.py"] = "mock"
    assert t.script_calibratable("tx") is True            # script declares a signal
    t._script_cal_signals["tx.py"] = None
    assert t.script_calibratable("tx") is False           # script declares none → raw by design


# ── frequency units reach the fold as Hz (not the raw field value) ──────────────────────

def test_hz_per_unit_maps_field_units_to_hz():
    from ui.param_form import hz_per_unit
    assert hz_per_unit("Hz") == 1.0
    assert hz_per_unit("kHz") == 1e3
    assert hz_per_unit("MHz") == 1e6
    assert hz_per_unit("GHz") == 1e9
    assert hz_per_unit("mhz") == 1e6                       # case-insensitive
    assert hz_per_unit(None) == 1.0 and hz_per_unit("") == 1.0 and hz_per_unit("dBm") == 1.0


def test_step_editor_clamp_warning_folds_at_hz_not_the_raw_mhz_value():
    # Regression: the sequence step editor passed the freq field's RAW value (in MHz) to
    # clamp_warning, which expects Hz — so it folded at ~0 Hz and the caption read "0.001 MHz" for
    # a 1227.6 MHz carrier. It must convert through the field's unit first (hz_per_unit), so the
    # fold — and the caption — use the real carrier.
    from PyQt6.QtWidgets import QLabel

    from ui.param_form import find_power_index                # noqa: F401 (import parity w/ editor)
    from ui.timeline_editor import StepEditorDialog

    # Frequency-dependent chain (a passive hop that varies with freq), so the fold frequency
    # actually matters; a −18.15 dBm request overshoots the ceiling at the real carrier.
    art = {"anchor_curve": [[40, -60.0], [80, -30.0]], "min_gain_db": 40.0, "max_gain_db": 80.0,
           "gain_ceiling_db": 80.0, "gain_step_db": 1.0,
           "passive_hops": [{"delta_db_by_freq": [[1.0e9, 0.0], [1.3e9, -5.0]]}],
           "readings": {"limiting": {"kind": "same"}}, "center_freq_hz": 1.2276e9}
    bounds = {"min_power_dbm": -180.0, "max_power_dbm": -30.0, "quantity": "EIRP",
              "operating_plane": "sdr_output", "artifact": art}
    specs = [{"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz", "is_freq": True},
             {"dest": "power", "flags": ["--power"], "type": "float", "unit": "dBm",
              "snap_role": "power"}]

    class _Editor:
        _script_cal_freq_params = {"chirp.py": "freq"}

        def cal_bounds_for_task(self, task):
            return bounds

        def script_for_task(self, task):
            return ("chirp.py", [])

        def param_cache(self):
            return {"chirp.py": specs}

    dlg = StepEditorDialog.__new__(StepEditorDialog)         # bypass the heavy dialog build
    dlg._editor = _Editor()
    dlg._clamp_warn = QLabel()
    dlg._task = type("T", (), {"currentText": lambda self_: "mocktask"})()
    dlg._form = type("F", (), {"values": lambda self_: {}})()
    # the running step is at 1227.6 (its field unit, MHz) with a −18.15 dBm request
    dlg._carried_values = lambda task, script, spx: {"freq": 1227.6, "power": -18.15}

    dlg._update_clamp_warning()
    txt = dlg._clamp_warn.text()
    assert txt and "clamped down" in txt                    # the warning fires
    assert "1227.600 MHz" in txt                            # folded at the real carrier (Hz→MHz)
    assert "0.001 MHz" not in txt                           # not the raw-MHz-as-Hz mistake


# ── the extracted formula evaluator folds over any value source ─────────────────────────

def test_eval_formula_reads_from_a_dict_source():
    # eval_formula is the source-agnostic core the param form (reading widgets) and the sequence
    # step editor (reading a flat {dest: value} state) both fold through. Here we drive it from a
    # plain dict to prove the ops match the widget path and a missing source degrades to None.
    from ui.param_form import eval_formula
    from tests.test_live_tune_power import _ENBW
    get = {"sidelobes": 2, "a": 1200.0, "b": 1300.0}.get
    assert eval_formula({"table": ["sidelobes", *_ENBW]}, get) == pytest.approx(_ENBW[2])
    assert eval_formula({"center": ["a", "b"]}, get) == pytest.approx(1250.0)
    assert eval_formula({"span": ["a", "b"]}, get) == pytest.approx(100.0)
    assert eval_formula({"linear": ["sidelobes", 2.0, 1.0]}, get) == pytest.approx(5.0)
    assert eval_formula({"sum": ["a", 50.0]}, get) == pytest.approx(1250.0)
    # out-of-range table index clamps to the ends; a missing source (or empty formula) → None,
    # so a partially-typed / incomplete state stays silent rather than raising.
    assert eval_formula({"table": ["sidelobes", 5.0, 6.0]}, get) == pytest.approx(6.0)
    assert eval_formula({"span": ["a", "missing"]}, get) is None
    assert eval_formula(None, get) is None


def test_fold_params_from_values_resolves_derived_enbw():
    # A reading bridge that keys on a DERIVED, hidden quantity (GPS C/A's enbw_mhz — a table
    # lookup on --sidelobes) is not in the raw effective state, so fold_params_from_values must
    # compute it from its formula, tracking the live count — exactly what ParamForm.fold_params
    # does off its widgets, but over a flat dict.
    from ui.param_form import fold_params_from_values
    from tests.test_live_tune_power import _ENBW, _GPS_ART, _GPS_PARAMS
    specs = _GPS_PARAMS["params"]
    for n in (0, 1, 2):
        got = fold_params_from_values(_GPS_ART, specs, {"sidelobes": n, "freq": 1227.6})
        assert got["enbw_mhz"] == pytest.approx(_ENBW[n])
    # no usable artifact → None; an unresolvable key (no --sidelobes to key enbw on) → None —
    # either way the caller folds at the law's representative value instead of a wrong one.
    assert fold_params_from_values({}, specs, {"sidelobes": 0}) is None
    assert fold_params_from_values(_GPS_ART, specs, {}) is None


# ── the sequence step editor's clamp caption folds through the live BRIDGE params ─────────
# Regression: the step editor passed NO params to clamp_warning, so the caption's ceiling stuck at
# the limiting law's REPRESENTATIVE value and never tracked the live --sidelobes (or a chirp's
# --bw) — unlike the run and live-tune forms. The step editor has no single ParamForm holding the
# full effective state (a bridge-keyed source may be CARRIED from an earlier step, not in this
# step's form; derived keys like enbw aren't in the raw carried state at all), so it resolves the
# keyed params over the effective dict (carried ∪ this step's form) via fold_params_from_values.

def _gps_step_dialog(carried, form_values):
    from PyQt6.QtWidgets import QLabel
    from ui.timeline_editor import StepEditorDialog
    from tests.test_live_tune_power import _GPS_ART, _GPS_PARAMS

    specs = _GPS_PARAMS["params"]
    # GPS C/A: --power in spectral density, the dBm safety ceiling gauged through a limiting law
    # keyed on enbw (a table lookup on --sidelobes). The chain isn't frequency-dependent but IS
    # parameter-dependent, so the caption folds through --sidelobes.
    bounds = {"min_power_dbm": -120.0, "max_power_dbm": -10.0, "quantity": "spectral density",
              "operating_plane": "sdr_output", "artifact": _GPS_ART}

    class _Editor:
        _script_cal_freq_params = {"gps.py": "freq"}

        def cal_bounds_for_task(self, task):
            return bounds

        def script_for_task(self, task):
            return ("gps.py", [])

        def param_cache(self):
            return {"gps.py": specs}

    dlg = StepEditorDialog.__new__(StepEditorDialog)         # bypass the heavy dialog build
    dlg._editor = _Editor()
    dlg._clamp_warn = QLabel()
    dlg._task = type("T", (), {"currentText": lambda self_: "gps"})()
    dlg._form = type("F", (), {"values": lambda self_: dict(form_values)})()
    dlg._carried_values = lambda task, script, spx: dict(carried)
    return dlg


def _caption_for(carried, form_values):
    dlg = _gps_step_dialog(carried, form_values)
    dlg._update_clamp_warning()
    return dlg._clamp_warn.text().replace("−", "-")


def test_step_editor_clamp_warning_folds_through_live_bridge_params():
    # A −18.15 dBm request clears the 0-sidelobe ceiling (−18.0 dBm) but exceeds the tighter
    # 2-sidelobe one (≈ −18.30 dBm). So the caption is SILENT at 0 lobes and FIRES — naming the
    # folded ceiling — at 2. Without the fix (no params) the ceiling folds at the law's
    # representative value (−18.30) regardless of the count, so it would (wrongly) warn at 0 too.
    base = {"freq": 1227.6, "power": -18.15}

    # --sidelobes carried from an earlier step:
    assert _caption_for({**base, "sidelobes": 0}, {}) == ""          # under the 0-lobe ceiling
    hot = _caption_for({**base, "sidelobes": 2}, {})
    assert hot and "clamped down" in hot
    assert "1227.600 MHz" in hot                                     # folded at the carried carrier
    assert "-18.30" in hot                                           # the 2-lobe ceiling, tracked

    # --sidelobes set on THIS step's form overrides the carried count (both directions):
    assert _caption_for({**base, "sidelobes": 2}, {"sidelobes": 0}) == ""
    hot2 = _caption_for({**base, "sidelobes": 0}, {"sidelobes": 2})
    assert hot2 and "clamped down" in hot2 and "-18.30" in hot2
