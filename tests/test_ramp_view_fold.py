"""Stage 3 — the ramp editor authors --power in the CONTROLLED view (a chirp's live spectral
density), like the Run/Tune power card (restates_measurement replaces the measured quantity).

The ramp's From/To range is the LIVE density at the carried sweep bandwidth (not the bw-frozen
measured base): the base range is shifted into the view by view_delta(carried_bw) and relabelled,
the bounded field snaps in base but displays the view (BoundedNumberField.view_offset), the stored
ramp start/stop are BASE (offset removed on save, added back on load), and the ramp records its
control view so the achievability walk / hold treat it as that quantity.

Fixtures: the REAL chirp structure + editor from tests/test_step_editor_carried_bw.py.
See docs/sequence-power-achievability.md §10 Stage 3.
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

from ui import timeline_model as tlm
from ui.param_form import BoundedNumberField
from ui.ramp_editor import RampEditorDialog
from tests.test_step_editor_carried_bw import _editor as _chirp_editor, _bar, _set_bw

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


def _psd_max(bw):
    return -7.38 - 10 * math.log10(bw / 10.0)             # base_max + view_delta(bw)


def _ramp_dlg(items, offset=10.0):
    ed = _chirp_editor(items)
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=offset, ramp={})
    dlg = RampEditorDialog(src, ed, new=True)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    return dlg


def test_ramp_range_is_the_live_density_at_the_carried_bandwidth():
    # bar bw 10, a tune widens to 20 before the ramp → the ramp authors density at bw 20.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    spec = dlg._ramped_spec()
    assert spec.get("unit") == "dBm/MHz"
    assert spec.get("max") == pytest.approx(_psd_max(20), abs=0.06)     # ≈ −10.39, not −7.38


def test_ramp_range_tracks_the_carried_bandwidth():
    # bw 10 vs bw 20 → the density ceiling drops ≈ 3 dB (the same fold the step editor applies).
    d10 = _ramp_dlg([_bar(10)])
    d20 = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert d10._ramped_spec().get("max") == pytest.approx(_psd_max(10), abs=0.06)   # ≈ −7.38
    assert d20._ramped_spec().get("max") == pytest.approx(_psd_max(20), abs=0.06)   # ≈ −10.39
    assert d10._ramped_spec().get("max") > d20._ramped_spec().get("max") + 2.5


def test_from_field_displays_the_view_but_snaps_in_base():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    f = dlg._start_field
    assert isinstance(f, BoundedNumberField)
    assert f._view_off == pytest.approx(-10 * math.log10(2), abs=0.02)   # view_delta(20) ≈ −3.01
    assert f._spin.maximum() == pytest.approx(_psd_max(20), abs=0.06)    # view range on the spinbox


def test_ramp_seed_shows_the_saved_base_in_the_view_on_load():
    # A saved ramp stores BASE start/stop; reopened at carried bw 20 the fields show the live density
    # (base + view_delta(20)). base stop −11.99 → density −15.
    ed = _chirp_editor([_bar(10), _set_bw(20, 5.0)])
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=10.0,
                      ramp={"start": -30.0, "stop": -11.99, "step": 1.0, "hold_s": 5.0},
                      power_view="psd_live")
    ed._canvas.set_items([_bar(10), _set_bw(20, 5.0), src])
    dlg = RampEditorDialog(src, ed, new=False)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    assert dlg._val(dlg._stop_field) == pytest.approx(-11.99 + (-10 * math.log10(2)), abs=0.06)  # ≈ −15


def test_ramp_save_stores_base_and_records_the_view():
    # Author density From −25 → −18 at carried bw 20; the STORED ramp is base (density − view_delta),
    # and power_view records the controlled view so the walk/hold treat it as live density.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._accept()
    assert dlg.result_item is not None
    assert dlg.result_item.power_view == "psd_live"
    off = -10 * math.log10(2)                              # view_delta(20) ≈ −3.01
    assert dlg.result_item.ramp["start"] == pytest.approx(-25.0 - off, abs=0.06)   # base ≈ −21.99
    assert dlg.result_item.ramp["stop"] == pytest.approx(-18.0 - off, abs=0.06)    # base ≈ −14.99


def test_save_then_load_round_trips_the_density():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._accept()
    saved = dlg.result_item

    ed = _chirp_editor([_bar(10), _set_bw(20, 5.0), saved])
    dlg2 = RampEditorDialog(saved, ed, new=False)
    dlg2._param.setCurrentText("power")
    _app.processEvents()
    # the reopened From/To show the SAME densities the operator authored
    assert dlg2._val(dlg2._start_field) == pytest.approx(-25.0, abs=0.06)
    assert dlg2._val(dlg2._stop_field) == pytest.approx(-18.0, abs=0.06)


# ── Issue 2: the power card offers the OTHER quantities (dBm, dBm/Hz), not just density ──────────
# The ramp editor previously locked --power to the leading restates_measurement view (live density);
# the operator could not author the ramp in total power or dBm/Hz. It now renders the multi-quantity
# power card (the ramp analogue of the Run/Tune card): a RAMPING IN primary + an ALSO READS AS grid
# of companion tiles, each with a "Ramp in this →" switch. The card is backed by a hidden picker
# (_power_unit) that carries the view id list + the view-conversion wiring.

def _total(base):     # full-bandwidth total power = base + view_delta(fbw) = base + 10 dB (bw-invariant)
    return base + 10.0


def test_power_card_lists_every_view_and_defaults_to_density():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert dlg._card_active                                       # the card renders for calibrated --power
    ids = [dlg._power_unit.itemData(i) for i in range(dlg._power_unit.count())]
    assert ids == ["psd_live", "fbw_power", "psd_hz"]            # the chirp's CAL_POWER_LAWS, base dropped
    assert dlg._power_view == "psd_live"                         # the leading restatement is the default
    labels = [dlg._power_unit.itemText(i) for i in range(dlg._power_unit.count())]
    assert "dBm/MHz" in labels[0] and "dBm" == labels[1].split("[")[-1].rstrip("] ") and "dBm/Hz" in labels[2]
    # the OTHER two quantities each get a companion tile with a "Ramp in this →" button
    comp_ids = [v["id"] for v, _f, _t in dlg._companion_labels]
    assert comp_ids == ["fbw_power", "psd_hz"]


def test_power_card_hidden_for_a_non_power_param():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._param.setCurrentText("bw")
    _app.processEvents()
    assert not dlg._card_active                                  # a non-power ramp has no quantity card
    assert not dlg._companion_labels                             # …and no companion tiles
    dlg._param.setCurrentText("power")
    _app.processEvents()
    assert dlg._card_active                                      # ...and it returns when --power is swept


def test_switching_to_total_power_relabels_the_range_and_converts_the_values():
    # Author density −25 → −18 at bw 20, then switch the picker to Full-bandwidth (total) power.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    idx = next(i for i in range(dlg._power_unit.count()) if dlg._power_unit.itemData(i) == "fbw_power")
    dlg._power_unit.setCurrentIndex(idx)
    _app.processEvents()
    assert dlg._power_view == "fbw_power"
    spec = dlg._ramped_spec()
    assert spec.get("unit") == "dBm"                             # relabelled to total-power dBm
    assert spec.get("max") == pytest.approx(_total(-7.38), abs=0.06)   # base max −7.38 → +2.62 dBm
    # the SAME physical power, re-expressed: density −25 at bw 20 is base −21.99 → total −11.99 dBm.
    off = -10 * math.log10(2)                                    # view_delta(psd_live, 20) ≈ −3.01
    assert dlg._val(dlg._start_field) == pytest.approx(_total(-25.0 - off), abs=0.06)   # ≈ −11.99
    assert dlg._val(dlg._stop_field) == pytest.approx(_total(-18.0 - off), abs=0.06)    # ≈ −4.99


def test_saving_in_a_switched_view_stores_base_and_records_that_view():
    # Whatever quantity the operator authors in, the STORED ramp is base and power_view records the
    # chosen view — so switching density → total power doesn't change the base the unit is commanded.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._mode.setCurrentIndex(dlg._mode.findData("step_hold"))
    dlg._step.setText("1"); dlg._hold.setText("10")
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    idx = next(i for i in range(dlg._power_unit.count()) if dlg._power_unit.itemData(i) == "fbw_power")
    dlg._power_unit.setCurrentIndex(idx)
    _app.processEvents()
    dlg._accept()
    off = -10 * math.log10(2)
    assert dlg.result_item.power_view == "fbw_power"
    # base is unchanged by the view switch (density −25/−18 at bw 20 → base −21.99/−14.99).
    assert dlg.result_item.ramp["start"] == pytest.approx(-25.0 - off, abs=0.06)
    assert dlg.result_item.ramp["stop"] == pytest.approx(-18.0 - off, abs=0.06)


def test_step_listing_shares_the_form_scroll_and_expands():
    # The per-step listing must live in the SAME scroll as the rest of the form — not its own tiny
    # fixed-height box with an inner scrollbar (where you see ~one line at a time). It is a QLabel
    # (expands to its full content, no own scrollbar) sitting inside the outer QScrollArea whose body
    # also holds the From/To fields, so a long ramp shows every step and the whole dialog scrolls.
    from PyQt6.QtWidgets import QLabel, QScrollArea, QWidget
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert isinstance(dlg._steps_view, QLabel)                     # not a fixed-height text box
    body = next((s.widget() for s in dlg.findChildren(QScrollArea)
                 if s.widget() is not None
                 and dlg._steps_view in s.widget().findChildren(QLabel)), None)
    assert body is not None                                        # the listing is inside a scroll…
    assert dlg._start_box in body.findChildren(QWidget)            # …the SAME one as the From/To fields


def test_reopening_a_total_power_ramp_selects_that_view():
    # A ramp saved in the total-power view reopens with the picker on total power, showing dBm.
    ed = _chirp_editor([_bar(10), _set_bw(20, 5.0)])
    src = tlm.RunItem(task_name="chirp", action="ramp", anchor="start", offset=10.0,
                      ramp={"start": -21.99, "stop": -14.99, "step": 1.0, "hold_s": 5.0},
                      power_view="fbw_power")
    ed._canvas.set_items([_bar(10), _set_bw(20, 5.0), src])
    dlg = RampEditorDialog(src, ed, new=False)
    dlg._param.setCurrentText("power")
    _app.processEvents()
    assert dlg._power_unit.currentData() == "fbw_power"
    assert dlg._ramped_spec().get("unit") == "dBm"
    # base stop −14.99 → total power −4.99 dBm.
    assert dlg._val(dlg._stop_field) == pytest.approx(_total(-14.99), abs=0.06)


# ── The multi-quantity power card: companion From → To read-outs + "Ramp in this →" ──────────────

def _num(lbl_text):
    """A companion/span label reads back a formatted number with a unicode minus — parse it."""
    return float(str(lbl_text).replace("−", "-").split()[0])


def test_companion_tiles_show_each_quantity_from_to_live():
    # With --power swept in live density (default), the total-power companion shows the SAME sweep
    # re-expressed: density −25 → −18 at bw 20 is total power −11.99 → −4.99 dBm.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    tiles = {v["id"]: (f, t) for v, f, t in dlg._companion_labels}
    assert set(tiles) == {"fbw_power", "psd_hz"}                 # the two quantities not being swept
    off = -10 * math.log10(2)                                    # psd_live view_delta(20) ≈ −3.01
    ffrom, fto = tiles["fbw_power"]
    assert _num(ffrom.text()) == pytest.approx(_total(-25.0 - off), abs=0.06)   # ≈ −11.99
    assert _num(fto.text()) == pytest.approx(_total(-18.0 - off), abs=0.06)     # ≈ −4.99


def test_ramp_in_this_button_promotes_a_companion_to_primary():
    # "Ramp in this →" on the total-power tile makes it the swept quantity, re-expressing the same
    # physical sweep (density −25/−18 → total −11.99/−4.99 dBm) and demoting density to a companion.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._start_field.setValue(-25.0)
    dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    dlg._set_ramp_power_view("fbw_power")                        # the companion button's action
    _app.processEvents()
    assert dlg._power_view == "fbw_power"
    assert dlg._ramped_spec().get("unit") == "dBm"
    off = -10 * math.log10(2)
    assert dlg._val(dlg._start_field) == pytest.approx(_total(-25.0 - off), abs=0.06)   # ≈ −11.99
    # density is now one of the companions, not the primary
    assert "psd_live" in [v["id"] for v, _f, _t in dlg._companion_labels]


def test_span_readout_reports_the_sweep_direction():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._start_field.setValue(-30.0); dlg._stop_field.setValue(-18.0)
    _app.processEvents()
    assert "rising" in dlg._span_lbl.text()                      # From < To
    dlg._start_field.setValue(-18.0); dlg._stop_field.setValue(-30.0)
    _app.processEvents()
    assert "falling" in dlg._span_lbl.text()                     # From > To


def test_plain_from_to_rows_for_a_non_power_param():
    # A non-power ramp keeps the plain From/To rows — no card, no companion tiles, no span read-out.
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    dlg._param.setCurrentText("bw")
    _app.processEvents()
    assert not dlg._card_active
    assert dlg._span_lbl is None
    assert dlg._companion_labels == []
    assert dlg._start_field is not None and dlg._stop_field is not None   # still editable


# ── One shared dual-handle rail (the mockup's single power slider with two handles) ──────────────

def test_card_uses_one_shared_dual_rail_not_per_field_rails():
    from ui.param_widgets import DualRangeRail
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert isinstance(dlg._pwr_rail, DualRangeRail)         # one dual-handle rail for both endpoints
    assert dlg._start_field._rail is None                   # the fields drop their own single rails
    assert dlg._stop_field._rail is None
    # a non-power param falls back to per-field rails (no shared dual rail)
    dlg._param.setCurrentText("bw")
    _app.processEvents()
    assert dlg._pwr_rail is None
    assert dlg._start_field._rail is not None


def test_dual_rail_drag_snaps_into_the_field_and_clamps():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    lo, hi = dlg._start_field.bounds()
    dlg._on_rail_drag("to", -20.0)                          # a drag on the To handle
    _app.processEvents()
    assert dlg._val(dlg._stop_field) == pytest.approx(-20.0, abs=0.06)   # snapped to the level
    assert dlg._pwr_rail._to == pytest.approx(dlg._val(dlg._stop_field), abs=1e-6)  # handle re-synced
    dlg._on_rail_drag("from", 999.0)                        # a drag past MAX clamps to it
    _app.processEvents()
    assert dlg._val(dlg._start_field) == pytest.approx(hi, abs=0.06)


def test_min_max_labels_reflect_the_field_bounds():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    lo, hi = dlg._start_field.bounds()
    assert "MIN" in dlg._pwr_min.text() and "MAX" in dlg._pwr_max.text()
    # the numbers shown are the achievable bounds (density at the carried bw)
    assert _num(dlg._pwr_min.text().replace("MIN", "")) == pytest.approx(lo, abs=0.06)
    assert _num(dlg._pwr_max.text().replace("MAX", "")) == pytest.approx(hi, abs=0.06)


# ── From/To render as the mockup's .p-input, and their sub-labels follow the fire times ──────────

def test_from_to_fields_render_as_pinput_in_the_card():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)])
    assert dlg._start_field._pinput and dlg._stop_field._pinput      # the mockup's .p-input
    # the ▲/▼ steppers route through the spinbox's achievable-level stepping
    dlg._start_field.setValue(-30.0); _app.processEvents()
    before = dlg._start_field.value()
    dlg._start_field._spin.stepUp(); _app.processEvents()
    assert dlg._start_field.value() > before                         # stepped to the next level


def test_from_to_sublabels_follow_the_anchor_and_offset():
    dlg = _ramp_dlg([_bar(10), _set_bw(20, 5.0)], offset=10.0)       # anchor start, +10 s
    _app.processEvents()
    assert "on-air +10" in dlg._ft_from_lbl.text() and "ramp end" in dlg._ft_to_lbl.text()
    # switch to the off-air anchor: FROM is the ramp start, TO is held to off-air − offset
    dlg._anchor.setCurrentIndex(dlg._anchor.findData("stop"))
    dlg._offset.setValue(-5.0)
    _app.processEvents()
    assert "ramp start" in dlg._ft_from_lbl.text() and "off-air −5" in dlg._ft_to_lbl.text()
