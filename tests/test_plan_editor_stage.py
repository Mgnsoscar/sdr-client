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
