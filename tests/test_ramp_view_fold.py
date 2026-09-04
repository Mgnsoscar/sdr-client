"""Stage 3 — the ramp editor authors --power in the CONTROLLED view (a chirp's live spectral
density), like the Run/Tune power card (restates_measurement replaces the measured quantity).

The ramp's From/To range is the LIVE density at the carried sweep bandwidth (not the bw-frozen
measured base): the base range is shifted into the view by view_delta(carried_bw) and relabelled,
the bounded field snaps in base but displays the view (BoundedNumberField.view_offset), the stored
ramp start/stop are BASE (offset removed on save, added back on load), and the ramp records its
control view so the achievability walk / hold treat it as that quantity.

Fixtures: the REAL chirp structure + editor from tests/test_step_editor_carried_bw.py.
See docs/sequence-power-achievability.md §10 Stage 3.
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.param_form import BoundedNumberField
from ui.ramp_editor import RampEditorDialog
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar, _set_bw

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _psd_max(bw):
    return -7.38 - 10 * math.log10(bw / 10.0)             # base_max + view_delta(bw)


def _ramp_dlg(items, offset=10.0):
    ed = _chirp_editor(items)
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=offset, ramp={})
    dlg = RampEditorDialog(src, ed, new=True)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    return dlg


def test_ramp_range_is_the_live_density_at_the_carried_bandwidth():
    # bar bw 10, a tune widens to 20 before the ramp → the ramp authors density at bw 20.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    spec = dlg._ramped_spec()
    assert spec.get("unit") == "dBm/MHz"
    assert spec.get("max") == pytest.approx(_psd_max(20), abs=0.06)     # ≈ −10.39, not −7.38


def test_ramp_range_tracks_the_carried_bandwidth():
    # bw 10 vs bw 20 → the density ceiling drops ≈ 3 dB (the same fold the step editor applies).
    d10 = _ramp_dlg([_bar(10)])
    d20 = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert d10._ramped_spec().get("max") == pytest.approx(_psd_max(10), abs=0.06)   # ≈ −7.38
    assert d20._ramped_spec().get("max") == pytest.approx(_psd_max(20), abs=0.06)   # ≈ −10.39
    assert d10._ramped_spec().get("max") > d20._ramped_spec().get("max") + 2.5


def test_from_field_displays_the_view_but_snaps_in_base():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    f = dlg._start_field
    assert isinstance(f, BoundedNumberField)
    assert f._view_off == pytest.approx(-10 * math.log10(2), abs=0.02)   # view_delta(20) ≈ −3.01
    assert f._spin.maximum() == pytest.approx(_psd_max(20), abs=0.06)    # view range on the spinbox


def test_ramp_seed_shows_the_saved_base_in_the_view_on_load():
    # A saved ramp stores BASE start/stop; reopened at carried bw 20 the fields show the live density
    # (base + view_delta(20)). base stop −11.99 → density −15.
    ed = _chirp_editor([_bar(10), _set_bw(20, 5.0)])
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=10.0,
                      ramp={"start": -30.0, "stop": -11.99, "step": 1.0, "hold_s": 5.0},
                      power_view="psd_live")
    ed._canvas.set_items([_bar(10), _set_bw(20, 5.0), src])
    dlg = RampEditorDialog(src, ed, new=False)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    assert dlg._val(dlg._stop_field) == pytest.approx(-11.99 + (-10 * math.log10(2)), abs=0.06)  # ≈ −15


def test_ramp_save_stores_base_and_records_the_view():
    # Author density From −25 → −18 at carried bw 20; the STORED ramp is base (density − view_delta),
    # and power_view records the controlled view so the walk/hold treat it as live density.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._accept()
    assert dlg.result_item is not None
    assert dlg.result_item.power_view == "psd_live"
    off = -10 * math.log10(2)                              # view_delta(20) ≈ −3.01
    assert dlg.result_item.ramp["start"] == pytest.approx(-25.0 - off, abs=0.06)   # base ≈ −21.99
    assert dlg.result_item.ramp["stop"] == pytest.approx(-18.0 - off, abs=0.06)    # base ≈ −14.99


def test_save_then_load_round_trips_the_density():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._accept()
    saved = dlg.result_item

    ed = _chirp_editor([_bar(10), _set_bw(20, 5.0), saved])
    dlg2 = RampEditorDialog(saved, ed, new=False)
    dlg2._param.setCurrentText("power")
    _app.processEvents()
    # the reopened From/To show the SAME densities the operator authored
    assert dlg2._val(dlg2._start_field) == pytest.approx(-25.0, abs=0.06)
    assert dlg2._val(dlg2._stop_field) == pytest.approx(-18.0, abs=0.06)
