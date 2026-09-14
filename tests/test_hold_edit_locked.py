"""Hold-edit: the ELAPSED window is frosted + LOCKED (docs/hold-edit-elapsed-mockup.html, option A).

While a run is HOLDING, the edit dialog shows the whole sequence, but everything at/before the Hold
already ran: it paints monochrome under a frosted wash with an ELAPSED ribbon, takes no drag /
anchor / double-click / delete, a click just says so, and the Hold itself (NOW) can't move. A task
running since before the pause keeps only its stop editable. New steps seed post-hold, and the
dialog refuses a result whose window A changed (the backstop behind the canvas gates).
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui import timeline_model as tlm
from ui.timeline_editor import ELAPSED_HUE, HOLD_BAND_PX, LANES_TOP, LANE_H
from tests.test_step_editor_carried_bw import _editor as _chirp_editor

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# ── fixtures: a LoL-test-like sequence parked at its Hold ────────────────────

def _bar(start=0.0):
    return tlm.BarItem(task_name="chirp", args=["--freq", "1575.42", "--power", "-7.38", "--bw", "10"],
                       start_offset=start, stop_offset=0.0)


def _tune(off, anchor="start", **params):
    return tlm.RunItem(task_name="chirp", action="tune", anchor=anchor, offset=off,
                       params=params or {"bw": 12})


def _ramp(off, anchor="start", start=-90.0, stop=-58.0, dur=60.0):
    return tlm.RunItem(task_name="chirp", action="ramp", anchor=anchor, offset=off,
                       ramp={"param": "power", "start": start, "stop": stop, "steps": 3,
                             "duration_s": dur})


def _hold(off=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=off)


def _scene():
    """bar · RF-on tune @0 · bw tune @40 · run-up ramp 240→300 (ends AT the pause) · Hold @300 ·
    the resumed remainder ramp (resume +15) · a window-B tune (resume +90) · an off-air tune."""
    return {
        "bar": _bar(),
        "rf": _tune(0.0, rf="on"),
        "bw": _tune(40.0, bw=12),
        "runup": _ramp(240.0),
        "hold": _hold(300.0),
        "rem": _ramp(15.0, anchor="hold", start=-58.0, stop=-50.0, dur=30.0),
        "wb": _tune(90.0, anchor="hold", bw=20),
        "off": _tune(-10.0, anchor="stop", rf="off"),
    }


def _locked(items=None):
    s = _scene()
    ed = _chirp_editor(list(s.values()) if items is None else items)
    ed.set_elapsed_locked(True)
    return ed, s


def _cy(cv, it):
    return cv._geom[it.uid]["y"] + LANE_H / 2


def _press(cv, x, y, kind=QEvent.Type.MouseButtonPress):
    ev = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    if kind == QEvent.Type.MouseButtonDblClick:
        cv.mouseDoubleClickEvent(ev)
    else:
        cv.mousePressEvent(ev)


# ── what counts as elapsed ───────────────────────────────────────────────────

def test_elapsed_kind_classifies_window_a_the_running_task_and_the_hold():
    ed, s = _locked()
    cv = ed._canvas
    assert cv.elapsed_kind(s["rf"]) == "full"          # fired at on-air
    assert cv.elapsed_kind(s["bw"]) == "full"
    assert cv.elapsed_kind(s["runup"]) == "full"       # every point at/before the pause
    assert cv.elapsed_kind(s["bar"]) == "start"        # running since before the pause
    assert cv.elapsed_kind(s["hold"]) == "hold"        # NOW
    assert cv.elapsed_kind(s["rem"]) is None           # window B
    assert cv.elapsed_kind(s["wb"]) is None
    assert cv.elapsed_kind(s["off"]) is None           # off-air (post-hold)
    # A pause-anchored (enter) step is window A too; a window-B duration task is not.
    en = tlm.RunItem(task_name="chirp", action="tune", anchor="enter", offset=-5.0, params={"bw": 9})
    wbar = tlm.BarItem(task_name="chirp", start_offset=0.0, stop_offset=0.0, start_anchor="hold")
    ed2, _ = _locked([_bar(), _hold(300.0), en, wbar])
    assert ed2._canvas.elapsed_kind(en) == "full"
    assert ed2._canvas.elapsed_kind(wbar) is None
    # Not locked → nothing is elapsed, even with a Hold.
    ed3 = _chirp_editor(list(_scene().values()))
    assert all(ed3._canvas.elapsed_kind(it) is None for it in ed3._canvas.items())


def test_a_crossing_ramp_is_not_elapsed_but_its_split_runup_is():
    # Loaded through the dialog a crossing ramp is split; a raw crossing ramp (a point still to
    # fire after the pause) is NOT locked as a whole — only a wholly-fired ramp is.
    cross = _ramp(270.0)                                 # points 270, 285, 300, 315 → fires after 300
    ed, _ = _locked([_bar(), _hold(300.0), cross])
    assert ed._canvas.elapsed_kind(cross) is None
    at_pause = _ramp(255.0)                              # points 255, 270, 285, 300 → last AT the pause
    ed2, _ = _locked([_bar(), _hold(300.0), at_pause])
    assert ed2._canvas.elapsed_kind(at_pause) == "full"


# ── hit-testing: no handles, no drag, no edit on what already happened ───────

def test_locked_items_hit_as_locked_but_the_running_tasks_stop_stays_live():
    ed, s = _locked()
    cv = ed._canvas
    g = cv._geom
    assert cv._hit(g[s["rf"].uid]["cx"], _cy(cv, s["rf"]))[1] == "locked"        # the pin dot
    rp = g[s["runup"].uid]
    assert cv._hit((rp["start_x"] + rp["stop_x"]) / 2, _cy(cv, s["runup"]))[1] == "locked"
    assert cv._hit(rp["start_x"], _cy(cv, s["runup"]))[1] == "locked"             # its edge dot
    bg = g[s["bar"].uid]
    assert cv._hit(bg["start_x"], _cy(cv, s["bar"]))[1] == "locked"               # anchor dot
    assert cv._hit(bg["start_x"] + 60, _cy(cv, s["bar"]))[1] == "locked"          # body
    hit = cv._hit(bg["stop_x"] - 2, _cy(cv, s["bar"]))
    assert hit is not None and hit[0] is s["bar"] and hit[1] == "bar_stop"       # stop grip: live
    hh = cv._hit(cv._enter_x + 5, LANES_TOP + 5)
    assert hh is not None and hh[0] is s["hold"] and hh[1] == "locked"           # the Hold band
    # Window B is the normal editor.
    wb = cv._hit(g[s["wb"].uid]["cx"], _cy(cv, s["wb"]))
    assert wb is not None and wb[0] is s["wb"] and wb[1] == "edge_start"
    # …and nothing is "locked" when the same scene isn't locked.
    ed2 = _chirp_editor(list(_scene().values()))
    cv2 = ed2._canvas
    rf2 = next(it for it in cv2.items() if getattr(it, "params", None) == {"rf": "on"})
    assert cv2._hit(cv2._geom[rf2.uid]["cx"], _cy(cv2, rf2))[1] == "edge_start"


def test_a_press_or_double_click_on_a_locked_item_only_shows_the_notice(monkeypatch):
    ed, s = _locked()
    cv = ed._canvas
    notices, edits = [], []
    monkeypatch.setattr(cv, "_lock_notice", lambda it, pt: notices.append(it))
    monkeypatch.setattr(cv, "edit_item", lambda it: edits.append(it))
    g = cv._geom
    _press(cv, g[s["rf"].uid]["cx"], _cy(cv, s["rf"]))
    assert notices == [s["rf"]] and not cv._selection and cv._drag is None and cv._connect is None
    _press(cv, g[s["runup"].uid]["start_x"] + 20, _cy(cv, s["runup"]), QEvent.Type.MouseButtonDblClick)
    assert notices[-1] is s["runup"] and edits == []
    _press(cv, cv._enter_x + 5, LANES_TOP + 5)                                    # the Hold
    assert notices[-1] is s["hold"] and cv._drag is None
    # A window-B pin still selects / starts a connect-drag as usual.
    _press(cv, g[s["wb"].uid]["cx"], _cy(cv, s["wb"]))
    assert cv._selected == s["wb"].uid and cv._connect is not None


def test_select_all_and_the_marquee_skip_locked_items():
    ed, s = _locked()
    cv = ed._canvas
    cv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier))
    live = {s["rem"].uid, s["wb"].uid, s["off"].uid}
    assert cv._selection == live
    cv._clear_selection()
    cv._marquee = {"x0": 0.0, "y0": 0.0, "x1": float(cv._content_w), "y1": float(cv._baseline),
                   "additive": False, "base": set(), "moved": True}
    cv._apply_marquee()
    assert cv._selection == live


def test_nothing_may_anchor_to_what_already_happened():
    ed, s = _locked()
    cv = ed._canvas
    g = cv._geom
    src = s["wb"].uid
    # the run-up ramp's end dot, the RF-on pin (on the on-air line) and the on-air line itself
    assert cv._drop_target(g[s["runup"].uid]["stop_x"], _cy(cv, s["runup"]), src) is None
    assert cv._drop_target(g[s["rf"].uid]["cx"], _cy(cv, s["rf"]), src) is None
    assert cv._drop_target(cv._on, _cy(cv, s["rem"]), src) is None
    # the running task's START edge is history, its STOP edge is a fine target
    assert cv._drop_target(g[s["bar"].uid]["start_x"], _cy(cv, s["bar"]), src) is None
    tgt = cv._drop_target(g[s["bar"].uid]["stop_x"], _cy(cv, s["bar"]), src)
    assert tgt is not None and tgt[0] is s["bar"] and tgt[1] == "end"
    # a window-B step is a normal target
    tgt = cv._drop_target(g[s["rem"].uid]["start_x"], _cy(cv, s["rem"]), src)
    assert tgt is not None and tgt[0] is s["rem"]


def test_drags_cannot_reach_back_into_the_past():
    ed, s = _locked()
    cv = ed._canvas
    assert cv._clamp_tune_offset(s["wb"], -30.0) == 0.0            # window B: never before resume
    floor = (cv._resume_x - cv._off) / cv._eff()
    assert cv._clamp_tune_offset(s["off"], -5000.0) == pytest.approx(floor)
    ed2 = _chirp_editor(list(_scene().values()))                  # unlocked: free as before
    wb2 = next(it for it in ed2._canvas.items() if getattr(it, "anchor", "") == "hold"
               and getattr(it, "action", "") == "tune")
    assert ed2._canvas._clamp_tune_offset(wb2, -30.0) == -30.0


def test_the_running_tasks_launch_gate_is_left_alone(monkeypatch):
    spec = [{"dest": "rf", "flags": ["--rf"], "choices": ["on", "off"]}]
    bar = _bar(start=-5.0)                                        # started before on-air → muted launch
    ed, _ = _locked([bar, _hold(300.0), _tune(20.0, anchor="hold", bw=9)])
    monkeypatch.setattr(ed, "task_param_specs", lambda task: spec)
    args0 = list(bar.args)
    assert ed._canvas._auto_rf_gate(bar) is False                  # nothing touched: it already launched
    assert bar.args == args0 and not any(getattr(it, "params", None) == {"rf": "on"}
                                         for it in ed._canvas.items())
    ed2 = _chirp_editor([_bar(start=-5.0), _hold(300.0)])
    monkeypatch.setattr(ed2, "task_param_specs", lambda task: spec)
    assert ed2._canvas._auto_rf_gate(ed2._canvas.items()[0]) is True   # unlocked: the usual gating


def test_new_steps_seed_in_the_post_hold_window():
    ed, _ = _locked()
    cv = ed._canvas
    assert cv._seed_item("tune").anchor == "hold"
    assert cv._seed_item("ramp").anchor == "hold"
    assert cv._seed_item("run").anchor == "hold"
    assert cv._seed_item("bar").start_anchor == "hold"
    ed2 = _chirp_editor(list(_scene().values()))
    assert ed2._canvas._seed_item("tune").anchor == "start"
    assert ed2._canvas._seed_item("bar").start_anchor == "start"


# ── the picture: frost, grey, HOLDING, row header ────────────────────────────

def test_paint_frosts_the_elapsed_window_and_greys_its_steps():
    ed, s = _locked()
    cv = ed._canvas
    assert cv._hold_tag == "⏸ HOLDING"
    grey = ELAPSED_HUE.lower()
    assert cv._item_colors(s["rf"])[0].name().lower() == grey
    assert cv._item_colors(s["runup"])[0].name().lower() == grey
    assert cv._item_colors(s["wb"])[0].name().lower() != grey        # window B keeps its task hue
    assert cv._item_colors(s["bar"])[0].name().lower() != grey       # a running task keeps its hue
    cv.resize(cv._content_w, cv._content_h)
    pm = QPixmap(cv.width(), cv.height()); cv.render(pm)              # paints without error
    rr = cv._elapsed_ribbon
    assert rr is not None
    assert rr.left() > cv._on and rr.right() < cv._enter_x + HOLD_BAND_PX / 2   # clear of the tab
    hdr = ed._rowhdr
    hdr.resize(hdr.HDR_W, cv._content_h)
    hdr.render(QPixmap(hdr.width(), hdr.height()))                   # the header paints too
    assert hdr._meta(s["rf"])[1].endswith("· ran")
    assert hdr._meta(s["bar"])[1] == "runs through the Hold"
    assert hdr._meta(s["wb"])[1] == "bw=20"
    # Unlocked, the same scene paints no ribbon and the plain ⏸ HOLD tab.
    ed2 = _chirp_editor(list(_scene().values()))
    cv2 = ed2._canvas
    cv2.resize(cv2._content_w, cv2._content_h)
    cv2.render(QPixmap(cv2.width(), cv2.height()))
    assert cv2._elapsed_ribbon is None and cv2._hold_tag == "⏸ HOLD"


def test_tooltips_say_what_already_happened():
    ed, s = _locked()
    cv = ed._canvas
    assert cv._tooltip_text(s["rf"]).startswith("<b>Locked</b> · already ran before the Hold")
    assert cv._tooltip_text(s["bar"]).startswith("<b>Running</b> · started before the Hold")
    assert "holding now" in cv._tooltip_text(s["hold"])
    assert "Locked" not in cv._tooltip_text(s["wb"])
    ed2 = _chirp_editor(list(_scene().values()))
    rf2 = next(it for it in ed2._canvas.items() if getattr(it, "params", None) == {"rf": "on"})
    assert "Locked" not in ed2._canvas._tooltip_text(rf2)


# ── the dialog: locked on load, refuses a window-A change ─────────────────────

class _EditHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        client = type("C", (), {
            "list_tasks": lambda self_: [type("T", (), {"name": "chirp"})()],
            "get_tasks_yaml": lambda self_: ("tasks:\n  - name: chirp\n"
                                             "    command: [python3, chirp.py]\n"),
            "get_calibration": lambda self_: {"unit_type": "broadcaster", "valid": True,
                                              "signals": {}},
        })()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:                         # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _wire_seq():
    spec = m.RampSpec(param="power", start=-90.0, stop=-58.0, steps=3, duration_s=60.0)
    return [
        m.SequenceStep(anchor="start", offset_s=0.0, action="start", task_name="chirp"),
        m.SequenceStep(anchor="start", offset_s=0.0, action="tune", task_name="chirp",
                       params={"rf": "on"}),
        m.SequenceStep(anchor="start", offset_s=240.0, action="ramp", task_name="chirp",
                       ramp=spec, id="rmp"),
        m.SequenceStep(anchor="start", offset_s=300.0, action="hold", task_name=""),
        m.SequenceStep(anchor="hold", offset_s=20.0, action="tune", task_name="chirp",
                       params={"bw": 12}),
        m.SequenceStep(anchor="stop", offset_s=0.0, action="stop", task_name="chirp"),
    ]


def test_hold_edit_dialog_locks_the_elapsed_window_and_refuses_a_window_a_change():
    from ui.hold_edit_dialog import HoldEditDialog

    dlg = HoldEditDialog(_EditHub(), "unit", m.Sequence(id="s1", name="LoL", steps=_wire_seq()))
    _app.processEvents()
    tl = dlg._timeline
    assert tl.elapsed_locked() and tl._canvas._hold_tag == "⏸ HOLDING"
    assert "locked" in tl._hint.text()
    items = tl.items()
    runup = next(it for it in items if getattr(it, "action", "") == "ramp")
    wb = next(it for it in items if getattr(it, "anchor", "") == "hold")
    assert tl.elapsed_kind(runup) == "full" and tl.elapsed_kind(wb) is None
    assert tl.elapsed_kind(next(it for it in items if it.kind == "bar")) == "start"
    # Untouched (or window B edited) → accepted with the Hold + window B intact.
    wb.params = {"bw": 15}
    dlg._accept()
    assert dlg.result_steps is not None
    assert any(s.action == m.StepAction.HOLD for s in dlg.result_steps)
    assert any(s.anchor == "hold" and s.params == {"bw": 15} for s in dlg.result_steps)
    # A change to a step that already ran (slipped past the canvas) is refused.
    dlg.result_steps = None
    runup.offset = 200.0
    tl._canvas.relayout()
    dlg._accept()
    assert dlg.result_steps is None
    assert "already run" in dlg._status.text()
    dlg.close()


# ── code-review follow-ups: the lock holds at the MUTATION layer too ─────────

def test_the_running_task_is_untouchable_through_its_live_stop_grip(monkeypatch):
    ed, s = _locked()
    cv = ed._canvas
    g = cv._geom
    notices, menus = [], []
    monkeypatch.setattr(cv, "_lock_notice", lambda it, pt: notices.append(it))
    monkeypatch.setattr(cv, "_open_context_menu", lambda it, pos, x=None: menus.append(it))
    # A press on the LIVE stop grip starts the resize drag but selects nothing (a selection would
    # expose Delete / Ctrl+D / the menu on a task that already launched).
    cv._select_only(s["wb"].uid)
    _press(cv, g[s["bar"].uid]["stop_x"] - 2, _cy(cv, s["bar"]))
    assert cv._drag is not None and cv._drag["part"] == "bar_stop" and not cv._selection
    cv._drag = None
    # Right-click on the same grip (its post-hold stretch) → only what's still ahead of the task:
    # Tune… / Ramp… / Stop offset… — never Edit… / Duplicate / Delete, and it stays unselected.
    x, y = g[s["bar"].uid]["stop_x"] - 2, _cy(cv, s["bar"])
    cv.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(int(x), int(y)),
                                          QPoint(int(x), int(y))))
    assert menus == [s["bar"]] and not notices and not cv._selection
    assert cv._context_menu_spec(s["bar"]) == ["Tune…", "Ramp…", "—", "Stop offset…"]
    n = len(cv.items())
    # The mutation layer refuses what already happened, whichever path reaches it.
    cv._delete_uids({s["bar"].uid})
    cv._delete_uids({s["rf"].uid, s["runup"].uid, s["hold"].uid})
    assert len(cv.items()) == n and not cv.can_undo()
    cv._duplicate_item(s["runup"]); cv._duplicate_item(s["bar"])
    assert len(cv.items()) == n
    cv.edit_item(s["bar"]); cv.edit_item(s["rf"])
    assert notices[-2:] == [s["bar"], s["rf"]]
    # Even a forced selection (a bypass) + Delete / Ctrl+D leaves the running task alone…
    cv._selection = {s["bar"].uid}; cv._selected = s["bar"].uid
    cv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    cv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier))
    assert len(cv.items()) == n and any(it is s["bar"] for it in cv.items())
    # …while a window-B step still deletes as usual (its own undo step).
    cv._delete_uids({s["wb"].uid})
    assert len(cv.items()) == n - 1 and cv.can_undo()


def test_elapsed_classification_is_cached_per_layout_and_follows_edits():
    ed, s = _locked()
    cv = ed._canvas
    assert set(cv._elapsed) == {it.uid for it in cv.items()}          # one entry per item
    assert cv._elapsed[s["runup"].uid] == "full" and cv._elapsed[s["wb"].uid] is None
    # A step added later is classified as soon as it is laid out.
    late = _tune(10.0, bw=5)                                            # window A (on-air +10)
    cv.add_item(late)
    assert cv.elapsed_kind(late) == "full" and cv._elapsed[late.uid] == "full"
    # Unlocking clears the classification; re-locking restores it without a relayout.
    ed.set_elapsed_locked(False)
    assert cv._elapsed == {} and cv.elapsed_kind(s["runup"]) is None
    ed.set_elapsed_locked(True)
    assert cv.elapsed_kind(s["runup"]) == "full" and cv.elapsed_kind(s["bar"]) == "start"


def test_hold_edit_dialog_refuses_when_window_a_cannot_be_signed(monkeypatch):
    from ui.hold_edit_dialog import HoldEditDialog

    dlg = HoldEditDialog(_EditHub(), "unit", m.Sequence(id="s1", name="LoL", steps=_wire_seq()))
    _app.processEvents()
    assert dlg._window_a_sig                                            # signed at load
    monkeypatch.setattr(dlg, "_window_a_signature", lambda: None)      # a probe failure at accept
    dlg._accept()
    assert dlg.result_steps is None and "couldn't verify" in dlg._status.text()
    dlg.close()
