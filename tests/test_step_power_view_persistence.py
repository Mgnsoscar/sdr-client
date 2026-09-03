"""The per-step --power CONTROL QUANTITY (power_view) is persisted and round-trips.

A sequence step now records which calibrated --power view the operator authored it in (a
CAL_POWER_LAWS id such as "psd_live" or "fbw_power", or None for the signal default). This is the
foundation for HOLDING the latest-set control quantity across a sequence (docs §10): it must
round-trip through the item⇄step conversion AND through the SequenceStep model, and the step editor
must save the currently-controlled view and restore it on reopen — so the operator's choice sticks
and the walk/hold know which quantity is held.

Client-only authoring metadata; --power is still sent in the base quantity, and the agent never
reads power_view. Real chirp fixtures are reused from tests/test_step_editor_carried_bw.py.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.timeline_editor import StepEditorDialog, TimelineEditor
from tests.test_step_editor_carried_bw import (_editor, _bar, _power_step, PSD, FBW,  # noqa: F401
                                               _CMD, _SPECS, _SIGNAL, _ART)
import api.models as m

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# ── pure model round-trip (no Qt widgets) ───────────────────────────────────────────────────────

def test_power_view_round_trips_through_item_and_step_conversion():
    run = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                      params={"power": -7.38}, power_view="fbw_power")
    steps = tlm.items_to_steps([run])
    assert steps[0]["power_view"] == "fbw_power"
    back = tlm.steps_to_items(steps)
    assert back[0].power_view == "fbw_power"


def test_power_view_round_trips_on_a_bar():
    bar = tlm.BarItem(task_name="chirp", args=["--power", "-7.38"], start_offset=0.0,
                      stop_offset=600.0, power_view="psd_live")
    steps = tlm.items_to_steps([bar])
    # the START step carries it (the bar's authoring), the STOP step doesn't set power
    start = next(s for s in steps if s["action"] == "start")
    assert start["power_view"] == "psd_live"
    back = tlm.steps_to_items(steps)
    assert next(it for it in back if it.kind == "bar").power_view == "psd_live"


def test_power_view_round_trips_through_the_sequencestep_model():
    ed = _editor([tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=5.0,
                              params={"power": -7.38}, power_view="fbw_power")])
    seq_steps = ed.steps()
    assert isinstance(seq_steps[0], m.SequenceStep)
    assert seq_steps[0].power_view == "fbw_power"
    # And back onto the canvas.
    ed.set_steps(seq_steps)
    assert ed.items()[0].power_view == "fbw_power"


def test_missing_power_view_is_none_not_an_error():
    run = tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=5.0,
                      args=["--power", "-7.38"])
    assert tlm.items_to_steps([run])[0]["power_view"] is None
    assert tlm.steps_to_items(tlm.items_to_steps([run]))[0].power_view is None


# ── the step editor saves the controlled view and restores it on reopen ─────────────────────────

def test_step_editor_saves_the_controlled_view():
    pstep = _power_step()
    dlg = StepEditorDialog(pstep, _editor([_bar(10), pstep]), new=False)
    _app.processEvents()
    # The chirp drops the base density; psd_live leads (default control quantity).
    assert dlg._form.power_view() == "psd_live"
    # Promote total power and save — the step must remember it.
    dlg._form._set_power_view("fbw_power")
    _app.processEvents()
    dlg._accept()
    assert dlg.result_item is not None
    assert dlg.result_item.power_view == "fbw_power"


def test_step_editor_restores_the_controlled_view_on_reopen():
    # A step authored controlling in total power reopens showing total power, not the default.
    pstep = _power_step()
    pstep.power_view = "fbw_power"
    dlg = StepEditorDialog(pstep, _editor([_bar(10), pstep]), new=False)
    _app.processEvents()
    assert dlg._form.power_view() == "fbw_power"
    assert dlg._form._selected_view()["id"] == "fbw_power"


def test_step_editor_default_view_persists_as_the_leading_quantity():
    # Authoring in the default (untouched) still records the concrete leading quantity, so the held
    # quantity is unambiguous downstream (not a bare None that the walk would have to re-derive).
    pstep = _power_step()
    dlg = StepEditorDialog(pstep, _editor([_bar(10), pstep]), new=False)
    _app.processEvents()
    dlg._accept()
    assert dlg.result_item.power_view == "psd_live"


def test_step_editor_tolerates_a_legacy_or_mismatched_view():
    # A recorded view that isn't among this signal's laws falls back to the default (no crash).
    pstep = _power_step()
    pstep.power_view = "not_a_real_law"
    dlg = StepEditorDialog(pstep, _editor([_bar(10), pstep]), new=False)
    _app.processEvents()
    assert dlg._form._selected_view()["id"] == "psd_live"     # default, not the bogus id
