"""The ramp editor's --power range folds through the BRIDGE PARAMS in effect when the ramp
fires — not just the frequency. A parameter-dependent calibration (GPS C/A: the dBm ceiling is
gauged through a limiting law keyed on enbw, a hidden table lookup on --sidelobes) therefore
bounds the sweep to what the unit can deliver at the carried --sidelobes, so every level From..To
is checked against the real operating point. Regression: the fold passed only the frequency, so
the range stuck at the law's representative value and never tracked --sidelobes.

Uses the REAL TimelineEditor + RampEditorDialog end to end (like test_ramp_cal_freq_fold.py).
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import TimelineEditor
from tests.test_live_tune_power import _ENBW, _GPS_ART, _GPS_CAL, _GPS_PARAMS

_app = QApplication.instance() or QApplication([])

_SCRIPT = "gps_ca_code_10.23Mcps.py"


class _Hub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        client = type("C", (), {
            "get_calibration": lambda s_: _GPS_CAL,
            "get_script_params": lambda s_, n: _GPS_PARAMS,
        })()
        self.fleet = type("F", (), {"get": lambda s_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _editor():
    t = TimelineEditor()
    hub = _Hub()
    t.set_context(hub, "unit-1")
    t.set_tasks(["gps"])
    t.set_task_commands({"gps": ["python3", _SCRIPT]})
    t.set_task_signals({"gps": "gpsca"})
    t.set_calibration(hub, "unit-1")                       # synchronous fetch here
    t.param_cache()[_SCRIPT] = _GPS_PARAMS["params"]
    t._script_cal_freq_params[_SCRIPT] = "freq"
    return t


def _ramp_power_max(sidelobes: int):
    """The folded --power (density) ceiling the ramp offers when the running task carries
    ``--sidelobes = sidelobes``."""
    t = _editor()
    bar = tlm.BarItem(task_name="gps",
                      args=["--freq", "1227.6", "--power", "-40", "--sidelobes", str(sidelobes)],
                      start_offset=0.0, stop_offset=60.0)
    t._canvas.set_items([bar])
    src = tlm.RunItem(task_name="gps", action="ramp", anchor="start", offset=10.0, ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    spec = dlg._ramped_spec()
    return spec.get("max")


def test_power_range_folds_through_carried_sidelobes():
    # More sidelobes pass more of the signal → more in-band power for a given density → a LOWER
    # density ceiling. The ramp's max must MOVE with the carried --sidelobes (the whole point).
    m0, m2 = _ramp_power_max(0), _ramp_power_max(2)
    assert m0 == pytest.approx(-18.0, abs=0.02)
    assert m2 == pytest.approx(-18.0 - 10 * math.log10(_ENBW[2] / _ENBW[0]), abs=0.02)   # ≈ −18.30
    assert m0 > m2                                          # the ceiling tightened with sidelobes


def test_from_field_snaps_through_bridge_params():
    # The user-visible From field folds through the bridge params too: fold_params reaches its
    # achievable-level snappers, so a value above the (sidelobes-2) ceiling snaps down to it.
    from ui.param_form import BoundedNumberField
    t = _editor()
    bar = tlm.BarItem(task_name="gps",
                      args=["--freq", "1227.6", "--power", "-40", "--sidelobes", "2"],
                      start_offset=0.0, stop_offset=60.0)
    t._canvas.set_items([bar])
    src = tlm.RunItem(task_name="gps", action="ramp", anchor="start", offset=10.0, ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    f = dlg._start_field
    assert isinstance(f, BoundedNumberField)
    ceiling = -18.0 - 10 * math.log10(_ENBW[2] / _ENBW[0])
    assert f._spin.maximum() == pytest.approx(ceiling, abs=0.02)     # bound folded at sidelobes 2
    # the achievable-level snapper folds at the same bridge params (never above the ceiling).
    assert f._psnap is not None
    assert f._psnap(0.0) <= ceiling + 0.05
