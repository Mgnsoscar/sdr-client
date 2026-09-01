"""The ramp editor's --power range folds at the frequency the ramped task is running
at — the same fold the step editor applies — so a frequency-dependent calibration
bounds the sweep to what the unit can deliver where the task is tuned, not only at the
calibration's representative frequency.

Uses the REAL TimelineEditor (not a stub) so items() and the script→freq-param wiring
that the fold relies on are exercised end to end.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.ramp_editor import RampEditorDialog
from ui.timeline_editor import TimelineEditor, task_signals_from_yaml

_app = QApplication.instance() or QApplication([])

# A frequency-dependent chain: at the 1.5 GHz rep the range is −30…4 dBm; a passive hop
# (e.g. a cable/antenna whose loss varies with frequency) adds +6 dB at 1.0 GHz and −6 dB
# at 2.0 GHz, so the whole range shifts with the transmit frequency.
_ART = {
    "anchor_curve": [[40.0, -30.0], [74.0, 4.0]],
    "min_gain_db": 40.0, "gain_ceiling_db": 74.0, "center_freq_hz": 1.5e9,
    "passive_hops": [{"plane": "antenna",
                      "delta_db_by_freq": [[1.0e9, 6.0], [1.5e9, 0.0], [2.0e9, -6.0]]}],
}
_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -30.0, "max_power_dbm": 4.0, "artifact": _ART}}}
_YAML = ("tasks:\n  - name: mocktask\n    command: [python3, mock_tx.py]\n"
         "    env: { SDR_CAL_SIGNAL_ID: mock }\n")
_FREQ = {"dest": "freq", "name": "freq", "flags": ["-Center", "--freq"], "type": "float",
         "unit": "MHz", "min": 70.0, "max": 6000.0, "default": 1500.0, "live": True,
         "is_freq": True}
_POWER = {"dest": "power", "name": "power", "flags": ["-Power", "--power"], "type": "float",
          "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "live": True}


class _Hub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        client = type("C", (), {
            "get_calibration": lambda s_: _CAL,
            "get_script_params": lambda s_, n: {
                "params": [_FREQ, _POWER], "calibration_signal": "mock",
                "calibration_freq_param": "freq"},
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
    t.set_tasks(["mocktask"])
    t.set_task_commands({"mocktask": ["python3", "mock_tx.py"]})
    t.set_task_signals(task_signals_from_yaml(_YAML))
    t.set_calibration(hub, "unit-1")            # synchronous fetch here
    t.param_cache()["mock_tx.py"] = [_FREQ, _POWER]
    t._script_cal_freq_params["mock_tx.py"] = "freq"
    return t


def _ramp_range(bar_freq_mhz, *, offset=0.0):
    t = _editor()
    bar = tlm.BarItem(task_name="mocktask", args=["--freq", str(bar_freq_mhz)],
                      start_offset=0.0, stop_offset=60.0)
    t._canvas.set_items([bar])   # place the duration bar
    src = tlm.RunItem(task_name="mocktask", action="ramp", anchor="start",
                      offset=offset, ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    spec = dlg._ramped_spec()
    return (spec.get("min"), spec.get("max")) if spec else None


def test_range_folds_at_bar_frequency():
    assert _ramp_range(1500.0) == (-30.0, 4.0)      # rep frequency
    assert _ramp_range(2000.0) == (-36.0, -2.0)     # cold: whole range 6 dB lower
    assert _ramp_range(1000.0) == (-24.0, 10.0)     # hot: whole range 6 dB higher


def test_from_to_widget_bounds_fold_at_frequency():
    # The user-visible From/To fields (spinbox + rail) carry the folded range, and the rail
    # notes the frequency it was folded at.
    from ui.param_form import BoundedNumberField
    t = _editor()
    bar = tlm.BarItem(task_name="mocktask", args=["--freq", "2000"],
                      start_offset=0.0, stop_offset=60.0)
    t._canvas.set_items([bar])
    src = tlm.RunItem(task_name="mocktask", action="ramp", anchor="start", offset=0.0, ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    f = dlg._start_field
    assert isinstance(f, BoundedNumberField)
    assert (f._spin.minimum(), f._spin.maximum()) == (-36.0, -2.0)
    assert f._rail._note is not None and "2000.00 MHz" in f._rail._note.text()


def test_fold_survives_when_ramp_offset_coincides_with_bar_start():
    # A ramp at on-air offset 0 shares the bar's order key; the fold must still see the
    # bar's frequency (seeded from the bar's args) rather than dropping it.
    assert _ramp_range(2000.0, offset=0.0) == (-36.0, -2.0)


def test_earlier_tune_moves_the_fold_frequency():
    t = _editor()
    bar = tlm.BarItem(task_name="mocktask", args=["--freq", "1500"],
                      start_offset=0.0, stop_offset=60.0)
    tune = tlm.RunItem(task_name="mocktask", action="tune", anchor="start", offset=10.0,
                       params={"freq": 2000.0})
    t._canvas.set_items([bar, tune])
    src = tlm.RunItem(task_name="mocktask", action="ramp", anchor="start", offset=30.0,
                      ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    spec = dlg._ramped_spec()
    # by the ramp (offset 30) the earlier tune (offset 10) has moved freq to 2.0 GHz
    assert (spec.get("min"), spec.get("max")) == (-36.0, -2.0)


def test_constant_chain_keeps_the_flat_range():
    # No artifact fold data → refold is a no-op; the resolved flat range still applies.
    flat = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
        "operating_plane": "antenna_eirp", "quantity": "EIRP",
        "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}

    class _FlatHub(_Hub):
        def __init__(self):
            super().__init__()
            client = type("C", (), {
                "get_calibration": lambda s_: flat,
                "get_script_params": lambda s_, n: {
                    "params": [_FREQ, _POWER], "calibration_signal": "mock",
                    "calibration_freq_param": "freq"},
            })()
            self.fleet = type("F", (), {"get": lambda s_, h: client})()

    t = TimelineEditor()
    hub = _FlatHub()
    t.set_context(hub, "unit-1")
    t.set_tasks(["mocktask"])
    t.set_task_commands({"mocktask": ["python3", "mock_tx.py"]})
    t.set_task_signals(task_signals_from_yaml(_YAML))
    t.set_calibration(hub, "unit-1")
    t.param_cache()["mock_tx.py"] = [_FREQ, _POWER]
    t._script_cal_freq_params["mock_tx.py"] = "freq"
    bar = tlm.BarItem(task_name="mocktask", args=["--freq", "2000"],
                      start_offset=0.0, stop_offset=60.0)
    t._canvas.set_items([bar])
    src = tlm.RunItem(task_name="mocktask", action="ramp", anchor="start", offset=0.0, ramp={})
    dlg = RampEditorDialog(src, t, new=True)
    dlg._param.setCurrentText("power")
    spec = dlg._ramped_spec()
    assert (spec.get("min"), spec.get("max")) == (-1.8, 28.2)
