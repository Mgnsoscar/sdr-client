"""LiveTuneDialog reflects the unit's resolved --power range while retuning a
running task (reads SDR_CAL_SIGNAL_ID, fetches /calibration, bounds the field)."""
import math
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QFrame, QPushButton

from api.client import AgentHTTPError
from ui.live_tune_dialog import LiveTuneDialog
from tests.test_param_form_power_units import (
    FBW, PSD, _artifact, _density_reported,
)

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _flush_deferred_deletes():
    """Drain Qt's queued work after each test. A LiveTuneDialog built here re-renders on load /
    on a re-fold, which reparents the old widget frames and queues them for deleteLater; left
    pending, that queue fires during a LATER test module's processEvents and aborts on a
    now-stale widget (a headless-Qt SIGABRT). Flushing the DeferredDelete queue here keeps each
    test's Qt teardown contained."""
    yield
    _app.processEvents()
    _app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    _app.processEvents()

YAML = (
    "tasks:\n"
    "  - name: mocktask\n"
    "    command: [python3, mock_tx.py, --power, \"-30\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock }\n"
)
PARAMS = {"params": [
    {"dest": "power", "flags": ["-Power", "--power"], "type": "float", "live": True,
     "unit": "dBm", "min": -140.0, "max": 60.0, "default": -20.0, "help": "power"},
]}
CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "antenna_eirp", "quantity": "EIRP",
    "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}


class FakeClient:
    def __init__(self, cal=CAL, snapshot=None):
        self._cal = cal
        self._snapshot = snapshot if snapshot is not None else {"current": {}, "applied": {}}

    def get_tasks_yaml(self):
        return YAML

    def get_script_params(self, name):
        return PARAMS

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def get_task_params(self, name):
        return self._snapshot


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, client):
        super().__init__()
        self.fleet = type("F", (), {"get": lambda self_, h: client})()

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _power_spec(dlg):
    return dlg._form._widgets["power"][1]


def test_livetune_bounds_power_when_calibrated():
    dlg = LiveTuneDialog(FakeHub(FakeClient()), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-1.8, 28.2)
    assert sp["unit"] == "dBm EIRP"


def test_livetune_keeps_schema_when_uncalibrated():
    dlg = LiveTuneDialog(FakeHub(FakeClient(cal=AgentHTTPError("u", 404, "none"))), "u", "mocktask")
    sp = _power_spec(dlg)
    assert (sp["min"], sp["max"]) == (-140.0, 60.0)


# ── seeding the running task's current --power ──────────────────────────────────────

_ACTIVE_ARTIFACT = {
    "curve": [[0.0, -40.0], [40.0, 0.0]], "min_gain_db": 0.0, "max_gain_db": 40.0,
    "gain_step_db": 1.0,
    "active_components": [{"plane": "atten_out", "task": "atten_set", "param": "attenuation",
                           "sense": "attenuation", "min_db": 0.0, "max_db": 95.0,
                           "step_db": 0.25, "engage_pct": 0.0, "baseline_delta_by_freq": []}],
}
_ACTIVE_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
    "operating_plane": "atten_out", "quantity": "EIRP",
    "min_power_dbm": -135.0, "max_power_dbm": 0.0, "artifact": _ACTIVE_ARTIFACT}}}


def test_livetune_active_seeds_requested_power_not_the_gain_derived_value():
    # On an active chain the script reports --power from its SDR gain alone (attenuator
    # omitted), so `applied` reads high. The dialog must seed the accepted request (`current`),
    # or opening Tune would stage a power tens of dB above what's actually set.
    client = FakeClient(cal=_ACTIVE_CAL,
                        snapshot={"current": {"power": -100.0}, "applied": {"power": -80.0}})
    dlg = LiveTuneDialog(FakeHub(client), "u", "mocktask")
    assert dlg._form.values()["power"] == pytest.approx(-100.0)


def test_livetune_nonactive_seeds_the_applied_power():
    # With no active components `applied` is the real (gain-quantised) power, so it still wins.
    cal = {"unit_type": "broadcaster", "valid": True, "signals": {"mock": {
        "operating_plane": "antenna_eirp", "quantity": "EIRP",
        "min_power_dbm": -1.8, "max_power_dbm": 28.2}}}
    client = FakeClient(cal=cal, snapshot={"current": {"power": 5.0}, "applied": {"power": 10.0}})
    dlg = LiveTuneDialog(FakeHub(client), "u", "mocktask")
    assert dlg._form.values()["power"] == pytest.approx(10.0)


