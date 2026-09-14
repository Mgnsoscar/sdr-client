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


def test_display_order_puts_ramps_before_tunes_within_a_task():
    # Under a duration task the rows sort by KIND: the bar, then RAMPS, then TUNES, then
    # one-shot runs — regardless of authored order or fire time across kinds.
    b = _bar("ca")
    t_early = _tune("ca", 5)          # a tune that fires BEFORE the ramp
    r = _ramp("ca", 40)              # a ramp that fires AFTER the tune
    t_late = _tune("ca", 90)
    rows, _ = tlm.display_order([b, t_early, r, t_late])
    assert rows[0] is b               # bar leads
    assert rows[1] is r               # ramp next, even though it fires after t_early
    assert rows[2] is t_early and rows[3] is t_late   # then tunes, by fire time


def test_display_order_ramp_then_tune_then_oneshot_ranks():
    b = _bar("ca")
    one = _one("ca", 10)
    tune = _tune("ca", 20)
    ramp = _ramp("ca", 30)
    rows, _ = tlm.display_order([b, one, tune, ramp])
    assert [r for r in rows] == [b, ramp, tune, one]


def test_display_order_oneshot_is_its_own_group_not_under_a_task():
    # A one-shot run stands alone: even sharing a task_name with a duration bar it must NOT
    # be pulled under that bar's group — it launches a task once, it doesn't modify one.
    b = _bar("ca")
    tune = _tune("ca", 20)
    shot = _one("ca", 40)              # same task_name as the bar, but a one-shot
    rows, _ = tlm.display_order([b, tune, shot])
    # ca bar + its tune form one group; the one-shot slots in by its own fire time (40 > 20),
    # as its own row — it is NOT sorted last-within-the-ca-group by kind rank.
    assert rows.index(shot) > rows.index(tune)   # after (one-shots sit at the bottom)
    # a one-shot is its own group AND is pinned to the BOTTOM — even one firing before the
    # bar goes on air sits below the whole ca task group, never nested in it.
    late_bar = BarItem(task_name="ca", start_offset=30.0, stop_offset=0.0)
    late_tune = _tune("ca", 35)
    early_shot = _one("ca", 5)                    # a one-shot BEFORE the bar goes on air
    rows2, _ = tlm.display_order([late_bar, late_tune, early_shot])
    assert rows2 == [late_bar, late_tune, early_shot]   # task group first, one-shot last


def test_display_order_all_oneshots_sink_to_the_bottom():
    # One-shots are independent of the tasks, so they collect at the bottom regardless of
    # their fire times — never interleaved between duration-task groups.
    b_ca = _bar("ca")
    t_ca = _tune("ca", 40)
    b_ch = BarItem(task_name="chirp", start_offset=100.0, stop_offset=0.0)
    early = _one("marker", 5)      # fires first of all
    mid = _one("ping", 70)         # between the two task groups by fire time
    late = _one("beep", 300)
    rows, _ = tlm.display_order([early, b_ca, t_ca, mid, b_ch, late])
    names = [r.task_name for r in rows]
    # both task groups first (ca then chirp by fire), then the three one-shots by fire time
    assert names == ["ca", "ca", "chirp", "marker", "ping", "beep"]


def test_display_order_two_oneshots_same_task_are_separate_rows():
    a = _one("cw", 10)
    c = _one("cw", 60)
    rows, _ = tlm.display_order([c, a])
    assert rows == [a, c]                          # two independent rows, by fire time


def test_display_order_does_not_mutate_input():
    items = [_ramp("ca", 300), _bar("ca")]
    before = list(items)
    tlm.display_order(items)
    assert items == before


def test_display_order_orders_step_anchored_rows_by_resolved_time():
    # A step anchored to a LATE target resolves late even with a small offset, so it must sort
    # by its RESOLVED fire time (target edge + offset), not its raw offset-from-edge — else a
    # +5 s dependent of a 200 s step would wrongly jump to the top of the group.
    b = _bar("ca")
    late = RunItem(task_name="ca", action="tune", anchor="start", offset=200.0,
                   step_id="late", params={"x": 1})
    early = _tune("ca", 60)                                 # plain start at 60
    dep = RunItem(task_name="ca", action="tune", anchor="step", offset=5.0,
                  anchor_step_id="late", anchor_edge="start", params={"x": 1})   # resolves at 205
    rows, _ = tlm.display_order([b, late, early, dep])
    assert rows.index(early) < rows.index(dep)             # 60 before 205 (not before the raw +5)
    assert rows.index(late) < rows.index(dep)              # 200 before 205
