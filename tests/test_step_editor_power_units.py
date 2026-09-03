"""Surface C — the multi-quantity --power CARD in the sequence STEP editor (run/tune steps).

The step editor's parameter form now offers the same power card the Run and live-tune forms do:
an "ALSO READS AS" companion read-out per other quantity (each promotable with "Control in this
→"), a DEPENDS ON row, and finest-step rounding. The card is gated purely on two set_params
kwargs the step editor now passes — the script's CAL_POWER_LAWS, and (for a TUNE step) the full
schema plus its non-live dests as fold CONTEXT, seeded from the CARRIED sequence state — so a
companion / ceiling folds through the operating point in effect when the step fires, and re-folds
when the step is moved (its carried state depends on which earlier steps precede it).

Client-only; no agent/scripts change. See docs/sequence-power-achievability.md §5 Surface C.
"""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from ui import timeline_model as tlm
from ui.timeline_editor import StepEditorDialog, TimelineEditor
# Reuse the chirp density↔total laws (drift-free: imported, not re-declared).
from tests.test_param_form_power_units import FBW, PSD, _artifact, _total_reported

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    """Drain Qt's queued work after each test. A StepEditorDialog built here re-renders on
    load / on a re-fold, which reparents the old widget frames and queues them for deleteLater;
    left pending, that queue fires during a LATER module's processEvents and aborts on a stale
    widget (a headless-Qt SIGABRT). Same pattern as test_live_tune_power."""
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()


# ── Fixture: a chirp whose --power is controlled in TOTAL power (dBm) with the spectral density
# (dBm/MHz) as a companion. The density law keys on --bw, so the density companion tracks the
# sweep bandwidth. --bw and --freq are NON-live (fixed per run) → fold context in a tune step. ──
# base = total power (dBm); the spectral-density companion (PSD law) tracks --bw. The measured
# artifact reports through the FBW law; ``quantity`` is a display name (overridden for a clean
# base label — the fold math reads operating_unit / curves / readings, never the name).
_ART = {**_artifact(_total_reported()), "quantity": "Total power"}
_SIGNAL = {
    "min_power_dbm": -16.76, "max_power_dbm": -6.71, "quantity": "spectral density",
    "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": _ART,
}
_SPECS = [
    {"dest": "freq", "flags": ["--freq"], "type": "float", "step": 0.01, "unit": "MHz",
     "default": 1575.42, "is_freq": True},                              # non-live → fold context
    {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
     "unit": "dBm", "snap_role": "power", "default": -6.71, "live": True},
    {"dest": "bw", "flags": ["--bw"], "type": "float", "step": 0.1, "unit": "MHz",
     "default": 20.0, "min": 0.001, "max": 55.0},           # non-live → fold context, keyed by PSD
]
_CMD = ["python3", "chirp.py", "--freq", "1575.42", "--power", "-6.71", "--bw", "10"]


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.fleet = type("F", (), {"get": lambda self_, h: None})()

    def run_async(self, label, fn):                          # never reached — the cache is seeded
        try:
            res = fn()
        except Exception as exc:                             # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _editor(items):
    ed = TimelineEditor()
    ed.set_context(FakeHub(), "unit")
    ed.set_task_commands({"chirp": list(_CMD)})
    ed.set_task_signals({"chirp": "chirp"})
    # Seed the caches a step/ramp dialog would normally populate from get_script_params.
    ed._param_specs = {"chirp.py": _SPECS}
    ed._script_cal_freq_params = {"chirp.py": "freq"}
    ed._script_power_laws = {"chirp.py": [FBW, PSD]}
    ed._script_cal_signals = {"chirp.py": "chirp"}
    ed._cal_hostname = "unit"
    ed._calibration = {"unit_type": "broadcaster", "valid": True,
                       "signals": {"chirp": _SIGNAL}}
    ed._canvas.set_items(items)
    return ed


def _bar(bw=10, stop=600.0):
    return tlm.BarItem(task_name="chirp",
                       args=["--freq", "1575.42", "--power", "-6.71", "--bw", str(bw)],
                       start_offset=0.0, stop_offset=stop)