# ── the redesigned multi-quantity power card is offered in Tune, not just Run ─────────
# A signal that declares CAL_POWER_LAWS gets the same power card in the live-tune form as in
# the run form: an "ALSO READS AS" companion read-out per other quantity, each with a
# "Control in this →" switch. Regression: the dialog used to omit power_laws from set_params,
# so the companions/switch never rendered while retuning.

_CHIRP_YAML = (
    "tasks:\n"
    "  - name: chirp\n"
    "    command: [python3, fm_chirp_tx.py, --power, \"-20\", --bw, \"20\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: fm_chirp }\n"
)
# freq/power/bw are all live — a chirp retunes its sweep bandwidth live, so the density↔total
# companions track --bw exactly as in the run form.
_CHIRP_PARAMS = {
    "calibration_freq_param": "freq",
    "calibration_power_laws": [FBW, PSD],
    "params": [
        {"dest": "freq", "flags": ["--freq"], "type": "float", "step": 0.01, "unit": "MHz",
         "default": 1575.42, "is_freq": True, "live": True},
        {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
         "unit": "dBm/MHz", "snap_role": "power", "default": -20.0, "live": True},
        {"dest": "bw", "flags": ["--bw"], "type": "float", "step": 0.1, "unit": "MHz",
         "default": 20.0, "min": 0.001, "max": 55.0, "live": True},
    ],
}
_CHIRP_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"fm_chirp": {
    "min_power_dbm": -26.76, "max_power_dbm": -16.71, "quantity": "spectral density",
    "operating_plane": "sdr_output", "amplitude": 0.5,
    "artifact": _artifact(_density_reported())}}}


class ChirpClient(FakeClient):
    def get_tasks_yaml(self):
        return _CHIRP_YAML

    def get_script_params(self, name):
        return _CHIRP_PARAMS


def _companion_cards(dlg):
    return [c for c in dlg._form.findChildren(QFrame)
            if c.objectName() == "pwrCompanionCard"]


def test_livetune_offers_power_companions_for_declared_laws():
    dlg = LiveTuneDialog(FakeHub(ChirpClient(cal=_CHIRP_CAL)), "u", "chirp")
    cards = _companion_cards(dlg)
    btns = [b for b in dlg._form.findChildren(QPushButton)
            if b.objectName() == "pwrControlIn"]
    # controlled in the measured density → the full-bandwidth-power quantity is a companion.
    assert len(cards) == 1
    assert len(btns) == len(cards)                     # each companion promotable to primary
    names = {v["name"] for v in dlg._form._power_views()}
    assert "Full-bandwidth (total) power" in names
    assert "spectral density" in names


def test_livetune_control_in_switches_the_power_quantity():
    dlg = LiveTuneDialog(FakeHub(ChirpClient(cal=_CHIRP_CAL)), "u", "chirp")
    assert dlg._form._selected_view()["name"] == "spectral density"   # base (measured) axis
    btn = next(b for b in dlg._form.findChildren(QPushButton)
               if b.objectName() == "pwrControlIn")
    btn.click()
    assert dlg._form._selected_view()["name"] == "Full-bandwidth (total) power"


# ── live tune folds against fixed context: GPS C/A sidelobes drive the power limits ──────
# A limiting reading can key on a DERIVED quantity (GPS C/A's enbw — a table lookup on the live
# --sidelobes) while the operator's real knob (--sidelobes) is live and the carrier (--freq) is
# fixed per run. The tune form renders ONLY the live knobs but keeps the full schema as fold
# context, so the --power ceiling re-folds through the limiting reading as --sidelobes is retuned,
# and folds at the DEPLOYED --freq. Regression: the form used to pass only live params, dropping
# the derived enbw and the fixed --freq, so the range folded at a representative value and never
# moved with --sidelobes.

_ENBW = [9.235883, 9.717879, 9.886379]        # enbw_mhz(0), (1), (2)
# Full in-band power (dBm) from the measured density, keyed on enbw: 10·log10(enbw / enbw0).
_FULL = {"id": "full", "name": "Full in-band power", "in": "density", "out": "abs",
         "param": "enbw_mhz", "coeff": 10.0, "ref": _ENBW[0], "k": 0.0, "rep": _ENBW[2]}
