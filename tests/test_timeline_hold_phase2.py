"""Phase 2 of the Hold step (docs/sequence-hold-step.md §6): the timeline CANVAS renders
the Hold as a third-anchor divider and the operator can author one, and the step editor
offers "Hold" as a window-B anchor once a Hold exists.

Covers:
  - geometry: compute_anchors places a window-B (anchor="hold") item to the right of the
    Hold divider; hold_offset / has_hold / effective_anchor_offset.
  - canvas: a Hold marker paints, is hit-testable as its own divider (winning over a bar
    body sharing its x), '+ Hold' disables once one exists, one-Hold-per-sequence.
  - step editor: the "Hold" anchor option appears only when a Hold exists (or when editing
    a hold-anchored step) and a hold-anchored tune round-trips through the dialog.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.timeline_editor import HoldEditorDialog, StepEditorDialog, TimelineEditor
from tests.test_step_editor_power_units import FakeHub

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _hold(offset=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=offset)


# ── Pure geometry helpers ────────────────────────────────────────────────────

def test_hold_offset_and_has_hold():
    items = [tlm.BarItem(task_name="tx", start_offset=0.0, stop_offset=0.0), _hold(180.0)]
    assert tlm.hold_offset(items) == 180.0
    assert tlm.has_hold(items) is True
    assert tlm.hold_offset([tlm.BarItem(task_name="tx")]) is None
    assert tlm.has_hold([tlm.BarItem(task_name="tx")]) is False


def test_effective_anchor_offset_maps_window_b_to_the_hold_side():
    wb = tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=20.0)
    # A window-B item is PLACED as start-anchored at hold_offset + its own offset…
    assert tlm.effective_anchor_offset(wb, 300.0) == ("start", 320.0)
    # …but only when a hold exists (else it keeps its own anchor).
    assert tlm.effective_anchor_offset(wb, None) == ("hold", 20.0)
    # A normal start item is unaffected.
    run = tlm.RunItem(task_name="tx", anchor="start", offset=5.0)
    assert tlm.effective_anchor_offset(run, 300.0) == ("start", 5.0)


def test_compute_anchors_places_window_b_right_of_the_hold():
    on0, off0, _ = tlm.compute_anchors([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    items = [
        tlm.BarItem(task_name="tx", start_offset=0.0, stop_offset=0.0),
        _hold(120.0),
        tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=30.0, params={"p": 1}),
    ]
    on, off, _ = tlm.compute_anchors(items)
    # window B fires at 120+30 = 150 on the on-air side, so the band widens well past the
    # bare on-air gap — the off-air anchor moves right to keep window B before it.
    hold_x = tlm.offset_to_x("start", 120.0, on, off)
    wb_x = tlm.offset_to_x(*tlm.effective_anchor_offset(items[2], 120.0), on, off)
    assert on < hold_x < wb_x < off
    assert (off - on) > (off0 - on0)          # the band grew to hold window B


# ── The editor canvas: paint, hit, add ───────────────────────────────────────

def _editor(items):
    ed = TimelineEditor()
    ed.set_context(FakeHub(), "unit")
    ed.set_tasks(["tx"])
    ed.set_task_commands({"tx": ["python3", "tx.py"]})
    ed._param_specs = {"tx.py": []}
    ed._canvas.set_items(items)
    ed.resize(1400, 380)
    ed._canvas.resize(1400, 380)
    ed._canvas._place()
    return ed


def test_hold_marker_paints_and_is_its_own_divider():
    items = [tlm.BarItem(task_name="tx", args=["--p", "1"], start_offset=-10, stop_offset=5),
             _hold(300.0),
             tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=5, params={"p": 1})]
    ed = _editor(items)
    # Paints without error (the divider + tab + chip).
    ed._canvas.render(QPixmap(1400, 380))
    hold = items[1]
    g = ed._canvas._geom[hold.uid]
    assert g["kind"] == "hold"
    # A click on the divider grabs the Hold, not the wide bar body sharing that x.
    hit = ed._canvas._hit(g["cx"], 60)
    assert hit is not None and hit[0] is hold and hit[1] == "hold_body"


def test_hold_marker_owns_no_lane():
    items = [tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0), _hold(120.0)]
    ed = _editor(items)
    # The hold is excluded from lane packing (it's a full-height divider).
    assert items[1].uid not in ed._canvas._lane_of
    assert items[0].uid in ed._canvas._lane_of


def test_add_hold_button_disables_once_a_hold_exists():
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    assert ed.has_hold() is False
    assert ed._add_hold.isEnabled() is True
    ed._canvas.set_items([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0), _hold(60)])
    assert ed.has_hold() is True
    assert ed._add_hold.isEnabled() is False


def test_plan_editor_hides_the_hold_button():
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    ed.set_hold_authoring(False)
    assert ed._add_hold.isVisible() is False
    ed.set_hold_authoring(True)
    assert ed._add_hold.isVisibleTo(ed) is True


def test_default_hold_offset_sits_past_the_furthest_window_a_point():
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0),
                  tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=45.0)])
    assert ed._canvas._default_hold_offset() == 45.0
    ed2 = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    assert ed2._canvas._default_hold_offset() == 60.0     # nothing precedes it → a default


def test_hold_editor_dialog_builds_a_hold_item():
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    dlg = HoldEditorDialog(_hold(0.0), ed, new=True)
    dlg._off.setValue(150.0)
    dlg._accept()
    it = dlg.result_item
    assert it.action == "hold" and it.anchor == "start" and it.offset == 150.0
    assert it.task_name == ""


# ── validate() tolerates the taskless Hold marker ────────────────────────────

def test_validate_accepts_a_hold_bearing_timeline():
    items = [tlm.BarItem(task_name="tx", start_offset=-10, stop_offset=5),
             _hold(120.0),
             tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=30, params={"g": 1})]
    assert tlm.validate(items, known_tasks=["tx"]) is None
    # a Hold alone isn't a real on-air step
    assert tlm.validate([_hold(10.0)]) == "needs at least one on-air step"


# ── The step editor's "Hold" anchor option ───────────────────────────────────

def test_hold_anchor_option_appears_only_with_a_hold():
    # No hold on the timeline → only on-air / off-air.
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    d0 = StepEditorDialog(tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=0.0),
                          ed, new=True)
    assert [d0._anchor.itemData(i) for i in range(d0._anchor.count())] == ["start", "stop"]

    # A hold present → the "hold" anchor is offered.
    ed2 = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0), _hold(100.0)])
    d1 = StepEditorDialog(tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=0.0),
                          ed2, new=True)
    assert "hold" in [d1._anchor.itemData(i) for i in range(d1._anchor.count())]


def test_editing_a_hold_anchored_step_keeps_the_option_and_round_trips():
    # Even with no Hold currently on THIS editor, editing a hold-anchored step keeps the
    # option (so its anchor survives an edit) — and _accept round-trips anchor + offset.
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0)])
    ed._param_specs = {"tx.py": [{"dest": "p", "flags": ["--p"], "type": "float",
                                  "default": 0.0, "live": True}]}
    wb = tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=12.0, params={"p": 1})
    dlg = StepEditorDialog(wb, ed, new=False)
    assert dlg._anchor.currentData() == "hold"
    dlg._accept()
    assert dlg.result_item is not None
    assert dlg.result_item.anchor == "hold" and dlg.result_item.offset == 12.0
    assert dlg.result_item.params == {"p": 1.0}
