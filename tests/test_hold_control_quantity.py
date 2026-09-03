"""Stage 2 — the HOLD: client precompute of the latest-set control quantity.

`timeline_model.hold_control_quantity(items, resolve)` returns a copy of the timeline with the
calibrated --power INJECTED so a held live density stays constant across a --bw change — the
client-side precompute of the hold the Run/Tune form already does live (keep the displayed quantity
fixed on a --bw change; re-send the recomputed base). It is applied to the DEPLOYED steps
(`TimelineEditor.steps`); the authored canvas stays clean because `set_steps` strips the injected
--power (by its recorded `power_hold_dest`) on load and re-derives it fresh on the next save.

Undeliverable → clamp + warn (base clamped to max; `achievability_warnings` flags it). A non-density
control view (total power / gain / dBm) holds via a constant base, which the runtime already keeps,
so nothing is injected. See docs/sequence-power-achievability.md §10 Stage 2.
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from tests.test_achievability_view_fold import (_ART, _PSD, _FBW, _SPECS, _BASE,  # noqa: F401
                                                _resolve, _bar, _set_bw, _set_power_view, _psd_max)
from tests.test_step_editor_carried_bw import _editor as _carried_editor

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _bar_density(dbm, bw=10):
    return tlm.BarItem(task_name="chirp",
                       args=["--freq", "1575.42", "--power", str(dbm), "--bw", str(bw)],
                       start_offset=0.0, stop_offset=600.0)


def _tune_bw(bw, offset):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"bw": bw})


def _delivered_density(base, bw):
    return base + (-10 * math.log10(bw / 10.0))       # base + view_delta(bw)


# ── the hold keeps a deliverable density constant across a --bw change ───────────────────────────

def test_hold_injects_power_to_keep_the_density_constant():
    # density −20 dBm/MHz at bw 10, later widen to bw 20. Base held would drift to −23; the hold
    # injects base −16.99 into the --bw step so the DELIVERED density stays −20.
    held = tlm.hold_control_quantity([_bar_density(-20), _tune_bw(20, 10.0)], _resolve)
    assert held[0].args == _bar_density(-20).args          # the bar is untouched
    t = held[1]
    assert t.power_hold_dest == "power"                    # marked injected (stripped on load)
    assert t.power_view == "psd_live"                      # in the held view
    assert _delivered_density(t.params["power"], 20) == pytest.approx(-20.0, abs=0.01)


def test_hold_clamps_an_undeliverable_density_and_agrees_with_the_warning():
    # density at the bw-10 MAX (−7.38), widen to bw 20: holding it needs base above max → clamp base
    # to −7.38 (delivered density drops to −10.39). The hold clamps AND the warning flags it — they
    # agree (both computed from the same set-time model).
    items = [_bar_density(-7.38), _tune_bw(20, 10.0)]
    held = tlm.hold_control_quantity(items, _resolve)
    assert held[1].params["power"] == pytest.approx(-7.38, abs=0.01)     # clamped to base max
    issues = tlm.achievability_warnings(items, _resolve)
    assert any(i.direction == "high" for i in issues)                    # and the banner warns


def test_hold_propagates_when_the_upstream_density_changes():
    # Change the bar density −20 → −15; the injected hold on the later --bw step follows (the canvas
    # stays clean, so steps() always re-derives from the current upstream value).
    held = tlm.hold_control_quantity([_bar_density(-15), _tune_bw(20, 10.0)], _resolve)
    assert _delivered_density(held[1].params["power"], 20) == pytest.approx(-15.0, abs=0.01)


def test_hold_is_idempotent_after_a_strip():
    # deploy (inject) → strip (as set_steps does: drop the injected --power) → deploy again yields
    # the same injected value, so re-editing and re-saving is stable.
    held1 = tlm.hold_control_quantity([_bar_density(-20), _tune_bw(20, 10.0)], _resolve)
    v1 = held1[1].params["power"]
    # a re-loaded (stripped) timeline is just the clean --bw tune again
    held2 = tlm.hold_control_quantity([_bar_density(-20), _tune_bw(20, 10.0)], _resolve)
    assert held2[1].params["power"] == pytest.approx(v1)


def test_two_widens_each_hold_the_density():
    held = tlm.hold_control_quantity(
        [_bar_density(-20), _tune_bw(20, 10.0), _tune_bw(40, 20.0)], _resolve)
    assert _delivered_density(held[1].params["power"], 20) == pytest.approx(-20.0, abs=0.01)
    assert _delivered_density(held[2].params["power"], 40) == pytest.approx(-20.0, abs=0.01)


# ── nothing is injected when there's nothing to hold across --bw ─────────────────────────────────

def test_total_power_control_is_not_re_derived_on_a_bw_change():
    # Controlling in TOTAL POWER: base is bandwidth-invariant, so the runtime already holds it — no
    # injection on a --bw change (only a bw-keyed density needs re-derivation).
    held = tlm.hold_control_quantity(
        [_bar(), _set_power_view(-7.38, 5.0, "fbw_power"), _tune_bw(20, 10.0)], _resolve)
    assert held[2].power_hold_dest is None


def test_a_freq_change_does_not_inject():
    # The chirp's density view keys on --bw, not --freq, so a retune moves nothing to hold.
    retune = tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=10.0,
                         params={"freq": 1227.6})
    held = tlm.hold_control_quantity([_bar_density(-20), retune], _resolve)
    assert held[1].power_hold_dest is None


def test_no_control_view_is_a_no_op():
    # A signal with no bw-keyed control view (GPS / a base-quantity signal) injects nothing.
    def _no_view(task):
        info = _resolve(task, view=False)
        return info
    held = tlm.hold_control_quantity([_bar_density(-20), _tune_bw(20, 10.0)], _no_view)
    assert all(getattr(it, "power_hold_dest", None) is None for it in held)


# ── the editor round-trip: steps() injects, set_steps() strips, re-save is stable ────────────────

def test_editor_steps_injects_and_set_steps_strips_and_reinjects():
    ed = _carried_editor([_bar_density(-20), _tune_bw(20, 10.0)])
    steps = ed.steps()
    tune = next(s for s in steps
                if (s.action.value if hasattr(s.action, "value") else s.action) == "tune")
    assert tune.power_hold_dest == "power"                 # injected on deploy
    assert _delivered_density(tune.params["power"], 20) == pytest.approx(-20.0, abs=0.01)

    # Re-load the deployed steps → the authored --bw tune is CLEAN again (no --power on the canvas).
    ed.set_steps(steps)
    canvas_tune = next(it for it in ed.items()
                       if getattr(it, "action", None) == "tune")
    assert "power" not in (canvas_tune.params or {})
    assert getattr(canvas_tune, "power_hold_dest", None) is None

    # Re-save → the hold is re-derived to the same value (stable round-trip).
    steps2 = ed.steps()
    tune2 = next(s for s in steps2
                 if (s.action.value if hasattr(s.action, "value") else s.action) == "tune")
    assert tune2.params["power"] == pytest.approx(tune.params["power"])