def _run_reset(bw, offset):
    """A one-shot that resets the task args (replace_args) — used only to carry a different --bw
    forward past a given offset, so moving the edited step across it changes its carried state."""
    return tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=offset,
                       args=["--freq", "1575.42", "--power", "-6.71", "--bw", str(bw)])


def _tune(offset=50.0, power=-6.71):
    return tlm.RunItem(task_name="chirp", action="tune", anchor="start", offset=offset,
                       params={"power": power})


def _run_step(power=-6.71, bw=10):
    return tlm.RunItem(task_name="chirp", action="run", anchor="start", offset=50.0,
                       args=["--freq", "1575.42", "--power", str(power), "--bw", str(bw)])


def _dialog(item, items):
    return StepEditorDialog(item, _editor(items), new=False)


def _companion_cards(dlg):
    return [c for c in dlg._form.findChildren(QFrame) if c.objectName() == "pwrCompanionCard"]


def _companion_value(dlg, name):
    for c in _companion_cards(dlg):
        if c.findChild(QLabel, "pwrCompanionName").text() == name:
            return c.findChild(QLabel, "pwrCompanionValue").text().replace("−", "-")
    return None


def _dep_values(dlg):
    return [l.text() for l in dlg._form.findChildren(QLabel) if l.objectName() == "depValue"]


# ── the card renders in both step types ───────────────────────────────────────────────────────

def test_run_step_offers_power_companions():
    dlg = _dialog(_run_step(), [_bar(), _run_step()])
    cards = _companion_cards(dlg)
    # a run/bar step renders the FULL schema → freq/power/bw all editable, density companion shown.
    assert set(dlg._form._widgets) == {"freq", "power", "bw"}
    assert [c.findChild(QLabel, "pwrCompanionName").text() for c in cards] == ["Spectral density"]
    names = {v["name"] for v in dlg._form._power_views()}
    assert "Total power" in names and "Spectral density" in names


def test_tune_step_offers_power_companions_only_live_editable():
    tune = _tune()
    dlg = _dialog(tune, [_bar(), tune])
    # only the live knob (--power) is editable; --freq/--bw are fold CONTEXT (present, not rendered).
    assert set(dlg._form._widgets) == {"power"}
    assert set(dlg._form._context_dests) == {"freq", "bw"}
    assert len(_companion_cards(dlg)) == 1
    assert dlg._form._selected_view()["name"] == "Total power"   # base axis, live-edited


# ── a companion / DEPENDS ON tracks a CARRIED (non-live) bridge param ───────────────────────────

def test_tune_step_companion_tracks_the_carried_bandwidth():
    # The density companion folds through --bw, which is CARRIED (a fixed param the earlier bar set),
    # not a field on this step. So a task started at bw 10 vs bw 40 gives a different density read-
    # out (density = total − 10 − 10·log10(bw/10)), and DEPENDS ON names the carried bandwidth.
    tune = _tune()
    d10 = _dialog(tune, [_bar(bw=10), tune])
    assert "10" in _dep_values(d10)                          # DEPENDS ON shows the carried --bw
    v10 = _companion_value(d10, "Spectral density")

    tune2 = _tune()
    d40 = _dialog(tune2, [_bar(bw=40), tune2])
    assert "40" in _dep_values(d40)
    v40 = _companion_value(d40, "Spectral density")

    # density at total −6.71: bw 10 → −16.7 ; bw 40 → −16.71 − 10·log10(4) ≈ −22.7 dBm/MHz.
    assert v10 is not None and v40 is not None and v10 != v40
    assert float(v10) == pytest.approx(-16.71, abs=0.1)
    assert float(v40) == pytest.approx(-16.71 - 10 * math.log10(4.0), abs=0.1)


