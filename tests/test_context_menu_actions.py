"""Right-click context menus on the sequence canvas (owner ask: a duration task's menu = edit ·
set the offset from its anchor · tune… · ramp… · delete).

One shape for every kind — Edit… · its offset(s) · Duplicate (a step) · Remove anchor (when hung
off another step) · Delete — plus Tune… / Ramp… on a duration task (a new step on THAT task,
pre-filled at the time under the cursor, anchored to the window it was clicked in), an "Add …"
menu on empty canvas at the clicked time, and inline offset entries at the cursor captioned with
the reference they're measured from. In the Hold-edit (locked) mode the task running THROUGH the
Hold takes Tune… / Ramp… / Stop offset… on its post-hold stretch only.
"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.timeline_editor import HOLD_BAND_PX, LANE_H, _OffsetPopup
from tests.test_step_editor_carried_bw import _editor as _chirp_editor

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# ── fixtures ─────────────────────────────────────────────────────────────────

def _bar(start=0.0, stop=0.0, **kw):
    return tlm.BarItem(task_name="chirp", args=["--freq", "1575.42", "--power", "-7.38", "--bw", "10"],
                       start_offset=start, stop_offset=stop, **kw)


def _tune(off, anchor="start", sid="", **params):
    return tlm.RunItem(task_name="chirp", action="tune", anchor=anchor, offset=off,
                       params=params or {"bw": 12}, step_id=sid)


def _ramp(off, anchor="start", dur=60.0, sid="", **kw):
    return tlm.RunItem(task_name="chirp", action="ramp", anchor=anchor, offset=off, step_id=sid,
                       ramp={"param": "power", "start": -90.0, "stop": -58.0, "steps": 3,
                             "duration_s": dur}, **kw)


def _oneshot(off):
    return tlm.RunItem(task_name="atten", action="run", anchor="start", offset=off)


def _hold(off=300.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=off)


def _editor(items, tasks=("chirp", "atten")):
    ed = _chirp_editor(items)
    ed.set_tasks(list(tasks))
    return ed


def _cy(cv, it):
    return cv._geom[it.uid]["y"] + LANE_H / 2


def _rclick(cv, x, y):
    cv.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(int(x), int(y)),
                                          QPoint(int(x), int(y))))


class _Captured:
    """Stands in for the step / ramp / Hold editor: records the seeded item, opens nothing."""
    REMOVE = 2

    def __init__(self, sink, item):
        sink.append(item)
        self.result_item = None

    def exec(self):
        return 0                                        # Rejected


def _capture_dialogs(monkeypatch, cv):
    seeded = []
    monkeypatch.setattr(cv, "_dialog_for", lambda item, new: _Captured(seeded, item))
    return seeded


# ── the menu's shape, per kind ───────────────────────────────────────────────

def test_menu_shape_per_kind():
    bar, tune, ramp, one, hold = _bar(), _tune(30.0), _ramp(60.0), _oneshot(-20.0), _hold(300.0)
    cv = _editor([bar, tune, ramp, one, hold])._canvas
    assert cv._context_menu_spec(bar) == ["Edit…", "Start offset…", "Stop offset…", "—",
                                          "Tune…", "Ramp…", "—", "Delete"]
    assert "Duplicate" not in cv._context_menu_spec(bar)          # one bar per task (rule E)
    for it in (tune, ramp, one):
        assert cv._context_menu_spec(it) == ["Edit…", "Offset…", "Duplicate", "—", "Delete"]
    assert cv._context_menu_spec(hold) == ["Edit…", "Offset…", "—", "Delete"]
    # A multi-selection acts on the whole set.
    cv._selection = {tune.uid, ramp.uid}; cv._selected = tune.uid
    assert cv._context_menu_spec(tune) == ["Delete selected"]


def test_remove_anchor_is_conditional_and_a_both_ramp_has_no_offset():
    up = _ramp(0.0, sid="up")
    down = _ramp(3.0, anchor="step", anchor_step_id="up", anchor_edge="end")
    lead = _tune(-5.0, sid="lead")
    hung = _bar(start=10.0, start_anchor="step", start_anchor_step_id="lead", start_anchor_edge="end")
    fill = _ramp(0.0, anchor="both")
    cv = _editor([hung, up, down, lead, fill])._canvas
    assert cv._context_menu_spec(down) == ["Edit…", "Offset…", "Duplicate", "Remove anchor", "—", "Delete"]
    assert cv._context_menu_spec(hung) == ["Edit…", "Start offset…", "Stop offset…", "Remove anchor",
                                           "—", "Tune…", "Ramp…", "—", "Delete"]
    assert cv._context_menu_spec(fill) == ["Edit…", "Duplicate", "—", "Delete"]     # no free offset
    assert cv._offset_entries(fill) == []


# ── the time under the cursor → (anchor, offset) of the window it lies in ────

def test_window_at_x_follows_the_window_under_the_cursor():
    cv = _editor([_bar(), _tune(30.0), _tune(-20.0, anchor="stop")])._canvas
    eff, on, off = cv._eff(), cv._on, cv._off
    assert cv._window_at_x(on + 10 * eff) == ("start", 10.0)          # the green on-air window
    assert cv._window_at_x(on - 7 * eff) == ("start", -7.0)           # the warm-up before on-air
    assert cv._window_at_x(off - 5 * eff) == ("stop", -5.0)           # the red off-air window
    assert cv._window_at_x(off + 12 * eff) == ("stop", 12.0)          # the cool-down past off-air
    # In the hatched relative stretch the nearer absolute window wins.
    def_x, off_def = cv._def_x(), cv._off_def_x()
    assert off_def > def_x
    assert cv._window_at_x(def_x + 3)[0] == "start"
    assert cv._window_at_x(off_def - 3)[0] == "stop"


def test_window_at_x_with_a_hold_reads_resume_offsets_after_the_band():
    cv = _editor([_bar(), _hold(300.0), _tune(90.0, anchor="hold"), _tune(-10.0, anchor="stop")])._canvas
    eff, off = cv._eff(), cv._off
    enter, resume = cv._enter_x, cv._resume_x
    assert resume == enter + HOLD_BAND_PX
    assert cv._window_at_x(enter - 3 * eff) == ("start", 297.0)       # window A: on-air offset
    assert cv._window_at_x(enter + 10) == ("hold", 0.0)               # anywhere on the band: resume
    assert cv._window_at_x(resume + 20 * eff) == ("hold", 20.0)       # post-hold: forward from resume
    assert cv._window_at_x(off - 5 * eff) == ("stop", -5.0)           # the off-air window
    # Nothing after the Hold: resume IS off-air (merged) — everything past it counts from resume.
    cv2 = _editor([_bar(), _hold(300.0)])._canvas
    assert cv2._hold_merged
    assert cv2._window_at_x(cv2._resume_x + 30 * cv2._eff()) == ("hold", 30.0)


# ── Tune… / Ramp… on a duration task: pre-filled at the click, clamped inside it ──

def test_tune_and_ramp_from_a_task_menu_seed_that_task_at_the_click_time(monkeypatch):
    bar = _bar()
    ed = _editor([bar, _tune(30.0)])
    cv = ed._canvas
    seeded = _capture_dialogs(monkeypatch, cv)
    eff, on, off = cv._eff(), cv._on, cv._off
    cv._run_context_action(bar, "Tune…", x=on + 45 * eff)
    t = seeded[-1]
    assert t.action == "tune" and t.task_name == "chirp" and t.anchor == "start" and t.offset == 45.0
    cv._run_context_action(bar, "Ramp…", x=on + 45 * eff)
    r = seeded[-1]
    assert r.action == "ramp" and r.task_name == "chirp" and r.anchor == "start" and r.offset == 45.0
    assert r.ramp == {}                                             # the editor fills the rest in
    # Clamped inside the task like a drag: past its end → its off-air end; before on-air → its start.
    cv._run_context_action(bar, "Tune…", x=off + 30 * eff)
    assert (seeded[-1].anchor, seeded[-1].offset) == ("stop", 0.0)
    cv._run_context_action(bar, "Tune…", x=on - 30 * eff)
    assert (seeded[-1].anchor, seeded[-1].offset) == ("start", 0.0)
    # No click position (keyboard) → at the task's start.
    cv._run_context_action(bar, "Tune…")
    assert (seeded[-1].anchor, seeded[-1].offset) == ("start", 0.0)
    assert len(cv.items()) == 2                                     # every dialog was cancelled


def test_the_task_menus_open_the_right_editor(monkeypatch):
    """Tune… opens the step editor as a TUNE on the task; Ramp… the ramp editor — both pre-filled."""
    from ui.ramp_editor import RampEditorDialog
    from ui.timeline_editor import StepEditorDialog
    bar = _bar()
    cv = _editor([bar])._canvas
    opened = []
    monkeypatch.setattr(StepEditorDialog, "exec", lambda self: opened.append(self) or 0)
    monkeypatch.setattr(RampEditorDialog, "exec", lambda self: opened.append(self) or 0)
    cv._run_context_action(bar, "Tune…", x=cv._on + 20 * cv._eff())
    dlg = opened[-1]
    assert isinstance(dlg, StepEditorDialog)
    assert dlg._type.currentData() == "tune" and dlg._task.currentText() == "chirp"
    assert dlg._anchor.currentData() == "start" and dlg._run_off.value() == 20.0
    cv._run_context_action(bar, "Ramp…", x=cv._on + 20 * cv._eff())
    dlg = opened[-1]
    assert isinstance(dlg, RampEditorDialog)
    assert dlg._task.currentText() == "chirp"
    assert dlg._anchor.currentData() == "start" and dlg._offset.value() == 20.0
    for d in opened:
        d.close()


# ── the empty-canvas menu ────────────────────────────────────────────────────

def test_empty_canvas_menu_adds_at_the_click_time_on_the_rows_task(monkeypatch):
    bar = _bar()
    ed = _editor([bar, _tune(30.0)])
    cv = ed._canvas
    seeded = _capture_dialogs(monkeypatch, cv)
    eff, on = cv._eff(), cv._on
    assert cv._canvas_menu_spec(on) == ["Add duration task…", "Add one-shot…", "Add tune…",
                                        "Add ramp…", "—", "Add Hold"]
    y_bar = _cy(cv, bar)
    cv._run_canvas_action("Add tune…", on + 50 * eff, y_bar)
    t = seeded[-1]
    assert t.action == "tune" and t.task_name == "chirp" and (t.anchor, t.offset) == ("start", 50.0)
    cv._run_canvas_action("Add ramp…", on + 50 * eff, y_bar)
    assert seeded[-1].action == "ramp" and seeded[-1].task_name == "chirp"
    cv._run_canvas_action("Add one-shot…", on - 15 * eff, y_bar + 500)     # far below every row
    o = seeded[-1]
    assert o.action == "run" and o.task_name == "chirp" and (o.anchor, o.offset) == ("start", -15.0)
    cv._run_canvas_action("Add duration task…", on + 12 * eff, y_bar + 500)
    b = seeded[-1]
    assert b.kind == "bar" and b.start_anchor == "start" and b.start_offset == 12.0
    cv._run_canvas_action("Add Hold", on + 80 * eff, y_bar + 500)
    h = seeded[-1]
    assert tlm._is_hold(h) and h.offset == 80.0
    # Once a Hold exists it isn't offered again.
    cv.add_item(_hold(300.0))
    assert "Add Hold" not in cv._canvas_menu_spec(on)
    # A task's tune row names the task too; a row with no duration task → the default.
    tune = next(it for it in cv.items() if getattr(it, "action", "") == "tune")
    assert cv._task_at_y(_cy(cv, tune)) == "chirp"
    assert cv._task_at_y(y_bar + 500) == ""


def test_right_click_routes_to_the_item_or_the_canvas_menu(monkeypatch):
    bar, tune = _bar(), _tune(30.0)
    cv = _editor([bar, tune])._canvas
    item_menus, canvas_menus = [], []
    monkeypatch.setattr(cv, "_open_context_menu", lambda it, pos, x=None: item_menus.append((it, x)))
    monkeypatch.setattr(cv, "_open_canvas_menu", lambda x, y, pos: canvas_menus.append((x, y)) or True)
    g = cv._geom[tune.uid]
    _rclick(cv, g["cx"], _cy(cv, tune))
    assert item_menus[-1][0] is tune and cv._selected == tune.uid       # re-selects the item
    assert item_menus[-1][1] == float(int(g["cx"]))                     # the click x rides along
    _rclick(cv, cv._on + 40 * cv._eff(), _cy(cv, tune) + 400)          # empty canvas
    assert canvas_menus and len(item_menus) == 1


# ── the inline offset entry ──────────────────────────────────────────────────

def test_apply_offset_goes_through_the_drag_clamps_in_one_undo_step():
    bar, tune, hold = _bar(), _tune(30.0), _hold(300.0)
    up = _ramp(0.0, sid="up")
    dep = _ramp(3.0, anchor="step", anchor_step_id="up", anchor_edge="end")
    cv = _editor([bar, tune, hold, up, dep])._canvas
    n = len(cv._undo)
    assert cv._apply_offset(tune, "offset", 45.0) == 45.0 and tune.offset == 45.0
    assert len(cv._undo) == n + 1
    # A no-op entry pushes no undo step.
    assert cv._apply_offset(tune, "offset", 45.0) == 45.0 and len(cv._undo) == n + 1
    # A bar's start and stop; the Hold's position; a step-anchored offset (negative allowed).
    assert cv._apply_offset(bar, "start", -20.0) == -20.0 and bar.start_offset == -20.0
    assert cv._apply_offset(bar, "stop", 30.0) == 30.0 and bar.stop_offset == 30.0
    assert cv._apply_offset(hold, "offset", 250.0) == 250.0 and hold.offset == 250.0
    assert cv._apply_offset(dep, "offset", -5.0) == -5.0 and dep.offset == -5.0
    # A pause-anchored step never reaches into the pause.
    en = _tune(-5.0, anchor="enter", bw=9)
    cv.add_item(en)
    assert cv._apply_offset(en, "offset", 4.0) == 0.0
    # An item no longer on the canvas is refused.
    gone = _tune(10.0)
    assert cv._apply_offset(gone, "offset", 1.0) is None
    # A tune stays inside its task (its end is the bar's off-air stop, drawn at off_x) — and one
    # Ctrl+Z takes the whole entry back (undo restores snapshot copies, so re-find the tune).
    top = (cv._off - cv._on) / cv._eff() + 30.0        # the bar's stop is now +30 past off-air
    assert cv._apply_offset(tune, "offset", 1e5) == pytest.approx(top) and tune.offset == pytest.approx(top)
    cv.undo()
    tune = next(it for it in cv.items() if getattr(it, "action", "") == "tune" and it.params == {"bw": 12})
    assert tune.offset == 45.0


def test_offset_popup_captions_name_the_reference_and_enter_commits():
    bar, wb, off, en, hold = (_bar(), _tune(90.0, anchor="hold"), _tune(-10.0, anchor="stop"),
                              _tune(-5.0, anchor="enter", bw=9), _hold(300.0))
    up = _ramp(0.0, sid="up")
    dep = _ramp(3.0, anchor="step", anchor_step_id="up", anchor_edge="end")
    lead = _tune(20.0, sid="lead")
    hung = _bar(start=10.0, start_anchor="step", start_anchor_step_id="lead", start_anchor_edge="end")
    hung.task_name = "atten"
    cv = _editor([bar, wb, off, en, hold, up, dep, lead, hung])._canvas
    at = QPoint(0, 0)
    pops = []

    def cap(it, which):
        p = cv._prompt_offset(it, which, at)
        pops.append(p)
        return p.caption()

    assert cap(bar, "start") == "Start — from on-air"
    assert cap(bar, "stop") == "Stop — from off-air"
    assert cap(wb, "offset") == "Offset — from resume"
    assert cap(off, "offset") == "Offset — from off-air"
    assert cap(en, "offset") == "Offset — from the pause (≤ 0)"
    assert cap(hold, "offset") == "Pause at — from on-air"
    assert cap(dep, "offset") == "Offset — from Ramp · chirp's end"
    assert cap(hung, "start") == "Start — from Tune · chirp's end"
    # The popup opens on the current value; Enter applies it through _apply_offset.
    p = cv._prompt_offset(bar, "start", at)
    assert isinstance(p, _OffsetPopup) and p.value() == 0.0
    p.set_value(25.0)
    p._commit()
    assert bar.start_offset == 25.0
    p = cv._prompt_offset(wb, "offset", at)
    p.set_value(-4.0)                                  # unlocked, a window-B step may dwell into
    p._commit()                                        # the pause ("pre-hold") — plain offset
    assert wb.offset == -4.0
    for q in pops:
        q.close()


# ── Hold-edit (locked): the running task takes only what's still ahead ────────

def _locked_scene():
    s = {"bar": _bar(), "rf": _tune(0.0, rf="on"), "runup": _ramp(240.0), "hold": _hold(300.0),
         "wb": _tune(90.0, anchor="hold", bw=20), "off": _tune(-10.0, anchor="stop", rf="off")}
    ed = _editor(list(s.values()))
    ed.set_elapsed_locked(True)
    return ed, s


def test_locked_running_task_menu_only_on_its_post_hold_stretch(monkeypatch):
    ed, s = _locked_scene()
    cv = ed._canvas
    notices, menus, canvas_menus = [], [], []
    monkeypatch.setattr(cv, "_lock_notice", lambda it, pt: notices.append(it))
    monkeypatch.setattr(cv, "_open_context_menu", lambda it, pos, x=None: menus.append((it, x)))
    monkeypatch.setattr(cv, "_open_canvas_menu",
                        lambda x, y, pos: canvas_menus.append(cv._canvas_menu_spec(x)) or True)
    bar = s["bar"]
    y = _cy(cv, bar)
    # Its ELAPSED stretch → the notice; its POST-HOLD stretch → the restricted menu, unselected.
    _rclick(cv, cv._enter_x - 20, y)
    assert notices[-1] is bar and menus == []
    _rclick(cv, cv._resume_x + 20, y)
    assert menus[-1][0] is bar and not cv._selection
    assert cv._context_menu_spec(bar) == ["Tune…", "Ramp…", "—", "Stop offset…"]
    # A fired step and the Hold band: the notice.
    _rclick(cv, cv._geom[s["rf"].uid]["cx"], _cy(cv, s["rf"]))
    assert notices[-1] is s["rf"]
    _rclick(cv, cv._enter_x + 5, _cy(cv, s["runup"]))
    assert notices[-1] is s["hold"]
    # A window-B step keeps its full menu.
    _rclick(cv, cv._geom[s["wb"].uid]["cx"], _cy(cv, s["wb"]))
    assert menus[-1][0] is s["wb"]
    assert cv._context_menu_spec(s["wb"]) == ["Edit…", "Offset…", "Duplicate", "—", "Delete"]
    # Empty canvas: nothing on the elapsed side; Add… (never a Hold) on the post-hold side.
    assert cv._canvas_menu_spec(cv._enter_x - 20) == []
    assert cv._canvas_menu_spec(cv._resume_x + 20) == ["Add duration task…", "Add one-shot…",
                                                       "Add tune…", "Add ramp…"]


def test_locked_mutations_refuse_what_already_happened(monkeypatch):
    ed, s = _locked_scene()
    cv = ed._canvas
    notices = []
    monkeypatch.setattr(cv, "_lock_notice", lambda it, pt: notices.append(it))
    bar = s["bar"]
    # The running task's START is history; its STOP still moves. A fired step doesn't.
    assert cv._apply_offset(bar, "start", -20.0) is None and bar.start_offset == 0.0
    assert cv._apply_offset(bar, "stop", 20.0) == 20.0 and bar.stop_offset == 20.0
    assert cv._apply_offset(s["rf"], "offset", 5.0) is None and s["rf"].offset == 0.0
    assert cv._apply_offset(s["hold"], "offset", 250.0) is None and s["hold"].offset == 300.0
    assert cv._prompt_offset(bar, "start", QPoint(0, 0)) is None and notices[-1] is bar
    assert cv._prompt_offset(s["rf"], "offset", QPoint(0, 0)) is None and notices[-1] is s["rf"]
    # A window-B step can't be entered back into the past.
    assert cv._apply_offset(s["wb"], "offset", -30.0) == 0.0
    # Tune… / Ramp… on the running task seed in the POST-HOLD window, at the clicked resume time.
    seeded = _capture_dialogs(monkeypatch, cv)
    eff = cv._eff()
    cv._run_context_action(bar, "Tune…", x=cv._resume_x + 25 * eff)
    assert (seeded[-1].anchor, seeded[-1].offset, seeded[-1].task_name) == ("hold", 25.0, "chirp")
    cv._run_context_action(bar, "Ramp…", x=cv._resume_x + 25 * eff)
    assert seeded[-1].action == "ramp" and seeded[-1].anchor == "hold" and seeded[-1].offset == 25.0
    # Even a seed asked for on the on-air clock lands post-hold while locked (the backstop).
    cv.add_new("tune", task="chirp", anchor="start", offset=10.0)
    assert seeded[-1].anchor == "hold" and seeded[-1].offset == 10.0
    # The canvas "Add …" on the post-hold side likewise.
    cv._run_canvas_action("Add one-shot…", cv._resume_x + 40 * eff, _cy(cv, bar) + 500)
    assert (seeded[-1].anchor, seeded[-1].offset) == ("hold", 40.0)