_GPS_YAML = (
    "tasks:\n"
    "  - name: gps\n"
    "    command: [python3, gps_ca_code_10.23Mcps.py, --freq, \"1227.6\", --prn, \"5\","
    " --power, \"-40\", --sidelobes, \"2\"]\n"
    "    env: { SDR_CAL_SIGNAL_ID: gpsca }\n"
)
_GPS_PARAMS = {
    "calibration_freq_param": "freq",
    "calibration_power_laws": [_FULL],
    "params": [
        {"dest": "power", "flags": ["--power"], "type": "float", "step": 0.01,
         "unit": "dBm", "snap_role": "power", "default": -40.0, "live": True},
        {"dest": "gain", "flags": ["--gain"], "type": "float", "min": 0, "max": 76,
         "default": 60, "unit": "dB", "live": True},
        {"dest": "freq", "flags": ["--freq"], "type": "float", "unit": "MHz",
         "min": 1000, "max": 1800, "default": 1575.42, "is_freq": True},        # fixed per run
        {"dest": "prn", "flags": ["--prn"], "type": "int", "min": 1, "max": 32,
         "default": 1, "required": True},                                       # fixed per run
        {"dest": "sidelobes", "flags": ["--sidelobes"], "type": "int", "min": 0, "max": 2,
         "step": 1, "default": 1, "live": True},
        {"dest": "enbw_mhz", "kind": "derived", "name": "enbw_mhz", "unit": "MHz",
         "hidden": True, "formula": {"table": ["sidelobes", *_ENBW]}},          # law's key
    ],
}
_GPS_ART = {
    "schema_version": 1, "signal_id": "gpsca", "operating_plane": "sdr_output",
    "quantity": "spectral density", "amplitude": 0.5, "min_gain_db": 40.0, "max_gain_db": 80.0,
    "min_power_dbm": -120.0, "max_power_dbm": -10.0,
    "curve": {"interp": "linear", "points": [[40, -120.0], [80, -10.0]]},
    "operating_unit": "dBm/MHz", "anchor_curve": [[40, -120.0], [80, -10.0]], "passive_hops": [],
    "readings": {"reported": {"kind": "same"},
                 "limiting": {"kind": "law", "unit": "dBm", "law": _FULL, "max_dbm": -18.0},
                 "reported_delta_db": 0.0, "limiting_delta_db": 0.0},
}
_GPS_CAL = {"unit_type": "broadcaster", "valid": True, "signals": {"gpsca": {
    "min_power_dbm": -120.0, "max_power_dbm": -10.0, "quantity": "spectral density",
    "operating_plane": "sdr_output", "amplitude": 0.5, "artifact": _GPS_ART}}}


class GpsClient(FakeClient):
    def get_tasks_yaml(self):
        return _GPS_YAML

    def get_script_params(self, name):
        return _GPS_PARAMS

    def get_task_params(self, name):
        return {"current": {"power": -40.0, "sidelobes": 2}, "applied": {}}


def test_livetune_renders_only_live_knobs_but_keeps_fold_context():
    dlg = LiveTuneDialog(FakeHub(GpsClient(cal=_GPS_CAL)), "u", "gps")
    # editable fields are the live knobs only (--gain dropped by absolute mode); the fixed carrier
    # / PRN and the derived enbw are fold context, present in the schema but not rendered.
    assert set(dlg._form._widgets) == {"power", "sidelobes"}
    assert set(dlg._context_dests) == {"freq", "prn", "enbw_mhz"}
    assert dlg._deployed_freq == pytest.approx(1227.6)          # parsed from the deployed command


def test_livetune_power_ceiling_tracks_sidelobes():
    dlg = LiveTuneDialog(FakeHub(GpsClient(cal=_GPS_CAL)), "u", "gps")
    f = dlg._form

    def ceiling_at(n):
        f._widgets["sidelobes"][0].setValue(n)
        f._on_freq_changed()                    # the debounced re-fold, driven synchronously
        return f._widgets["power"][1]["max"]

    got = {n: ceiling_at(n) for n in (0, 1, 2)}
    exp = {n: -18.0 - 10 * math.log10(_ENBW[n] / _ENBW[0]) for n in (0, 1, 2)}
    for n in (0, 1, 2):
        assert got[n] == pytest.approx(exp[n], abs=0.02)
    # more sidelobes pass more of the signal → more in-band power for a given density → a LOWER
    # density ceiling. The ceiling actually MOVES with the live knob (the whole point).
    assert got[0] > got[1] > got[2]