def test_moving_the_offset_refolds_the_card():
    # The edited tune step's carried --bw depends on WHERE it sits: a one-shot at 100 s resets the
    # task to bw 40. Before it (offset 50) the step carries bw 10; after it (offset 150) bw 40.
    # Moving the offset must re-fold the card through the new carried state (set_fold_context).
    tune = _tune(offset=50.0)
    dlg = _dialog(tune, [_bar(bw=10), _run_reset(bw=40, offset=100.0), tune])
    assert "10" in _dep_values(dlg)
    before = _companion_value(dlg, "Spectral density")

    dlg._run_off.setValue(150.0)                             # drag the step past the 100 s reset
    _app.processEvents()
    assert "40" in _dep_values(dlg)                          # carried --bw re-derived to 40
    after = _companion_value(dlg, "Spectral density")
    assert after != before
    assert float(after) == pytest.approx(-16.71 - 10 * math.log10(4.0), abs=0.1)


# ── "Control in this →" promotes a companion to the primary quantity ────────────────────────────

def test_control_in_promotes_the_companion_in_a_tune_step():
    tune = _tune()
    dlg = _dialog(tune, [_bar(), tune])
    assert dlg._form._selected_view()["name"] == "Total power"
    btn = next(b for b in dlg._form.findChildren(QPushButton)
               if b.objectName() == "pwrControlIn")
    btn.click()
    _app.processEvents()
    assert dlg._form._selected_view()["name"] == "Spectral density"


# ── save paths send --power in the BASE quantity and never leak a context dest ──────────────────

def test_tune_save_emits_base_quantity_power_without_context_leakage():
    # Controlling in the density companion, then saving, must still send --power in the BASE (total)
    # quantity — and the tune params must contain ONLY the ticked live knob, never a fold-context
    # dest (--freq / --bw). values() is exactly what _accept stores as the step's params.
    tune = _tune(power=-6.71)
    dlg = _dialog(tune, [_bar(bw=10), tune])
    # promote to density and set a density value in the primary field.
    next(b for b in dlg._form.findChildren(QPushButton)
         if b.objectName() == "pwrControlIn").click()
    _app.processEvents()
    dlg._form._widgets["power"][0].setValue(-16.71)          # −16.71 dBm/MHz at bw 10
    params = dlg._form.values()
    assert set(params) == {"power"}                          # no freq / bw leaked in
    # −16.71 dBm/MHz density at bw 10 → −6.71 dBm total (the base quantity actually sent).
    assert params["power"] == pytest.approx(-6.71, abs=0.05)


def test_a_ramp_warmed_cache_still_yields_the_power_card():
    # Regression (owner-reported): the param cache is populated ONLY through
    # TimelineEditor.cache_script_meta (used by BOTH the step and ramp editors), so a cache warmed
    # by a ramp editor still carries the script's power laws. A step editor opened AFTER a ramp
    # editor then finds the cache warm and STILL renders the multi-quantity card — previously the
    # ramp editor populated only a subset, so the step card silently vanished.
    ed = TimelineEditor()
    ed.set_context(FakeHub(), "unit")
    ed.set_task_commands({"chirp": list(_CMD)})
    ed.set_task_signals({"chirp": "chirp"})
    ed._cal_hostname = "unit"
    ed._calibration = {"unit_type": "broadcaster", "valid": True, "signals": {"chirp": _SIGNAL}}
    # One fetch (whichever dialog does it) populates EVERY per-script cache atomically.
    ed.cache_script_meta("chirp.py", {
        "params": _SPECS, "calibration_signal": "chirp", "calibration_freq_param": "freq",
        "calibration_power_laws": [FBW, PSD]})
    assert ed._script_power_laws["chirp.py"] == [FBW, PSD]
    assert set(ed.param_cache()) == {"chirp.py"}                      # cache is warm

    tune = _tune()
    ed._canvas.set_items([_bar(), tune])
    dlg = StepEditorDialog(tune, ed, new=False)                      # finds the cache warm
    assert len(_companion_cards(dlg)) == 1                           # card still renders (was 0)


def test_run_save_emits_no_context_dests():
    # A run/bar step renders the full schema (no context dests), so build_args carries the real
    # fields and nothing spurious. The offset subtraction still lands --power in the base quantity.
    run = _run_step()
    dlg = _dialog(run, [_bar(), run])
    args = dlg._form.build_args()
    flags = [a for a in args if a.startswith("--")]
    assert set(flags) == {"--freq", "--power", "--bw"}       # exactly the schema's own flags
