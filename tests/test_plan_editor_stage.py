"""The plan editor stage (ui/plan_editor.py): every sequence at its resolved window, the embedded
sequence canvases aligned to the plan axis, cross-sequence anchoring by drop (sequence ↔ sequence,
step ↔ step across sequences, with a loop refused), detach-in-place, sequence drags, channel
conflicts, folding into lanes, healing of dangling references, and the PlanItem round-trip."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from api import models as m
from ui import plan_editor as pe
from ui import plan_graph as pg
from ui import timeline_model as tlm
from tests.test_plan_canvas_paint import _Hub, _steps, _item

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _editor(items, hosts=("a", "b")):
    seqs = {h: [m.Sequence(id="s1", name="PRN", steps=_steps())] for h in hosts}
    ed = pe.PlanTimelineEditor(_Hub(), seqs)
    ed.set_sequences(seqs)
    ed.set_items(items)
    ed.resize(1400, 700)
    ed.show()
    _app.processEvents()
    return ed


def _two(same_unit=False):
    a = _item("a", "s1", "A", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=300.0)
    b = _item("a" if same_unit else "b", "s2", "B", on=480.0)
    return _editor([a, b])


def _mouse(kind, widget, x, y, button=Qt.MouseButton.LeftButton, buttons=None):
    if buttons is None:
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    gp = widget.mapToGlobal(QPointF(x, y).toPoint())
    return QMouseEvent(kind, QPointF(x, y), QPointF(gp), button, buttons, Qt.KeyboardModifier.NoModifier)


def test_sequences_sit_at_their_resolved_windows_and_steps_align_with_the_plan_axis():
    ed = _two()
    st = ed._stage; na, nb = st.nodes(); eff = st.eff()
    assert na.on_x == pytest.approx(st._on_x)                              # plan on-air + 0
    assert na.off_x == pytest.approx(na.on_x + 300.0 * eff)                # fixed length
    assert nb.on_x == pytest.approx(st._on_x + 480.0 * eff)
    assert nb.off_x == pytest.approx(st._off_x)                            # plan off-air + 0
    # the embedded canvas draws its steps on the PLAN's axis: a's up-ramp starts at a's on-air
    up = next(it for it in na.canvas.items() if tlm._is_ramp(it))
    assert na.canvas._geom[up.uid]["start_x"] == pytest.approx(na.on_x)
    assert na.canvas._geom[up.uid]["stop_x"] == pytest.approx(na.on_x + 120.0 * eff)
    # a's canvas spans the whole stage width at x=0, so its x IS the stage's x
    assert na.canvas.x() == 0 and na.canvas.width() == st.width()


def test_anchoring_a_sequence_edge_to_another_sequence_keeps_it_in_place():
    ed = _two()
    st = ed._stage; na, nb = st.nodes()
    before = nb.on_x
    st.apply_cross_anchor(("seq", nb, "on"), ("seq", na, "off"))
    it = nb.item
    assert (it.on_air_anchor, it.on_air_anchor_item, it.on_air_anchor_edge) == ("item", na.item.id, "off")
    assert it.on_air_offset_s == pytest.approx(180.0)                       # 480 − 300
    assert nb.on_x == pytest.approx(before)
    # the connector is routed by the sequence editor's router: one spec, from a's off edge to b's on edge
    specs = st._connector_specs()
    assert len(specs) == 1 and specs[0]["x2"] == pytest.approx(nb.on_x)
    assert specs[0]["x1"] == pytest.approx(na.off_x) and specs[0]["text"] == "+3:00"
    pts = st._router._connector_points(specs[0]["x1"], specs[0]["y1"], specs[0]["x2"], specs[0]["y2"],
                                       specs[0]["exit_dir"], [], specs[0]["chip_w"] + 24.0, None,
                                       specs[0]["entry_from_right"], two_sided=True)
    assert pts[-1] == (specs[0]["x2"], specs[0]["y2"]) and pts[-2][1] == specs[0]["y2"]   # horizontal entry
    # detach re-roots it on the plan clock at the instant it resolves to
    st.detach_edge(nb, "on")
    assert nb.item.on_air_anchor == "plan" and nb.item.on_air_offset_s == pytest.approx(480.0)
    assert nb.on_x == pytest.approx(before)


def test_a_step_anchors_to_a_step_in_another_sequence_and_a_loop_is_refused():
    ed = _two()
    st = ed._stage; na, nb = st.nodes(); eff = st.eff()
    up_a = next(it for it in na.canvas.items() if tlm._is_ramp(it))
    tune_b = next(it for it in nb.canvas.items() if getattr(it, "action", "") == "tune")
    x_before = nb.canvas._geom[tune_b.uid]["cx"]
    st.apply_cross_anchor(("step", nb.canvas, tune_b, "start"), ("step", na.canvas, up_a, "end"))
    assert tune_b.anchor == "step" and tune_b.anchor_item == na.item.id
    assert tune_b.anchor_step_id == up_a.step_id and tune_b.anchor_edge == "end"
    assert nb.canvas._geom[tune_b.uid]["cx"] == pytest.approx(x_before, abs=eff)   # snapped to 1 s
    assert tlm.is_cross_item(tune_b) and tune_b.uid in nb.canvas._ext_bases
    assert nb.editor.validate() is None
    # the wire carries the cross-item reference; a save keeps it
    wire = [s for s in ed.items()[1].steps if s.anchor == "step"]
    assert wire and wire[0].anchor_item == na.item.id and wire[0].anchor_step_id == up_a.step_id
    # a's on-air hung off THAT tune would loop (a → b's tune → a's ramp → a) → refused, unchanged
    st.apply_cross_anchor(("seq", na, "on"), ("step", nb.canvas, tune_b, "start"))
    assert na.item.on_air_anchor == "plan"
    # a step's cross anchor detaches in place through the canvas's own detach
    nb.canvas._detach_anchor(tune_b.uid)
    assert tune_b.anchor == "start" and tune_b.anchor_item == ""
    assert nb.canvas._geom[tune_b.uid]["cx"] == pytest.approx(x_before, abs=eff)


def test_external_target_finds_step_edges_and_sequence_lines_across_canvases():
    ed = _two()
    st = ed._stage; na, nb = st.nodes()
    up_a = next(it for it in na.canvas.items() if tlm._is_ramp(it))
    g = na.canvas._geom[up_a.uid]
    gp = na.canvas.mapToGlobal(QPointF(g["stop_x"], g["y"] + pe.LANE_H / 2).toPoint())
    hit = st.external_target(gp, exclude_canvas=nb.canvas)
    assert hit[0] == "step" and hit[1] is na.canvas and hit[2] is up_a and hit[3] == "end"
    # b's own on-air guide line, anywhere down its group, is b's on-air edge
    gp2 = nb.canvas.mapToGlobal(QPointF(nb.on_x, 10.0).toPoint())
    assert st.external_target(gp2, exclude_canvas=na.canvas) == ("seq", nb, "on")
    # and a sequence handle
    hx, hy = st.seq_edge_point(na, "off")
    assert st.external_target(st.mapToGlobal(QPointF(hx, hy).toPoint()))[:3] == ("seq", na, "off")


def test_dragging_a_collapsed_sequence_shifts_both_edges_and_clamps_at_the_plan_anchors():
    ed = _editor([_item("a", "s1", "A", on=60.0, off=-60.0, expanded=False)])
    st = ed._stage; n = st.nodes()[0]; eff = st.eff()
    y = n.row_y + 20
    st.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, st, n.on_x + 30, y))
    st.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, st, n.on_x + 30 + 30 * eff, y, Qt.MouseButton.NoButton))
    st.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, st, n.on_x + 30 + 30 * eff, y))
    assert n.item.on_air_offset_s == pytest.approx(90.0) and n.item.off_air_offset_s == pytest.approx(-30.0)
    # dragging further right than the plan's off-air allows is clamped
    st.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, st, n.on_x + 30, y))
    st.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, st, n.on_x + 30 + 500 * eff, y, Qt.MouseButton.NoButton))
    st.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, st, n.on_x + 30 + 500 * eff, y))
    assert n.item.off_air_offset_s == pytest.approx(0.0) and n.item.on_air_offset_s == pytest.approx(120.0)


def test_channel_conflict_only_between_overlapping_sequences_on_one_unit():
    ed = _two(same_unit=True)                 # both on unit a: A's stop tail overlaps B? no — check
    st = ed._stage; na, nb = st.nodes()
    assert st.conflicts() == set()            # A ends at 300 s, B starts at 480 s
    nb.item.on_air_offset_s = 100.0           # B now starts inside A's window
    st.relayout()
    assert st.conflicts() == {str(na.uid), str(nb.uid)}
    assert "Channel conflict" in ed._ready.text()
    ed2 = _two(same_unit=False)
    ed2._stage.nodes()[1].item.on_air_offset_s = 100.0
    ed2._stage.relayout()
    assert ed2._stage.conflicts() == set()    # different units: no shared channel


def test_folding_a_unit_packs_overlapping_sequences_into_lanes():
    ed = _two(same_unit=True)
    st = ed._stage
    st.nodes()[1].item.on_air_offset_s = 100.0
    st.toggle_folded("a")
    lane_rows = [r for r in st.rows() if r["type"] == "ulane"]
    assert lane_rows and lane_rows[0]["lanes"] == 2
    assert not any(n.canvas.isVisible() for n in st.nodes())
    st.toggle_folded("a")
    assert not any(r["type"] == "ulane" for r in st.rows())


def test_a_dangling_reference_is_healed_in_place_when_its_target_is_removed():
    ed = _two()
    st = ed._stage; na, nb = st.nodes()
    st.apply_cross_anchor(("seq", nb, "on"), ("seq", na, "off"))
    x_before = nb.on_x
    st.remove_node(na)
    assert nb.item.on_air_anchor == "plan" and nb.item.on_air_offset_s == pytest.approx(480.0)
    assert nb.on_x == pytest.approx(x_before)
    assert st.first_fault() is None


def test_items_round_trip_anchors_expansion_and_steps_and_legacy_items_seed_from_the_library():
    a = _item("a", "s1", "A", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=300.0,
              recovery_policy="auto", recovery_mode="replay", expanded=False)
    legacy = m.PlanItem(hostname="b", sequence_id="s1", steps=[],
                        overrides=[m.StepOverride(index=0, args=["--power", "-55"], replace_args=True)])
    ed = _editor([a, legacy])
    out = ed.items()
    assert out[0].id == a.id and out[0].off_air_anchor == "item" and out[0].off_air_offset_s == 300.0
    assert out[0].expanded is False and out[0].recovery_policy == "auto"
    assert len(out[0].steps) == len(_steps())
    assert out[1].id.startswith(pg.ITEM_ID_PREFIX) and out[1].steps          # seeded from the library
    start = next(s for s in out[1].steps if s.action == m.StepAction.START)
    assert start.args == ["--power", "-55"] and out[1].overrides == []      # the override was baked in
    assert ed.validate() is None and not ed.is_empty()


def test_step_tools_act_on_the_selected_sequence():
    ed = _two()
    st = ed._stage; na, nb = st.nodes()
    assert not ed._step_tools[0].isEnabled()
    st.select_node(nb)
    assert ed._step_tools[0].isEnabled() and "B" in ed._hint.text()
    # selecting a step in a canvas selects its sequence (and clears the other canvas's selection)
    up_a = next(it for it in na.canvas.items() if tlm._is_ramp(it))
    na.canvas._select_only(up_a.uid)
    assert st.selected_node() is na and not nb.canvas._selection
    st.select_node(None)
    assert not na.canvas._selection and not ed._step_tools[0].isEnabled()


def _cross_ramp_steps(target_item="pi-s1", offset=200.0):
    """The paint fixture's steps, with the up-ramp hung off another item's `up` ramp END."""
    out = []
    for st in _steps():
        if st.action == m.StepAction.RAMP:
            st = st.model_copy(update=dict(anchor="step", anchor_item=target_item, anchor_step_id="up",
                                           anchor_edge="end", offset_s=offset))
        out.append(st)
    return out


def test_a_connector_under_a_window_spanning_bar_never_loops_back_to_the_anchors_on_air():
    # A (unit a) fixed length; B (unit b) on the plan window, its ramp anchored to A's ramp END +3:20.
    # The rows between hold B's bar, which spans B's WHOLE window (from before the anchor's x): the
    # router must cross it, not push its drop column back past the anchor and wrap via B's on-air.
    a = _item("a", "s1", "A", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=300.0)
    b = _item("b", "s2", "B", steps=_cross_ramp_steps())
    ed = _editor([a, b])
    st = ed._stage; na, nb = st.nodes(); eff = st.eff()
    conns = [c for c in st._routed_connectors() if c["text"] == "+3:20"]
    assert len(conns) == 1
    c = conns[0]; sp = c["spec"]
    assert sp["x1"] == pytest.approx(na.on_x + 120.0 * eff)                    # A's up-ramp end
    assert sp["x2"] == pytest.approx(nb.on_x + 320.0 * eff, abs=1.0)           # B's ramp start
    raw = st._obstacles_between(sp["y1"], sp["y2"])
    assert any(lo <= sp["x1"] <= hi for lo, hi in raw)                         # B's bar spans the anchor x
    # with the raw obstacles the sequence editor's router WOULD loop back left of the anchor…
    looped = st._router._connector_points(sp["x1"], sp["y1"], sp["x2"], sp["y2"], sp["exit_dir"], raw,
                                          sp["chip_w"] + 24.0, None, sp["entry_from_right"], two_sided=True)
    assert min(x for x, _ in looped) < sp["x1"] - 20.0
    # …the stage crosses such an obstacle instead: the drawn line never goes left of the anchor
    assert min(x for x, _ in c["pts"]) >= sp["x1"] - 1.0
    assert c["pts"][0] == (sp["x1"], sp["y1"]) and c["pts"][-1] == (sp["x2"], sp["y2"])
    assert c["pts"][-2][1] == sp["y2"]                                          # horizontal entry
    kept = st._route_obstacles(raw, sp["x1"], sp["x2"], False)
    assert all(lo > sp["x1"] for lo, _ in kept)
    # a genuine obstacle strictly between anchor and dependent is still routed around
    assert st._route_obstacles([(sp["x1"] + 60.0, sp["x1"] + 80.0)], sp["x1"], sp["x2"], False) == \
        [(sp["x1"] + 60.0, sp["x1"] + 80.0)]
    # the right-entry mirror: an obstacle reaching the anchor from the left is crossed too
    assert st._route_obstacles([(100.0, 500.0)], 480.0, 300.0, True) == []
    assert st._route_obstacles([(340.0, 380.0)], 480.0, 300.0, True) == [(340.0, 380.0)]


def test_each_sequence_carries_its_own_defined_and_relative_windows():
    ed = _two()
    st = ed._stage; na, nb = st.nodes(); eff = st.eff()
    # A is fixed length: everything of it lives on the on clock → no off-clock content, no relative
    # region (painted all green); its defined on-air region ends at its last on-clock instant (stop +1)
    assert na.bwd_start_x is None
    assert na.def_end_x == pytest.approx(st._on_x + 301.0 * eff)
    # B hangs on the plan's two anchors: its defined on-air region ends at ITS up-ramp end (480 + 120),
    # its defined off-air region begins at ITS first off-clock instant (the rf-off tune at off-air 0)
    assert nb.def_end_x == pytest.approx(st._on_x + 600.0 * eff)
    assert nb.bwd_start_x == pytest.approx(st._off_x)
    # the plan-wide axis still reaches the furthest defined content of ANY sequence
    assert st._def_end >= nb.def_end_x - 1.0
    # the graph agrees, in seconds
    g = pg.PlanGraph([n.graph_item() for n in st.nodes()])
    assert g.item_windows(na.item.id) == (301.0, None)
    assert g.item_windows(nb.item.id) == (600.0, 0.0)


def test_a_cross_wire_drops_clear_of_another_wires_entry_run():
    # A: an up-ramp + its OWN down-ramp hung off the up-ramp END +2:00 (a wire A's canvas draws);
    # B (unit b): its ramp hung off A's up-ramp END +3:20 (a wire the stage draws). The stage wire
    # drops past A's down-ramp row, where the +2:00 wire runs in: its column must not land on that
    # entry run (chip + arrowhead) — the owner's "wires could be routed to avoid collision".
    down = m.SequenceStep(id="down", anchor="step", anchor_step_id="up", anchor_edge="end", offset_s=120,
                          action=m.StepAction.RAMP, task_name="tx",
                          ramp=m.RampSpec(param="power", start=-50, stop=-90, steps=4, duration_s=120))
    a = _item("a", "s1", "A", steps=_steps() + [down],
              off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=450.0)
    b = _item("b", "s2", "B", steps=_cross_ramp_steps())
    ed = _editor([a, b])
    st = ed._stage; na, nb = st.nodes()
    intra = na.canvas._routed_connectors()
    assert len(intra) == 1 and intra[0]["text"] == "+2:00"
    zone_lo, zone_hi = intra[0]["pts"][-2][0] - 6.0, intra[0]["x2"] + 6.0   # the +2:00 entry run
    cross = [c for c in st._routed_connectors() if c["text"] == "+3:20"]
    assert len(cross) == 1
    c = cross[0]; sp = c["spec"]
    # with steps as the only obstacles the router lands its column ON that entry run…
    naive = st._router._connector_points(
        sp["x1"], sp["y1"], sp["x2"], sp["y2"], sp["exit_dir"],
        st._route_obstacles(st._obstacles_between(sp["y1"], sp["y2"]), sp["x1"], sp["x2"], sp["entry_from_right"]),
        sp["chip_w"] + 24.0, None, sp["entry_from_right"], two_sided=True)
    assert zone_lo <= naive[-2][0] <= zone_hi
    # …the stage drops LEFT of it, still right of the anchor (no loop back), entering horizontally
    xd = c["pts"][-2][0]
    assert sp["x1"] < xd < zone_lo - 8.0
    assert c["pts"][-2][1] == sp["y2"] and c["pts"][-1] == (sp["x2"], sp["y2"])
    # every other row the wire crosses is unchanged: it starts at the anchor and never goes left of it
    assert c["pts"][0] == (sp["x1"], sp["y1"]) and min(x for x, _ in c["pts"]) >= sp["x1"] - 1.0


def test_sequence_windows_wash_across_the_frame_and_pill_and_no_row_is_a_grey_stripe():
    a = _item("a", "s1", "A", off_air_anchor="item", off_air_anchor_edge="on", off_air_offset_s=300.0,
              expanded=False)
    b = _item("b", "s2", "B", on=480.0)
    ed = _editor([a, b])
    st = ed._stage; na, nb = st.nodes()
    img = st.grab().toImage()

    def px(x, y):
        col = img.pixelColor(int(x), int(y))
        return col.red(), col.green(), col.blue()

    def neutral(rgb):                 # neither a green nor a red tint (a grey hatch hairline is blue-grey)
        r, g, _b = rgb
        return abs(g - r) <= 4
    # B's expanded frame carries ITS windows: green over its defined on-air region (a row gap inside its
    # canvas), the hatched relative region (neutral grey) before its off-air
    bar = next(it for it in nb.canvas.items() if it.kind == "bar")
    gy = nb.canvas_y + nb.canvas._geom[bar.uid]["y"] + pe.LANE_H + 3
    r, g, _b = px(nb.on_x + 60, gy)
    assert g - r >= 4, (r, g, _b)
    assert neutral(px(nb.off_x - 20, gy))
    # A's collapsed pill (fixed length) is washed green across its body
    w = max(float(pe.PILL_MIN_W), na.off_x - na.on_x)
    r, g, _b = px(na.on_x + w * 0.5, na.row_y + 5 + 21)
    assert g - r >= 4, (r, g, _b)
    # …and NO sequence-level row is a full-width grey stripe (the stage paints its own white ground):
    # B's header row and A's collapsed row are pure white left of any window
    assert px(20, nb.row_y + 4) == (255, 255, 255)
    assert px(20, na.row_y + 20) == (255, 255, 255)
    # while the unit band above keeps a soft (not white) wash at its sticky left edge
    unit = next(r_ for r_ in st._rows if r_["type"] == "unit")
    r, g, b_ = px(st._on_x + 250, unit["y"] + unit["h"] / 2)          # clear of the label + anchor line
    assert (r, g, b_) != (255, 255, 255) and 236 <= r <= 253 and g >= r
