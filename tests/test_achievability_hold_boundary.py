"""Phase 3 (§6.5): achievability across the Hold. The temporal power walk treats the Hold as a
clock-reset boundary — a window-B (anchor="hold") step is placed after every window-A step and
inherits the operating point held at the hold (the up-ramp's final --power + bridge params). So a
window-B density command / down-ramp is validated at the bandwidth CARRIED across the hold, not the
schema default, and the down-ramp's top is checked at its fire-time width.

Reuses the REAL FM-chirp fixtures (verbatim resolver output) from test_achievability_view_fold: the
base density is bandwidth-INVARIANT, so only the controlled-view fold (psd_live on --bw) catches a
clamp — the same machinery, now ordered across the hold.
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui import timeline_model as tlm
from tests.test_achievability_view_fold import (
    _resolve, _bar, _set_bw, _set_power, _psd_max, _density_ramp)


def _hold(offset=120.0):
    return tlm.RunItem(task_name="", action="hold", anchor="start", offset=offset)


def _wb_set_power(dbm, offset=0.0):
    """A window-B density command (anchor="hold") — fires at hold_offset + offset."""
    return tlm.RunItem(task_name="chirp", action="tune", anchor="hold", offset=offset,
                       params={"power": dbm})


def _wb_set_bw(bw, offset=0.0):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="hold", offset=offset,
                       params={"bw": bw})


def _wb_density_ramp(start=-16.24, stop=-7.38, steps=11, hold=12.0, offset=0.0):
    r = _density_ramp(start=start, stop=stop, steps=steps, hold=hold, offset=offset)
    r.anchor = "hold"
    return r


# ── a density held across the hold is re-checked by a window-B change ─────────

def test_window_a_density_held_across_the_hold_clamps_on_a_window_b_widen():
    # Window A sets the density to the bw-10 max (−7.38 dBm/MHz), which is held through the hold.
    # A window-B tune then widens the sweep to 20 MHz, where that held density is undeliverable
    # (max ≈ −10.39). The walk must ORDER the window-B widen after the window-A set (so it re-checks
    # the standing density) — the whole point of the hold boundary.
    items = [_bar(), _set_power(-7.38, 5.0), _hold(120.0), _wb_set_bw(20, 0.0)]
    issues = tlm.achievability_warnings(items, _resolve)
    assert len(issues) == 1
    assert issues[0].direction == "high"
    assert issues[0].bound == pytest.approx(_psd_max(20), abs=0.02)     # ≈ −10.39 dBm/MHz
    assert issues[0].unit == "dBm/MHz"
    assert "held" in issues[0].message


def test_window_b_widen_leaves_an_in_range_held_density_silent():
    # The held density (−15) is comfortably below the bw-20 max, so widening in window B is fine.
    items = [_bar(), _set_power(-15.0, 5.0), _hold(120.0), _wb_set_bw(20, 0.0)]
    assert tlm.achievability_warnings(items, _resolve) == []


def test_directly_set_window_b_density_folds_at_the_carried_width():
    # A window-B tune that COMMANDS --power at the width carried across the hold (20 MHz set in
    # window A) folds its base into a deliverable density (base −15 → −18 dBm/MHz at bw 20) — no
    # false positive, and the command is expressed at the carried width, not the schema default.
    items = [_bar(), _set_bw(20, 5.0), _hold(120.0), _wb_set_power(-15.0, 0.0)]
    assert tlm.achievability_warnings(items, _resolve) == []


def test_the_hold_orders_window_b_after_window_a():
    # The crux of the boundary: a window-B widen (anchored to the hold) is applied AFTER the
    # window-A density set, so it re-checks the held density and flags it. Were the same widen a
    # window-A step BEFORE the set, the set would fold at the wide width and be deliverable → silent.
    # Only the hold anchoring — which orders the widen last — changes the outcome.
    after = tlm.achievability_warnings(
        [_bar(), _set_power(-7.38, 5.0), _hold(120.0), _wb_set_bw(20, 0.0)], _resolve)
    before = tlm.achievability_warnings(
        [_bar(), _set_bw(20, 1.0), _set_power(-7.38, 5.0), _hold(120.0)], _resolve)
    assert len(after) == 1 and before == []


# ── a window-B down-ramp is validated at its fire-time width ─────────────────

def test_window_b_density_ramp_clamps_after_a_window_b_widen():
    # A window-B density down-ramp (anchored to the hold) with a later window-B tune widening the
    # sweep mid-ramp: the top points can't hold their intended density at the wider width and clamp,
    # while the lower ones stay in range — the same temporal check as in window A, ordered past the
    # hold.
    ramp = _wb_density_ramp(start=-16.24, stop=-7.38, steps=11, hold=12.0, offset=0.0)
    widen = _wb_set_bw(20, 36.0)                     # +36 s into window B (hold_offset + 36)
    issues = tlm.achievability_warnings([_bar(), _hold(120.0), ramp, widen], _resolve)
    assert len(issues) == 1
    iss = issues[0]
    assert iss.direction == "high"
    assert iss.bound == pytest.approx(_psd_max(20), abs=0.02)
    levels = [v for (_i, v, _t) in iss.points]
    assert max(levels) == pytest.approx(-7.38, abs=0.02)               # the top is flagged
    assert min(levels) > _psd_max(20) - 1.0                            # the low points are not
    assert "clamped down" in iss.message and "ramp" in iss.message


def test_window_b_density_ramp_silent_when_deliverable():
    # No widen: every point of the window-B ramp is deliverable at the width carried across the hold
    # (bw 10 from the baseline), so nothing is flagged.
    ramp = _wb_density_ramp(start=-16.24, stop=-7.38, steps=11, hold=12.0, offset=0.0)
    assert tlm.achievability_warnings([_bar(), _hold(120.0), ramp], _resolve) == []


# ── the deploy-time hold precompute also re-derives across the boundary ──────

def test_hold_control_quantity_injects_a_window_b_bw_change():
    # A density (−20 dBm/MHz at bw 10) held through the hold, then a window-B tune widens to 20 MHz:
    # the deploy-time precompute must re-derive the base INTO that window-B step (base −16.99) so the
    # delivered density stays −20 — i.e. it processes the hold-anchored --bw change in the right order.
    bar = tlm.BarItem(task_name="chirp",
                      args=["--freq", "1575.42", "--power", "-20", "--bw", "10"],
                      start_offset=0.0, stop_offset=600.0)
    wb_bw = tlm.RunItem(task_name="chirp", action="tune", anchor="hold", offset=0.0,
                        params={"bw": 20})
    held = tlm.hold_control_quantity([bar, _hold(120.0), wb_bw], _resolve)
    injected = [it for it in held if getattr(it, "power_hold_dest", None) == "power"]
    assert len(injected) == 1 and injected[0].anchor == "hold"
    base = injected[0].params["power"]
    delivered = base + (-10 * math.log10(20 / 10.0))       # base + view_delta(bw 20)
    assert delivered == pytest.approx(-20.0, abs=0.01)


def test_hold_marker_alone_does_not_perturb_a_normal_walk():
    # Dropping a Hold into an otherwise-normal timeline (no window-B steps) leaves the window-A
    # warnings byte-identical — the taskless marker contributes no events.
    with_hold = tlm.achievability_warnings(
        [_bar(), _set_power(-7.38, 5.0), _hold(300.0), _set_bw(20, 10.0)], _resolve)
    without = tlm.achievability_warnings(
        [_bar(), _set_power(-7.38, 5.0), _set_bw(20, 10.0)], _resolve)
    assert len(with_hold) == len(without) == 1
    assert with_hold[0].bound == pytest.approx(without[0].bound, abs=1e-9)
