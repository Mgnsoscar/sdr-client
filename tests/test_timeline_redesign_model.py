"""Editor-redesign foundations (pure, no Qt): per-task colour + Gantt row ordering."""
from ui import timeline_model as tlm
from ui.timeline_model import RunItem, BarItem


def _bar(task): return BarItem(task_name=task, start_offset=0.0, stop_offset=0.0)
def _tune(task, off): return RunItem(task_name=task, action="tune", anchor="start", offset=off,
                                     params={"x": 1})
def _ramp(task, off): return RunItem(task_name=task, action="ramp", anchor="start", offset=off,
                                     ramp={"param": "power"})
def _one(task, off): return RunItem(task_name=task, action="run", anchor="start", offset=off)
def _hold(off=200): return RunItem(task_name="", action="hold", anchor="start", offset=off)


def test_task_hue_map_first_seen_and_inherited():
    items = [_bar("ca"), _ramp("ca", 0), _ramp("ca", 300), _bar("chirp"), _tune("chirp", 30),
             _one("marker", 360)]
    hue = tlm.task_hue_map(items)
    assert hue["ca"] == tlm.TASK_HUES[0]
    assert hue["chirp"] == tlm.TASK_HUES[1]
    assert hue["marker"] == tlm.TASK_HUES[2]
    # a tune/ramp shares its parent task_name → same hue (inheritance is automatic)
    assert all(it.task_name == "ca" for it in items[:3])


def test_task_hue_map_ignores_holds_and_blanks():
    items = [_hold(), _bar("a"), _bar("b")]
    hue = tlm.task_hue_map(items)
    assert set(hue) == {"a", "b"} and hue["a"] != hue["b"]


def test_display_order_groups_steps_under_their_task():
    b_ca, r_up, r_dn = _bar("ca"), _ramp("ca", 0), _ramp("ca", 300)
    b_ch, t_rf = _bar("chirp"), _tune("chirp", 30)
    mk = _one("marker", 360)
    hold = _hold(400)
    # deliberately scrambled input order
    rows, holds = tlm.display_order([r_dn, mk, b_ch, hold, b_ca, t_rf, r_up])
    names = [r.task_name for r in rows]
    # ca group first (bar, then its ramps by fire time), then chirp group, then marker
    assert names == ["ca", "ca", "ca", "chirp", "chirp", "marker"]
    assert rows[0] is b_ca                      # the bar leads its group
    assert rows[1] is r_up and rows[2] is r_dn  # ramps by fire time (0 before 300)
    assert rows[3] is b_ch and rows[4] is t_rf
    assert holds == [hold]                       # holds pulled out (own no row)


def test_display_order_does_not_mutate_input():
    items = [_ramp("ca", 300), _bar("ca")]
    before = list(items)
    tlm.display_order(items)
    assert items == before
