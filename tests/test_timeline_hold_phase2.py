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
    # …and an ORPHANED hold anchor (no Hold on the timeline — an invalid state validate()
    # rejects) is placed start-side at its own offset, so it draws on-air (not off-air).
    assert tlm.effective_anchor_offset(wb, None) == ("start", 20.0)
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


# ── Carried-state fold across the hold (the down-ramp calibration case) ──────

def test_carry_order_key_orders_window_b_after_window_a():
    # window A (start) < window B hold < window B stop — so state is replayed in fire order.
    ka = tlm.carry_order_key("start", 50.0, 300.0)
    kb = tlm.carry_order_key("hold", 10.0, 300.0)    # -> (1, 310)
    ks = tlm.carry_order_key("stop", -5.0, 300.0)    # -> (2, -5)
    assert ka < kb < ks
    # A hold step orders by hold_offset + its offset, so it lands after every window-A start step.
    assert tlm.carry_order_key("hold", 0.0, 300.0) > tlm.carry_order_key("start", 250.0, 300.0)
    # No Hold present → a stray hold anchor falls back to the legacy start-phase ordering.
    assert tlm.carry_order_key("hold", 10.0, None) == tlm.carry_order_key("start", 10.0, None)


def test_sequence_effective_values_carries_window_a_across_the_hold():
    # A window-A tune (--bw 5 at on-air +50) must be carried into a window-B step at hold+10,
    # even though 50 > 10 — the down-ramp folds --power at the bandwidth HELD across the hold,
    # not the bar baseline. (Before the fix the window-A tune was skipped as "later".)
    specs = [{"dest": "bw", "flags": ["--bw"], "name": "bw"},
             {"dest": "power", "flags": ["--power"], "name": "power"}]
    bar = tlm.BarItem(task_name="tx", args=["--bw", "20", "--power", "-30"],
                      start_offset=0.0, stop_offset=600.0)
    wa = tlm.RunItem(task_name="tx", action="tune", anchor="start", offset=50.0, params={"bw": 5})
    hold = _hold(300.0)
    wb = tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=10.0,
                     params={"power": -25}, uid=999)
    items = [bar, wa, hold, wb]
    state = tlm.sequence_effective_values(
        items, "tx", ["--bw", "20", "--power", "-30"], specs, 999,
        target_key=tlm._carry_order_key(wb, tlm.hold_offset(items)))
    assert state["bw"] == 5.0        # carried across the hold — NOT the bar's 20


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


def test_dragging_a_window_b_pill_measures_offset_from_the_hold():
    # A window-B (anchor="hold") one-shot is placed from the Hold divider, so a drag must measure
    # its new offset from the hold — not from off-air (which produced a corrupted negative offset).
    items = [tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0),
             _hold(100.0),
             tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=5, params={"g": 1})]
    ed = _editor(items)
    c = ed._canvas
    wb = items[2]
    # The drag base for a window-B pill is the Hold's x, and a hold-anchored tune isn't span-clamped.
    hold_x = c._on + c._hold_off * c._eff()
    assert abs(c._anchor_base_x(wb) - hold_x) < 1e-6
    assert c._clamp_tune_offset(wb, 40.0) == 40.0            # window B not clamped to the on-air span
    # A drag to 40 s past the hold yields offset +40 (positive), and _live_relayout round-trips it.
    target_x = hold_x + 40 * c._eff()
    wb.offset = tlm._snap((target_x - c._anchor_base_x(wb)) / c._eff())
    assert wb.offset == 40.0
    c._live_relayout(wb)
    assert abs(c._geom[wb.uid]["cx"] - target_x) < 1.0


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


def test_default_hold_offset_lands_after_an_up_ramp_not_mid_ramp():
    # A start-anchored up-ramp starting at +30 s running 60 s ends at +90 s; the default Hold must
    # land at its END (90), not its start (30) — so it sits at the top of the run-up.
    ramp = {"param": "power", "flag": "--power", "start": -40, "stop": -20, "steps": 5, "hold_s": 15}
    ed = _editor([tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0),
                  tlm.RunItem(task_name="tx", action="ramp", anchor="start", offset=30.0, ramp=ramp)])
    assert ed._canvas._default_hold_offset() == 30.0 + tlm._ramp_duration(ramp)


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


def test_validate_rejects_an_orphaned_hold_anchored_step():
    # Removing the Hold but leaving its post-hold steps → an anchor="hold" step with no Hold to
    # anchor to. That mis-places on the canvas/walk, so validate() must reject it (agent parity).
    orphan = [tlm.BarItem(task_name="tx", start_offset=0, stop_offset=0),
              tlm.RunItem(task_name="tx", action="tune", anchor="hold", offset=5, params={"g": 1})]
    err = tlm.validate(orphan, known_tasks=["tx"])
    assert err is not None and "Hold" in err
    # Adding the Hold back makes it valid again.
    assert tlm.validate(orphan + [_hold(50.0)], known_tasks=["tx"]) is None


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
