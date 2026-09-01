"""Offscreen widget tests for the CalibrationPanel: the resolved summary, the
not-calibrated / invalid states, agent-rejection handling, and the Editor⇄JSON form
model (round-trip, curve-grid edits, add/remove signal). A fake hub runs the async
calls synchronously and emits task_done."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

from api.client import AgentHTTPError
from ui.calibration_panel import CalibrationPanel, _fmt_range

_app = QApplication.instance() or QApplication([])


class FakeClient:
    def __init__(self, cal=None, upload=None, caps=(), validate=None):
        self._cal = cal
        self._upload = upload or {"saved": "calibration.json", "calibration": {}}
        self._caps = list(caps)
        self._validate = validate
        self.uploaded = []
        self.validated = []
        self.components_uploaded = []

    def upload_components(self, content):
        self.components_uploaded.append(content)
        return {"saved": "components.yaml"}

    def get_components(self):
        return ""

    def get_calibration(self):
        if isinstance(self._cal, Exception):
            raise self._cal
        return self._cal

    def upload_file(self, name, content):
        if isinstance(self._upload, Exception):
            raise self._upload
        self.uploaded.append((name, content))
        return self._upload

    def supports(self, cap):
        return cap in self._caps

    def validate_calibration(self, document):
        self.validated.append(document)
        if isinstance(self._validate, Exception):
            raise self._validate
        return self._validate


class FakeFleet:
    def __init__(self, client):
        self._c = client

    def get(self, host):
        return self._c


class FakeHub(QObject):
    task_done = pyqtSignal(str, object)

    def __init__(self, client):
        super().__init__()
        self.fleet = FakeFleet(client)

    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def _doc():
    return {
        "schema_version": 1, "unit_id": "u1", "unit_type": "broadcaster",
        "chain": {
            "gain_limits": {"min_gain_db": 0.0, "max_gain_db": 89.75},
            "operating_plane": "sdr_output",
            "limits": [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}],
            "planes": {"sdr_output": {"type": "measured", "quantity": "total in-band power"}},
        },
        "defaults": {"amplitude": 0.5},
        "signals": {"mock": {"curves": {
            "sdr_output": {"points": [{"gain_db": 40, "power_dbm": -36},
                                      {"gain_db": 74, "power_dbm": -2.5}]}}}},
    }


# ── display states ───────────────────────────────────────────────────────────────

def test_fmt_range():
    assert _fmt_range(0.0, 74.0, "dB") == "0 – 74 dB"
    assert _fmt_range(None, 1, "dB") == "—"


def test_renders_calibrated_summary():
    cal = {"unit_type": "broadcaster", "valid": True, "document": _doc(),
           "signals": {"mock": {"operating_plane": "sdr_output", "quantity": "total in-band power",
                                 "min_gain_db": 0.0, "max_gain_db": 74.0,
                                 "min_power_dbm": -36.0, "max_power_dbm": -2.5}}}
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "mock"
    assert "calibrated" in p._status.text()
    assert p._download_btn.isEnabled()


def test_not_calibrated_hint():
    p = CalibrationPanel("u", FakeHub(FakeClient(
        cal=AgentHTTPError("u", 404, "No calibration document for this unit"))))
    p.on_shown()
    assert p._table.rowCount() == 0
    assert "not calibrated" in p._status.text()
    assert not p._download_btn.isEnabled()


def test_outdated_agent_on_get_is_flagged():
    # A generic "Not Found" 404 (route absent) ⇒ the deployed agent predates the
    # calibration endpoints — surface an update prompt, not a bare "not calibrated".
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=AgentHTTPError("u", 404, "Not Found"))))
    p.on_shown()
    assert "out of date" in p._status.text()
    assert p._table.rowCount() == 0


def test_outdated_agent_on_save_is_flagged(monkeypatch):
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("msg", a)))
    client = FakeClient(upload=AgentHTTPError("u", 404, "Not Found"))
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    p._on_save()
    assert "out of date" in p._status.text()
    assert "msg" in seen                                # a dialog explained the update


def test_invalid_stored_document():
    cal = {"unit_type": "broadcaster", "valid": False, "document": _doc(),
           "error": "curve not invertible"}
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    assert "INVALID" in p._status.text()


def test_resolved_power_ranges_survive_navigation():
    # The resolved --power column must not revert to "validate to resolve" on an unrelated
    # interaction (navigation re-reads the form) — only after a value is actually edited.
    cal = {"unit_type": "broadcaster", "valid": True, "document": _doc(),
           "signals": {"mock": {"operating_plane": "sdr_output",
                                 "quantity": "total in-band power",
                                 "min_gain_db": 0.0, "max_gain_db": 74.0,
                                 "min_power_dbm": -36.0, "max_power_dbm": -2.5}}}
    p = CalibrationPanel("u", FakeHub(FakeClient(cal=cal)))
    p.on_shown()
    resolved = p._table.item(0, 2).text()
    assert resolved not in ("validate to resolve", "", "—")

    p._select_plane("sdr_output")                    # navigation: re-reads the form
    assert p._table.item(0, 2).text() == resolved    # ranges preserved

    p._f["max_gain"].setText("50")                   # an actual edit
    p._select_plane("sdr_output")
    assert p._table.item(0, 2).text() == "validate to resolve"


# ── save paths ───────────────────────────────────────────────────────────────────

def test_save_after_json_rejection_surfaces_reason(monkeypatch):
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("msg", a)))
    client = FakeClient(upload=AgentHTTPError("u", 400, "curve not invertible"))
    p = CalibrationPanel("u", FakeHub(client))
    import json
    assert p._apply_json_text(json.dumps(_doc())) is None   # applied into the form
    p._on_save()
    assert "rejected" in p._status.text()
    assert client.uploaded == []
    assert "curve not invertible" in seen["msg"][2]


def test_apply_invalid_json_is_local_guard():
    # Invalid JSON pasted into the JSON dialog is rejected without touching the model.
    client = FakeClient()
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    before = p._read_form(strict=False)
    msg = p._apply_json_text("{ not json ")
    assert msg and "not valid JSON" in msg
    assert p._read_form(strict=False) == before             # model untouched


def test_save_from_form_serializes_document():
    client = FakeClient()
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())                                  # populates the form
    p._on_save()
    assert len(client.uploaded) == 1
    import json
    name, content = client.uploaded[0]
    sent = json.loads(content)
    assert sent["chain"]["operating_plane"] == "sdr_output"
    assert sent["signals"]["mock"]["curves"]["sdr_output"]["points"][0]["gain_db"] == 40


# ── form model ───────────────────────────────────────────────────────────────────

def test_form_round_trips_through_widgets():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    out = p._read_form(strict=True)
    assert out["chain"]["gain_limits"] == {"min_gain_db": 0.0, "max_gain_db": 89.75}
    assert out["chain"]["operating_plane"] == "sdr_output"
    assert out["chain"]["limits"] == [{"plane": "sdr_output", "max_dbm": -2.5, "reason": "amp P1dB"}]
    # amplitude is fixed fleet-wide: recorded on the chain default, not per signal
    assert out["defaults"]["amplitude"] == 0.5
    assert "amplitude" not in out["signals"]["mock"]
    pts = out["signals"]["mock"]["curves"]["sdr_output"]["points"]
    assert [(pt["gain_db"], pt["power_dbm"]) for pt in pts] == [(40.0, -36.0), (74.0, -2.5)]
    # plane topology is preserved from the model even though the form doesn't edit it
    assert out["chain"]["planes"]["sdr_output"]["type"] == "measured"


def test_curve_grid_edit_is_read_back():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.add_blank_row()
    r = tbl.rowCount() - 1
    tbl.item(r, 0).setText("60")
    tbl.item(r, 1).setText("-16")
    pts = p._read_form(strict=True)["signals"]["mock"]["curves"]["sdr_output"]["points"]
    assert {"gain_db": 60.0, "power_dbm": -16.0} in pts


def test_curve_grid_remove_without_selection_drops_last_row():
    # "− point" must remove something even when no whole row is selected (the common
    # case right after typing into cells), rather than silently doing nothing.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.clearSelection()
    tbl.setCurrentCell(-1, -1)
    before = tbl.rowCount()
    tbl.remove_selected()
    assert tbl.rowCount() == before - 1


def test_read_form_preserves_unmodeled_signal_fields():
    # The form doesn't model every signal/curve field (the JSON tab is the source of
    # truth for those). Editing in the Editor tab and reading back must not drop them.
    d = _doc()
    d["signals"]["mock"]["note"] = "keep me"                      # signal-level extra
    d["signals"]["mock"]["curves"]["sdr_output"]["interp"] = "pchip"  # curve-level extra
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    out = p._read_form(strict=True)["signals"]["mock"]
    assert out["note"] == "keep me"
    assert out["curves"]["sdr_output"]["interp"] == "pchip"


def test_bad_curve_cell_blocks_save_strictly():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.add_blank_row()
    tbl.item(tbl.rowCount() - 1, 0).setText("not-a-number")
    with pytest.raises(ValueError):
        p._read_form(strict=True)


def test_add_and_remove_signal(monkeypatch):
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("gps_l1", True)))
    p._on_add_signal()
    assert "gps_l1" in p._f["signals"]
    p._remove_signal("gps_l1")
    assert "gps_l1" not in p._f["signals"]


def _full_doc():
    d = _doc()
    d["chain"]["operating_plane"] = "antenna_eirp"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "total in-band power"},
        "amplifier_output": {"type": "measured", "quantity": "main-lobe power"},
        "cable_output": {"type": "derived", "from": "amplifier_output", "delta_db": -1.8},
        "antenna_eirp": {"type": "derived", "from": "cable_output", "delta_db": 6.0, "quantity": "EIRP"},
    }
    d["signals"]["mock"]["curves"]["amplifier_output"] = {
        "points": [{"gain_db": 40, "power_dbm": -6}, {"gain_db": 74, "power_dbm": 24}]}
    return d


def test_plane_topology_round_trips_through_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_full_doc())
    out = p._read_form(strict=True)["chain"]["planes"]
    assert out["sdr_output"] == {"type": "measured", "quantity": "total in-band power"}
    assert out["cable_output"] == {"type": "derived", "from": "amplifier_output", "delta_db": -1.8}
    assert out["antenna_eirp"]["type"] == "derived"
    assert out["antenna_eirp"]["from"] == "cable_output"
    assert out["antenna_eirp"]["quantity"] == "EIRP"
    # measured planes drive which curve grids exist per signal
    assert set(p._f["signals"]["mock"]["curves"]) == {"sdr_output", "amplifier_output"}


def test_add_and_remove_plane(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())                                   # one plane: sdr_output
    p._on_add_plane()
    planes = p._read_form(strict=False)["chain"]["planes"]
    assert len(planes) == 2 and "plane" in planes
    # remove the added one via its row
    row = next(r for r in p._f["planes"] if r["name"].text() == "plane")
    p._remove_plane(row)
    assert list(p._read_form(strict=False)["chain"]["planes"]) == ["sdr_output"]


def test_derived_plane_without_delta_blocks_save():
    # A constant Δ dB stage with no value entered can't be saved strictly.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    doc = _doc()
    doc["chain"]["planes"]["pad"] = {"type": "derived", "from": "sdr_output"}
    p._set_doc(doc)
    row = next(r for r in p._f["planes"] if r["name"].text() == "pad")
    assert row["role"] == "constant"
    row["delta"].setText("")
    with pytest.raises(ValueError):
        p._read_form(strict=True)


def test_template_seeds_empty_unit():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._on_new_template()
    assert p._doc is not None
    assert p._download_btn.isEnabled()
    assert "mock" in p._f["signals"]


# ── new-document unit_type comes from the unit, not a hardcoded 'broadcaster' ──────

class _TypedClient(FakeClient):
    def __init__(self, unit_type, unit_id):
        super().__init__()
        self.unit_type = unit_type
        self.unit_id = unit_id


def test_template_uses_units_real_type_and_id():
    p = CalibrationPanel("u", FakeHub(_TypedClient("x410", "unit_x410_7")))
    p._on_new_template()
    assert p._doc["unit_type"] == "x410"
    assert p._doc["unit_id"] == "unit_x410_7"


# ── renaming a plane propagates to everything that references it ───────────────────

def test_rename_plane_updates_all_references():
    from ui.calibration_panel import _rename_plane_in_doc
    d = _full_doc()                                  # antenna_eirp ← cable_output ← amplifier_output
    out = _rename_plane_in_doc(d, "amplifier_output", "amp_out")
    assert "amp_out" in out["chain"]["planes"]
    assert "amplifier_output" not in out["chain"]["planes"]
    # derived plane's parent pointer follows the rename
    assert out["chain"]["planes"]["cable_output"]["from"] == "amp_out"
    # the signal's curve keyed by the plane follows too
    assert "amp_out" in out["signals"]["mock"]["curves"]
    assert "amplifier_output" not in out["signals"]["mock"]["curves"]


def test_rename_operating_and_limit_plane_follow():
    from ui.calibration_panel import _rename_plane_in_doc
    d = _doc()                                       # operating + limit both on sdr_output
    out = _rename_plane_in_doc(d, "sdr_output", "sdr_port")
    assert out["chain"]["operating_plane"] == "sdr_port"
    assert out["chain"]["limits"][0]["plane"] == "sdr_port"
    assert "sdr_port" in out["signals"]["mock"]["curves"]


def test_rename_in_form_keeps_document_valid_shape():
    # End-to-end through the widget handler: rename the single plane and confirm the
    # operating plane + curve key follow, so the read-back doc has no dangling refs.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    row = p._f["planes"][0]
    assert row["orig"] == "sdr_output"
    row["name"].setText("sdr_port")
    p._on_plane_name_changed(row)
    out = p._read_form(strict=False)
    assert list(out["chain"]["planes"]) == ["sdr_port"]
    assert out["chain"]["operating_plane"] == "sdr_port"
    assert "sdr_port" in out["signals"]["mock"]["curves"]


def test_removing_plane_purges_its_references(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    d = _full_doc()
    # give amplifier_output a limit too, so we can check the limit purge on removal
    d["chain"]["limits"].append({"plane": "amplifier_output", "max_dbm": 24.0})
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    # remove amplifier_output (a non-source measured plane that carries a limit + a curve)
    row = next(r for r in p._f["planes"] if r["name"].text() == "amplifier_output")
    p._remove_plane(row)
    chain = p._doc["chain"]
    assert "amplifier_output" not in chain["planes"]
    assert all(l["plane"] != "amplifier_output" for l in chain["limits"])
    assert "amplifier_output" not in p._doc["signals"]["mock"]["curves"]


def test_source_stage_cannot_be_removed(monkeypatch):
    # The first (source) stage is the chain's measured origin and can't be rebuilt from
    # the editor, so removing it is refused — the plane and its curve stay put.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_full_doc())
    row = next(r for r in p._f["planes"] if r["name"].text() == "sdr_output")
    p._remove_plane(row)
    assert "sdr_output" in p._read_form(strict=False)["chain"]["planes"]


# ── the JSON escape hatch applies valid documents and rejects bad ones ─────────────

def test_apply_json_replaces_document_and_rebuilds_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    import json
    doc = _doc(); doc["signals"]["mock"]["amplitude"] = 0.42
    assert p._apply_json_text(json.dumps(doc)) is None
    out = p._read_form(strict=False)
    assert out["signals"]["mock"]["amplitude"] == 0.42   # editor rebuilt from the JSON


# ── B: local structural checks ────────────────────────────────────────────────────

def test_local_issues_clean_doc_has_none():
    from ui.calibration_panel import local_calibration_issues
    assert local_calibration_issues(_doc()) == []


def test_local_issues_empty_signals_is_clean():
    # An onboarding chain (planes + ceiling set, no signals measured yet) is a valid,
    # savable state — it must not be flagged as an issue.
    from ui.calibration_panel import local_calibration_issues
    d = _doc()
    d["signals"] = {}
    assert local_calibration_issues(d) == []


def test_local_issues_flags_non_invertible_curve():
    from ui.calibration_panel import local_calibration_issues
    d = _doc()
    d["signals"]["mock"]["curves"]["sdr_output"]["points"] = [
        {"gain_db": 40, "power_dbm": -20}, {"gain_db": 50, "power_dbm": -20}]  # flat power
    issues = local_calibration_issues(d)
    assert any("not invertible" in i for i in issues)


def test_local_issues_flags_missing_ceiling_and_unset_operating():
    from ui.calibration_panel import local_calibration_issues
    d = _doc()
    d["chain"]["gain_limits"].pop("max_gain_db")
    d["chain"]["limits"] = []
    d["chain"]["operating_plane"] = ""
    issues = local_calibration_issues(d)
    assert any("safety ceiling" in i for i in issues)
    assert any("operating plane" in i for i in issues)


def test_issues_panel_shows_after_bad_edit():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.item(1, 1).setText("-36")                    # make power flat (40→-36, 74→-36)
    # (isVisible() is unreliable on a never-shown offscreen widget; the label text is
    # cleared when there are no issues, so a non-empty text means the panel is showing.)
    assert "not invertible" in p._issues.text()


# ── C: unit_type + defaults.amplitude round-trip through the Editor ────────────────

def test_unit_type_round_trips_and_amplitude_is_fixed():
    d = _doc()
    d["unit_type"] = "x410"
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    out = p._read_form(strict=True)
    assert out["unit_type"] == "x410"
    # amplitude is not editable — it is normalised to the fixed fleet value on save
    assert out["defaults"]["amplitude"] == 0.5
    # …and there is no amplitude field in the editor
    assert "def_amp" not in p._f
    assert "amp" not in (p._f["signals"].get("mock") or {})


def test_matching_per_signal_amplitude_is_normalised_away():
    # A per-signal amplitude equal to the fixed value is dropped so it inherits the chain
    # default (kept as a single 0.5), rather than restated on every signal.
    from ui.calibration_panel import local_calibration_issues
    d = _doc()
    d["signals"]["mock"]["amplitude"] = 0.5
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    out = p._read_form(strict=True)
    assert "amplitude" not in out["signals"]["mock"]
    assert out["defaults"]["amplitude"] == 0.5
    assert local_calibration_issues(out) == []


def test_legacy_amplitude_is_flagged_not_silently_relabelled():
    # A calibration measured at a different amplitude (legacy) must be FLAGGED and its value
    # PRESERVED on save — never rewritten to 0.5, which would mislabel the old curves and
    # defeat the runtime amplitude gate.
    from ui.calibration_panel import local_calibration_issues
    d = _doc()
    d["defaults"]["amplitude"] = 0.8                      # measured with an older script
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    issues = local_calibration_issues(d)
    assert any("0.8" in i and "re-measure" in i for i in issues)
    out = p._read_form(strict=True)
    assert out["defaults"]["amplitude"] == 0.8           # preserved, not relabelled to 0.5


# ── D: dry-run validate ───────────────────────────────────────────────────────────

def test_validate_button_gated_on_capability():
    p_no = CalibrationPanel("u", FakeHub(FakeClient()))            # no caps
    p_no._set_doc(_doc())
    assert not p_no._validate_btn.isEnabled()
    p_yes = CalibrationPanel("u", FakeHub(FakeClient(caps=("cal-validate",))))
    p_yes._set_doc(_doc())
    assert p_yes._validate_btn.isEnabled()


def test_validate_valid_populates_summary_without_saving():
    client = FakeClient(caps=("cal-validate",), validate={
        "valid": True, "signals": {"mock": {"operating_plane": "sdr_output",
        "quantity": "q", "min_gain_db": 0.0, "max_gain_db": 74.0,
        "min_power_dbm": -36.0, "max_power_dbm": -2.5}}})
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    p._on_validate()
    assert client.validated                              # posted to the agent
    assert client.uploaded == []                         # but NOT stored
    assert p._table.rowCount() == 1
    assert "dry run" in p._status.text()


def test_validate_rejection_shows_reason():
    client = FakeClient(caps=("cal-validate",),
                        validate={"valid": False, "error": "curve not invertible"})
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    p._on_validate()
    assert "REJECTED" in p._status.text()
    assert "curve not invertible" in p._status.text()


def test_validate_without_capability_reports_local_only():
    p = CalibrationPanel("u", FakeHub(FakeClient()))     # no cal-validate
    p._set_doc(_doc())
    p._on_validate()
    assert "no local issues" in p._status.text()         # clean doc, agent can't dry-run


def test_validate_rejects_non_numeric_curve_cell():
    # The bug: Validate parsed the form leniently, silently dropping a bad cell, so it
    # reported valid even though Save (strict) would reject it. Validate must catch it.
    client = FakeClient(caps=("cal-validate",),
                        validate={"valid": True, "signals": {}})
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    tbl = p._f["signals"]["mock"]["curves"]["sdr_output"]
    tbl.add_blank_row()
    r = tbl.rowCount() - 1
    tbl.item(r, 0).setText("oops")                       # non-numeric gain
    tbl.item(r, 1).setText("5")
    p._on_validate()
    assert "invalid" in p._status.text().lower()
    assert "not a number" in p._status.text()
    assert client.validated == []                         # never dry-ran a bad doc


def test_apply_bad_json_leaves_document_and_reports():
    client = FakeClient(caps=("cal-validate",))
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    msg = p._apply_json_text("{ not valid json")
    assert msg and "not valid JSON" in msg
    p._on_validate()                                     # form is still the clean doc
    assert client.validated                              # dry-ran the (unchanged) doc


def test_validate_still_passes_a_clean_doc():
    # Regression: the stricter parse must not reject a genuinely valid document.
    client = FakeClient(caps=("cal-validate",), validate={
        "valid": True, "signals": {"mock": {"operating_plane": "sdr_output",
        "quantity": "q", "min_gain_db": 0.0, "max_gain_db": 74.0,
        "min_power_dbm": -36.0, "max_power_dbm": -2.5}}})
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    p._on_validate()
    assert client.validated                               # dry-ran the clean doc
    assert "dry run" in p._status.text()


# ── F: curve polish (CSV paste, sparkline) ────────────────────────────────────────

def test_csv_paste_adds_points():
    from PyQt6.QtWidgets import QApplication
    from ui.calibration_panel import _CurveTable
    QApplication.clipboard().setText("30, -40\n60,\t-10\n")
    tbl = _CurveTable()
    added = tbl._paste_csv()
    assert added
    pts = tbl.points(strict=True)
    assert {"gain_db": 30.0, "power_dbm": -40.0} in pts
    assert {"gain_db": 60.0, "power_dbm": -10.0} in pts


def test_sparkline_handles_points_and_empty():
    from ui.calibration_panel import _Sparkline
    s = _Sparkline()
    s.set_points([(40, -36), (74, -2.5)])
    assert len(s._pts) == 2
    s.set_points([])                                     # must not raise
    assert s._pts == []


# ── calibration v2: the chain-builder UI (mockup) ─────────────────────────────────

def _v2_doc():
    d = _doc()
    d["chain"]["operating_plane"] = "antenna_eirp"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "total in-band power"},
        "amplifier_output": {"type": "measured", "quantity": "main-lobe"},
        "cable_output": {"type": "derived", "from": "amplifier_output",
                         "component": "cable_lmr240_3m_a"},
        "antenna_eirp": {"type": "derived", "from": "cable_output",
                         "component": "patch_a", "quantity": "EIRP"},
    }
    d["signals"]["mock"]["center_freq_hz"] = 1575.42e6
    return d


def _seed_catalog(p):
    # Isolate the catalog to a throwaway file so tests never touch the repo's
    # components.json (ComponentCatalog.put persists to disk).
    import tempfile
    from pathlib import Path
    from state import ComponentCatalog
    p._catalog = ComponentCatalog(Path(tempfile.mkdtemp()) / "components.json")
    p._catalog.put("cable_lmr240_3m_a", "cable",
                   [[1.10e9, -2.30], [1.40e9, -2.62], [1.60e9, -2.81]], "LMR-240 · 3 m · A")
    p._catalog.put("patch_a", "antenna", [[1.15e9, 5.1], [1.60e9, 6.0]], "Patch A · 6 dBi")


def test_component_derived_plane_passes_local_check():
    # A derived plane whose Δ dB comes from a library component (not an inline delta_db)
    # must NOT be flagged "no Δ dB" — that was a v1-only requirement.
    from ui.calibration_panel import local_calibration_issues
    assert local_calibration_issues(_v2_doc()) == []


def test_chain_renders_a_stage_per_plane():
    from PyQt6.QtWidgets import QFrame
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    # each stage is a slot (card + trailing arrow); count the "stage" cards inside them.
    cards = p._chain_holder.findChildren(QFrame, "stage")
    assert len(cards) == 4                       # one stage per plane
    assert len(p._chain_slots) == 4


def _chain_widgets(p):
    return [p._chain_row.itemAt(i).widget() for i in range(p._chain_row.count())]


def _operating_marker(p):
    return next((w for w in _chain_widgets(p)
                 if w is not None and w.toolTip().startswith("Operating plane")), None)


def test_operating_marker_sits_after_the_last_stage_not_on_a_card():
    # The "--power reads here" callout is a standalone element to the RIGHT of the last
    # stage (so it doesn't travel with a card during a drag), not styling on the card.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    widgets = _chain_widgets(p)
    marker = _operating_marker(p)
    assert marker is not None                              # the callout exists
    # it comes after every stage slot and before the "+ Add stage" tile
    slot_idxs = [widgets.index(s) for _, s in p._chain_slots]
    add = next(w for w in widgets if w is not None and w.objectName() == "addstage")
    assert widgets.index(marker) > max(slot_idxs)
    assert widgets.index(marker) < widgets.index(add)


def test_no_operating_marker_without_stages():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc({"schema_version": 1, "chain": {"planes": {}}, "signals": {}})
    assert _operating_marker(p) is None


def test_selecting_a_stage_updates_selection_and_survives_read():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._select_plane("cable_output")
    assert p._selected_plane == "cable_output"
    # the passive stage's component is still read back from the (re-hosted) picker
    out = p._read_form(strict=True)
    assert out["chain"]["planes"]["cable_output"]["component"] == "cable_lmr240_3m_a"
    assert out["chain"]["planes"]["antenna_eirp"]["component"] == "patch_a"


def test_signals_table_shows_freq_and_range():
    # Amplitude is fixed fleet-wide, so the table no longer carries an Ampl. column:
    # Signal | Freq MHz | --power dBm.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._populate_table({"mock": {"min_power_dbm": -12.4, "max_power_dbm": 28.2}})
    assert p._table.columnCount() == 3
    assert p._table.item(0, 0).text() == "mock"
    assert p._table.item(0, 1).text() == "1575.42"       # centre freq in MHz
    assert "28.2" in p._table.item(0, 2).text()          # resolved --power range


def test_multifreq_signal_shows_at_run():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    d = _v2_doc()
    d["signals"]["mock"].pop("center_freq_hz", None)     # a chirp: many frequencies
    p._set_doc(d)
    p._populate_table({"mock": {}})
    assert p._table.item(0, 1).text() == "at run"
    assert p._table.item(0, 2).text() == "per frequency"


def test_editor_table_lists_signals_before_validate():
    # The signals table is populated straight from the document so signals are
    # clickable without a Validate first; --power reads a placeholder until then.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "mock"
    assert p._table.item(0, 2).text() == "validate to resolve"


def test_clicking_a_signal_opens_its_measured_curve():
    # Clicking a signal selects the measured stage carrying it and expands just that
    # signal in the stage detail (the others stay collapsed).
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._select_plane("cable_output")                      # start on a passive stage
    p._on_signal_row_clicked(0, 0)                       # click the "mock" signal row
    assert p._selected_plane == "sdr_output"             # jumped to a measured stage
    assert p._expanded_signals == {"mock"}


def test_add_signal_suggestions_prefer_tasks_then_cache_and_exclude_existing(cal_cache):
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_v2_doc())                                 # already defines "mock"
    p._task_signal_ids = ["gnss_l1", "mock"]             # a task references mock (defined)
    cal_cache.put("other-unit", {"unit_type": "broadcaster", "valid": True,
                                 "signals": {"gnss_l5": {}, "gnss_l1": {}}})
    sugg = p._suggested_signal_ids()
    assert "mock" not in sugg                             # already in the document → dropped
    assert sugg[0] == "gnss_l1"                           # task-referenced ids come first
    assert "gnss_l5" in sugg                              # cache-only id still offered
    assert sugg.count("gnss_l1") == 1                     # de-duplicated across sources


def test_signals_are_collapsible_in_measured_detail():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._select_plane("sdr_output")                        # measured stage
    assert "mock" not in p._expanded_signals             # collapsed by default
    p._toggle_signal("mock")
    assert p._expanded_signals == {"mock"}
    p._toggle_signal("mock")
    assert "mock" not in p._expanded_signals


def test_rename_signal_preserves_its_curves_and_order():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    d = _v2_doc()
    d["signals"]["other"] = {"amplitude": 0.5, "curves": {}}   # a second signal, after mock
    p._set_doc(d)
    before = p._read_form(strict=False)["signals"]["mock"]
    p._rename_signal("mock", "gnss_l1")
    out = p._read_form(strict=False)["signals"]
    assert "mock" not in out and "gnss_l1" in out
    assert out["gnss_l1"]["curves"] == before["curves"]        # curves carried over intact
    assert list(out.keys()) == ["gnss_l1", "other"]            # insertion order preserved


def test_rename_signal_rejects_a_collision(monkeypatch):
    seen = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: seen.setdefault("msg", a)))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    d = _v2_doc()
    d["signals"]["taken"] = {"amplitude": 0.5, "curves": {}}
    p._set_doc(d)
    p._rename_signal("mock", "taken")                          # collides with an existing id
    out = p._read_form(strict=False)["signals"]
    assert "mock" in out and "taken" in out                    # unchanged
    assert "msg" in seen                                       # and the user was warned


def test_rename_signal_keeps_it_expanded():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._expanded_signals = {"mock"}
    p._rename_signal("mock", "gnss_l1")
    assert p._expanded_signals == {"gnss_l1"}


class _TasksClient(FakeClient):
    """A FakeClient that also serves/updates this unit's tasks.yaml."""
    def __init__(self, tasks_yaml, **kw):
        super().__init__(**kw)
        self._tasks_yaml = tasks_yaml
        self.task_updates = []

    def get_tasks_yaml(self):
        return self._tasks_yaml

    def update_task(self, name, spec):
        self.task_updates.append((name, spec))
        return {"updated": name}


_TASKS_YAML = (
    "tasks:\n"
    "  - name: l1_tx\n"
    "    command: [python3, tx.py]\n"
    "    env: { SDR_CAL_SIGNAL_ID: mock, FOO: bar }\n"
    "  - name: unrelated\n"
    "    command: [python3, other.py]\n"
    "    env: { SDR_CAL_SIGNAL_ID: something_else }\n"
)


def test_renaming_a_signal_offers_to_update_referencing_tasks(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    client = _TasksClient(_TASKS_YAML)
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._handle_tasks(_TASKS_YAML)                      # as the cal_tasks fetch would
    p._rename_signal("mock", "gnss_l1")
    assert len(client.task_updates) == 1             # only the referencing task
    name, spec = client.task_updates[0]
    assert name == "l1_tx"
    assert spec["env"]["SDR_CAL_SIGNAL_ID"] == "gnss_l1"
    assert spec["env"]["FOO"] == "bar"               # other env preserved
    assert spec["command"] == ["python3", "tx.py"]   # rest of the entry preserved


def test_rename_without_referencing_tasks_does_not_prompt(monkeypatch):
    asked = {}
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **k: asked.setdefault("q", True) or QMessageBox.StandardButton.Yes))
    client = _TasksClient("tasks:\n  - name: x\n    env: { SDR_CAL_SIGNAL_ID: other }\n")
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._handle_tasks(client._tasks_yaml)
    p._rename_signal("mock", "gnss_l1")
    assert "q" not in asked                           # no task references "mock" → no prompt
    assert client.task_updates == []


def test_rename_persists_only_the_rename_not_the_working_edits(monkeypatch):
    # A rename must stick without a full Save (so it doesn't revert while the tasks stay
    # renamed) — but it must NOT push the working doc's other unsaved edits.
    import copy, json
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    client = _TasksClient(_TASKS_YAML)
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    saved = _v2_doc()                                # saved min_gain_db == 0.0
    p._set_doc(saved)
    p._saved_doc = copy.deepcopy(saved)              # as _handle_get would record
    p._handle_tasks(_TASKS_YAML)
    p._f["min_gain"].setText("5")                    # a pending, unsaved working-doc edit
    p._rename_signal("mock", "gnss_l1")
    saves = [c for (nm, c) in client.uploaded if nm == "calibration.json"]
    assert saves, "the rename should be persisted on its own"
    pushed = json.loads(saves[-1])
    assert "gnss_l1" in pushed["signals"] and "mock" not in pushed["signals"]
    # the unsaved min-gain edit must NOT have gone out with the rename
    assert pushed.get("chain", {}).get("gain_limits", {}).get("min_gain_db") == 0.0


def test_rename_of_an_unsaved_signal_does_not_push(monkeypatch):
    # Nothing persisted yet for this signal → no rename-only upload (only a full Save would).
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    client = _TasksClient(_TASKS_YAML)
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_v2_doc())                            # no _saved_doc set (never fetched)
    p._handle_tasks(_TASKS_YAML)
    p._rename_signal("mock", "gnss_l1")
    assert [c for (nm, c) in client.uploaded if nm == "calibration.json"] == []


class _LibTask:
    def __init__(self, name, sid):
        self.name = name
        self.env = {"SDR_CAL_SIGNAL_ID": sid} if sid else {}


class _FakeLibStore:
    def __init__(self, tasks):
        self._tasks = tasks
        self.upserts = []
    def tasks(self):
        return list(self._tasks)
    def upsert_task(self, t):
        self.upserts.append((t.name, dict(t.env)))


class _UnitClient(_TasksClient):
    """A fleet unit with its own calibration + tasks (offline units raise)."""
    def __init__(self, tasks_yaml, cal_doc=None, offline=False):
        cal = {"unit_type": "broadcaster", "valid": True, "document": cal_doc} if cal_doc else None
        super().__init__(tasks_yaml, cal=cal)
        self._offline = offline
    def get_calibration(self):
        if self._offline:
            raise RuntimeError("offline")
        return self._cal
    def get_tasks_yaml(self):
        if self._offline:
            raise RuntimeError("offline")
        return self._tasks_yaml


class _FleetFleet:
    def __init__(self, clients, store):
        self._clients = clients
        self._store = store
    def get(self, host):
        return self._clients[host]
    def hostnames(self):
        return list(self._clients.keys())
    def library_store(self):
        return self._store


class _FleetHub(QObject):
    task_done = pyqtSignal(str, object)
    def __init__(self, fleet):
        super().__init__()
        self.fleet = fleet
    def run_async(self, label, fn):
        try:
            res = fn()
        except Exception as exc:            # noqa: BLE001
            res = exc
        self.task_done.emit(label, res)


def test_rename_propagates_across_the_fleet(monkeypatch):
    import copy, json
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    warned = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.setdefault("msg", a)))
    cur = _UnitClient(_TASKS_YAML)                                   # this unit
    unitB = _UnitClient(
        "tasks:\n  - name: b_tx\n    command: [python3, tx.py]\n"
        "    env: { SDR_CAL_SIGNAL_ID: mock }\n",
        cal_doc={"schema_version": 1, "signals": {"mock": {"curves": {}}, "keep": {}}})
    unitC = _UnitClient(_TASKS_YAML, offline=True)                   # unreachable
    store = _FakeLibStore([_LibTask("l1_tx", "mock"), _LibTask("noop", "other")])
    p = CalibrationPanel("u", _FleetHub(_FleetFleet(
        {"u": cur, "unitB": unitB, "unitC": unitC}, store)))
    _seed_catalog(p)
    saved = _v2_doc()
    p._set_doc(saved); p._saved_doc = copy.deepcopy(saved)
    p._handle_tasks(_TASKS_YAML)
    p._rename_signal("mock", "gnss_l1")

    # the shared library task is repointed (the deploy source)
    assert ("l1_tx", {"SDR_CAL_SIGNAL_ID": "gnss_l1"}) in store.upserts
    assert all(name != "noop" for name, _ in store.upserts)         # unrelated task untouched
    # this unit's own task updated
    assert cur.task_updates[0][1]["env"]["SDR_CAL_SIGNAL_ID"] == "gnss_l1"
    # the other ONLINE unit: calibration renamed + its task updated
    b_cal = [json.loads(c) for (nm, c) in unitB.uploaded if nm == "calibration.json"]
    assert b_cal and "gnss_l1" in b_cal[-1]["signals"] and "mock" not in b_cal[-1]["signals"]
    assert unitB.task_updates[0][1]["env"]["SDR_CAL_SIGNAL_ID"] == "gnss_l1"
    # the offline unit is reported, not silently skipped
    assert "msg" in warned and "unitC" in warned["msg"][2]


def test_cancel_aborts_the_whole_rename(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel))
    client = _TasksClient(_TASKS_YAML)
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._handle_tasks(_TASKS_YAML)
    p._rename_signal("mock", "gnss_l1")
    out = p._read_form(strict=False)["signals"]
    assert "mock" in out and "gnss_l1" not in out    # signal left untouched
    assert client.task_updates == []                 # and no task changed


def test_rename_signal_from_the_table_cell(monkeypatch):
    # Double-clicking the Signal cell and committing a new name renames the signal.
    from PyQt6.QtCore import Qt as _Qt
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    # find the "mock" row's Signal cell and edit it, as an in-cell edit would
    row = next(r for r in range(p._table.rowCount()) if p._table.item(r, 0).text() == "mock")
    item = p._table.item(row, 0)
    assert item.data(_Qt.ItemDataRole.UserRole) == "mock"          # stores the old id
    assert bool(item.flags() & _Qt.ItemFlag.ItemIsEditable)        # editable
    assert not (p._table.item(row, 1).flags() & _Qt.ItemFlag.ItemIsEditable)  # others aren't
    item.setText("gnss_l1")                                        # commit the edit
    out = p._read_form(strict=False)["signals"]
    assert "gnss_l1" in out and "mock" not in out


def test_remove_signal_from_measured_detail(monkeypatch):
    # The expanded signal section carries a "Remove signal" action; confirming it drops
    # the signal (and all its curves) from the document.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    assert "mock" in p._f["signals"]
    p._on_remove_signal("mock")
    assert "mock" not in p._f["signals"]
    assert "mock" not in (p._read_form(strict=False)["signals"])


def test_remove_signal_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._on_remove_signal("mock")
    assert "mock" in p._f["signals"]                     # cancelled → still there


def test_active_signal_highlight_tracks_the_open_editor():
    # A signal is "active" (row-highlighted) only while its editor is on screen: a
    # MEASURED stage is selected and the signal is expanded. It clears on a passive stage.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._select_signal("mock")                             # opens on a measured stage
    assert p._active_signal_ids() == {"mock"}
    p._select_plane("cable_output")                      # passive stage → editor not shown
    assert p._active_signal_ids() == set()


def test_signal_without_points_inherits_previous_stage():
    # A signal measured only at the source has no points on a later measured stage; the
    # editor names that inheritance rather than treating it as an error (see the agent's
    # partial measured-stage fallback).
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    d = _doc()
    d["chain"]["operating_plane"] = "amplifier_output"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "tp"},
        "amplifier_output": {"type": "measured", "quantity": "mlp"},
    }
    # mock has a curve only on sdr_output (from _doc); none on amplifier_output.
    p._set_doc(d)
    out = p._read_form(strict=False)
    assert "amplifier_output" not in out["signals"]["mock"]["curves"]
    # no local-check error for the missing downstream curve
    from ui.calibration_panel import local_calibration_issues
    assert not any("amplifier_output" in i for i in local_calibration_issues(out))


def _v2_doc_measured_both():
    d = _v2_doc()                                        # mock is measured only on the source
    d["signals"]["mock"]["curves"]["amplifier_output"] = {
        "points": [{"gain_db": 40, "power_dbm": -6}, {"gain_db": 74, "power_dbm": 24}]}
    return d


def test_non_source_stage_shows_only_measured_signals():
    # mock is measured only on the source → it must NOT appear on a downstream measured
    # stage (that stage lists overrides, not the whole signal set).
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    assert p._signal_shown_on("mock", "sdr_output") is True
    assert p._signal_shown_on("mock", "amplifier_output") is False
    # once it's measured there, it shows
    p._set_doc(_v2_doc_measured_both())
    assert p._signal_shown_on("mock", "amplifier_output") is True


def test_clicking_signal_keeps_open_measured_stage():
    # A signal measured on the currently-open downstream stage stays there on click —
    # the view doesn't jump back to Source.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc_measured_both())
    p._select_plane("amplifier_output")
    p._on_signal_row_clicked(0, 0)                       # click the mock row
    assert p._selected_plane == "amplifier_output"
    assert p._expanded_signals == {"mock"}


def test_clicking_signal_falls_back_to_source_when_not_on_stage():
    # If the open stage doesn't carry the signal, clicking it drops to Source (which
    # always shows every signal).
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())                                # mock only on source
    p._select_plane("amplifier_output")
    p._on_signal_row_clicked(0, 0)
    assert p._selected_plane == "sdr_output"


def test_add_signal_to_downstream_stage(monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getItem",
                        staticmethod(lambda *a, **k: ("mock", True)))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())                                # mock only on source
    p._select_plane("amplifier_output")
    p._add_signal_to_stage("amplifier_output")
    assert "mock" in p._stage_extra.get("amplifier_output", set())
    assert p._signal_shown_on("mock", "amplifier_output") is True


def test_remove_signal_from_stage_keeps_signal(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc_measured_both())
    p._on_remove_signal_from_stage("mock", "amplifier_output")
    out = p._read_form(strict=False)
    assert "mock" in out["signals"]                      # the signal itself stays
    curves = out["signals"]["mock"]["curves"]
    assert "amplifier_output" not in curves              # only its data on this stage went
    assert "sdr_output" in curves                        # upstream measurement is intact


def _three_measured_doc():
    d = _doc()
    d["chain"]["operating_plane"] = "amp2"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "tp"},
        "amp1": {"type": "measured", "quantity": "m1"},
        "amp2": {"type": "measured", "quantity": "m2"},
    }
    pts = lambda a, b: {"points": [{"gain_db": 40, "power_dbm": a},
                                   {"gain_db": 74, "power_dbm": b}]}
    d["signals"]["mock"]["curves"] = {
        "sdr_output": pts(-36, -2.5), "amp1": pts(-6, 24), "amp2": pts(-4, 26)}
    return d


def test_remove_signal_from_stage_cascades_downstream(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_three_measured_doc())
    p._on_remove_signal_from_stage("mock", "amp1")       # remove at a middle stage
    curves = p._read_form(strict=False)["signals"]["mock"]["curves"]
    assert set(curves) == {"sdr_output"}                 # amp1 AND downstream amp2 removed


def test_calibration_panel_binds_fleet_shared_catalog():
    # A calibration panel uses the fleet's ONE shared component catalog, so a part
    # characterized in the Library tab is the same instance the chain pickers see.
    from api.fleet import Fleet

    class HubWithFleet(QObject):
        task_done = pyqtSignal(str, object)

        def __init__(self, fleet):
            super().__init__()
            self.fleet = fleet

    fleet = Fleet()
    p = CalibrationPanel("u", HubWithFleet(fleet))
    assert p._catalog is fleet.component_catalog()


def test_component_library_panel_characterizes_with_no_unit(tmp_path):
    # The library editor is a standalone panel: characterizing a part needs no unit — it
    # writes straight to the local catalog file.
    from state import ComponentCatalog
    from ui.component_library_dialog import ComponentLibraryPanel
    cat = ComponentCatalog(path=tmp_path / "components.json")
    panel = ComponentLibraryPanel(cat)
    panel._new()
    panel._id.setText("ant_x")
    panel._kind.setCurrentText("Antenna")
    panel._table.set_rows([[1.2e9, 5.0], [1.6e9, 6.0]])
    panel._save()
    assert "ant_x" in ComponentCatalog(path=tmp_path / "components.json").ids()  # persisted


def test_delta_sparkline_colours_by_sign():
    # A component's Δ dB curve is coloured by net sign (gain=accent, loss=red), NOT by
    # monotonicity — an antenna/cable that rolls off with frequency is still fine.
    from ui.calibration_panel import _Sparkline
    from ui.theme import Palette
    loss = _Sparkline(mode="delta"); loss.set_points([(1e9, -2.0), (2e9, -3.0)])
    assert loss._line_color().name() == QColor(Palette.CRASH).name()
    gain = _Sparkline(mode="delta"); gain.set_points([(1e9, 6.0), (2e9, 5.0)])   # rolls off
    assert gain._line_color().name() == QColor(Palette.ACCENT).name()            # still gain


def test_curve_sparkline_flags_non_invertible():
    # The gain→power sparkline (default mode) still reddens a non-increasing power run.
    from ui.calibration_panel import _Sparkline
    from ui.theme import Palette
    ok = _Sparkline(); ok.set_points([(40, -36), (74, -2.5)])
    assert ok._line_color().name() == QColor(Palette.ACCENT).name()
    bad = _Sparkline(); bad.set_points([(40, -20), (50, -20)])                    # flat power
    assert bad._line_color().name() == QColor(Palette.CRASH).name()


def _two_measured_doc(measure_second: bool):
    d = _doc()
    d["chain"]["operating_plane"] = "amplifier_output"
    d["chain"]["planes"] = {
        "sdr_output": {"type": "measured", "quantity": "tp"},
        "amplifier_output": {"type": "measured", "quantity": "mlp"},
    }
    if measure_second:
        d["signals"]["mock"]["curves"]["amplifier_output"] = {
            "points": [{"gain_db": 40, "power_dbm": -6}, {"gain_db": 74, "power_dbm": 24}]}
    return d


def test_partial_stage_detection():
    # A signal missing the curve for the 2nd measured stage relies on the fallback…
    assert CalibrationPanel._doc_uses_partial_stages(_two_measured_doc(False)) is True
    # …but a fully-measured document does not.
    assert CalibrationPanel._doc_uses_partial_stages(_two_measured_doc(True)) is False


def test_partial_stage_save_blocked_on_old_agent():
    # An agent without the 1.3.0 capability would reject the partial-stage doc, so Save
    # warns instead of pushing it.
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_two_measured_doc(False))
    p._on_save()
    assert "1.3.0" in p._status.text()
    assert client.uploaded == []                         # nothing was pushed


def test_partial_stage_save_allowed_on_capable_agent():
    client = FakeClient(caps=["calibration", "calibration-partial-stages"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_two_measured_doc(False))
    p._on_save()
    assert client.uploaded                                # the file was pushed (not blocked)


def test_no_signals_save_blocked_on_old_agent():
    # An agent without the 1.4.0 capability rejects a signal-less document, so Save warns
    # instead of pushing an onboarding chain that would fail on the unit.
    d = _doc()
    d["signals"] = {}
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(d)
    p._on_save()
    assert "1.4.0" in p._status.text()
    assert client.uploaded == []                         # nothing was pushed


def test_no_signals_save_allowed_on_capable_agent():
    d = _doc()
    d["signals"] = {}
    client = FakeClient(caps=["calibration", "calibration-no-signals"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(d)
    p._on_save()
    assert client.uploaded                                # the onboarding chain was pushed


# ── limit side: input/output stage boundary ────────────────────────────────────────

def _side_doc(side):
    d = _two_measured_doc(measure_second=True)
    d["chain"]["limits"] = [{"plane": "amplifier_output", "side": side, "max_dbm": -2.5}]
    return d


def test_limit_side_round_trips_through_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_side_doc("input"))
    out = p._read_form(strict=True)
    assert out["chain"]["limits"][0]["side"] == "input"
    assert out["chain"]["limits"][0]["plane"] == "amplifier_output"


def test_limit_side_output_is_omitted_from_document():
    # 'output' is the default, so it is not written — keeps existing docs byte-identical.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_side_doc("output"))
    lim = p._read_form(strict=True)["chain"]["limits"][0]
    assert "side" not in lim


def test_local_issues_input_side_on_first_plane_flagged():
    from ui.calibration_panel import local_calibration_issues
    d = _two_measured_doc(measure_second=True)
    d["chain"]["limits"] = [{"plane": "sdr_output", "side": "input", "max_dbm": -2.5}]
    assert any("nothing upstream" in i for i in local_calibration_issues(d))
    # the same limit input-side on the 2nd stage is fine (sdr_output is upstream)
    d["chain"]["limits"] = [{"plane": "amplifier_output", "side": "input", "max_dbm": -2.5}]
    assert local_calibration_issues(d) == []


def test_limit_side_detection():
    assert CalibrationPanel._doc_uses_limit_side(_side_doc("input")) is True
    assert CalibrationPanel._doc_uses_limit_side(_side_doc("output")) is False


def test_input_side_save_blocked_on_old_agent():
    # A ≤1.4.0 agent ignores 'side' and would cap the amp's OUTPUT instead of its input —
    # a different, unsafe limit. Save must refuse rather than silently mis-protect.
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_side_doc("input"))
    p._on_save()
    assert "1.5.0" in p._status.text()
    assert client.uploaded == []                         # nothing was pushed


def test_input_side_save_allowed_on_capable_agent():
    client = FakeClient(caps=["calibration", "calibration-limit-side"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_side_doc("input"))
    p._on_save()
    assert client.uploaded                                # the side-using doc was pushed


# ── measured-plane roles: limiting vs reported (§4.1) ───────────────────────────────

def _roles_doc():
    """A two-measured chain where the 2nd stage is reported (re-measures the source)."""
    d = _two_measured_doc(measure_second=True)
    d["chain"]["planes"]["amplifier_output"] = {
        "type": "measured", "quantity": "main-lobe power",
        "role": "reported", "of": "sdr_output"}
    return d


def test_reported_role_round_trips_through_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_roles_doc())
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["amplifier_output"]["role"] == "reported"
    assert planes["amplifier_output"]["of"] == "sdr_output"
    # the source stays limiting (no role key emitted — limiting is the default)
    assert "role" not in planes["sdr_output"]


def test_limiting_role_is_omitted_from_document():
    # 'limiting' is the default, so an all-limiting chain is byte-identical to a v1 doc.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_two_measured_doc(measure_second=True))
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert all("role" not in pl and "of" not in pl for pl in planes.values())


def test_plane_roles_detection():
    assert CalibrationPanel._doc_uses_plane_roles(_roles_doc()) is True
    assert CalibrationPanel._doc_uses_plane_roles(_two_measured_doc(measure_second=True)) is False


def test_reported_of_is_derived_automatically_not_from_the_document():
    # The input doc marks the stage reported but names a bogus `of`; the panel ignores it
    # and derives `of` from chain position (nearest limiting stage upstream = sdr_output).
    d = _two_measured_doc(measure_second=True)
    d["chain"]["planes"]["amplifier_output"] = {
        "type": "measured", "role": "reported", "of": "bogus", "quantity": "mlp"}
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    assert p._read_form(strict=True)["chain"]["planes"]["amplifier_output"]["of"] == "sdr_output"


def test_reported_stack_shares_the_limiting_basis():
    # Two reported re-measurements of the source node both gauge on the source (the nearest
    # limiting stage upstream), skipping over the intervening reported stage.
    d = _doc()
    d["chain"]["operating_plane"] = "view_b"
    d["chain"]["planes"] = {
        "source": {"type": "measured", "quantity": "full-band"},
        "view_a": {"type": "measured", "role": "reported", "quantity": "main-lobe"},
        "view_b": {"type": "measured", "role": "reported", "quantity": "narrower"},
    }
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    planes = p._read_form(strict=True)["chain"]["planes"]
    assert planes["view_a"]["of"] == "source"
    assert planes["view_b"]["of"] == "source"


def test_reported_after_a_passive_stage_is_not_honoured():
    # A passive stage between the reported stage and any limiting curve means it's a
    # different physical node, so there's no valid basis — the reported mark is dropped.
    d = _doc()
    d["chain"]["operating_plane"] = "post_pad"
    d["chain"]["planes"] = {
        "source":   {"type": "measured", "quantity": "full-band"},
        "pad":      {"type": "derived", "from": "source", "delta_db": -6.0},
        "post_pad": {"type": "measured", "role": "reported", "quantity": "main-lobe"},
    }
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    post = p._read_form(strict=True)["chain"]["planes"]["post_pad"]
    assert "role" not in post and "of" not in post          # left limiting, no basis


def test_reported_role_save_blocked_on_old_agent():
    # A ≤1.5.2 agent treats a reported stage as limiting and would mis-gauge the ceiling.
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_roles_doc())
    p._on_save()
    assert "1.6.0" in p._status.text()
    assert client.uploaded == []                         # nothing was pushed


def test_reported_role_save_allowed_on_capable_agent():
    client = FakeClient(caps=["calibration", "calibration-plane-roles"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_roles_doc())
    p._on_save()
    assert client.uploaded                                # the role-using doc was pushed


# ── gain step (SDR gain grid) ───────────────────────────────────────────────────────

def test_gain_step_round_trips_through_form():
    d = _doc()
    d["chain"]["gain_limits"]["gain_step_db"] = 0.25
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    assert p._f["gain_step"].text() in ("0.25", ".25")
    gl = p._read_form(strict=True)["chain"]["gain_limits"]
    assert gl["gain_step_db"] == 0.25


def test_blank_gain_step_is_omitted():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())                                   # no gain_step_db
    assert "gain_step_db" not in p._read_form(strict=True)["chain"]["gain_limits"]


def test_clearing_gain_step_removes_it():
    d = _doc()
    d["chain"]["gain_limits"]["gain_step_db"] = 0.25
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    p._f["gain_step"].setText("")                        # operator clears it
    assert "gain_step_db" not in p._read_form(strict=True)["chain"]["gain_limits"]


def test_gain_step_detection():
    d = _doc(); d["chain"]["gain_limits"]["gain_step_db"] = 0.25
    assert CalibrationPanel._doc_uses_gain_step(d) is True
    assert CalibrationPanel._doc_uses_gain_step(_doc()) is False


def test_gain_step_save_blocked_on_old_agent():
    d = _doc(); d["chain"]["gain_limits"]["gain_step_db"] = 0.25
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(d)
    p._on_save()
    assert "1.7.0" in p._status.text()
    assert client.uploaded == []


def test_gain_step_save_allowed_on_capable_agent():
    d = _doc(); d["chain"]["gain_limits"]["gain_step_db"] = 0.25
    client = FakeClient(caps=["calibration", "calibration-gain-step"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(d)
    p._on_save()
    assert client.uploaded


# ── optional centre frequency on a frequency-dependent chain (agent >= 1.7.1) ───────

def _freq_dep_no_center_doc():
    d = _v2_doc()
    d["signals"]["mock"].pop("center_freq_hz", None)     # blank on a freq-dependent chain
    return d


def test_freq_optional_center_detection():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    # a freq-dependent chain with no centre frequency → needs the newer agent
    assert p._doc_uses_freq_optional_center(_freq_dep_no_center_doc()) is True
    # the same chain WITH a centre frequency resolves on any agent → not gated
    assert p._doc_uses_freq_optional_center(_v2_doc()) is False
    # a flat chain (no multi-point component) is never gated, centre frequency or not
    assert p._doc_uses_freq_optional_center(_doc()) is False


_FREQ_BASE_CAPS = ["calibration", "calibration-components", "calibration-partial-stages"]


def test_freq_optional_center_save_blocked_on_old_agent():
    client = FakeClient(caps=_FREQ_BASE_CAPS)          # everything BUT the 1.7.1 gate
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_freq_dep_no_center_doc())
    p._on_save()
    assert "1.7.1" in p._status.text()
    assert client.uploaded == []


def test_freq_optional_center_save_allowed_on_capable_agent():
    client = FakeClient(caps=_FREQ_BASE_CAPS + ["calibration-freq-optional-center"])
    p = CalibrationPanel("u", FakeHub(client))
    _seed_catalog(p)
    p._set_doc(_freq_dep_no_center_doc())
    p._on_save()
    assert client.uploaded


# ── unsaved-changes tracking (warn before leaving the Calibration tab) ──────────────

def test_freshly_loaded_document_has_no_unsaved_changes():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    assert p.has_unsaved_changes() is False


def test_editing_a_field_marks_unsaved_changes():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    p._f["max_gain"].setText("50")                      # a real edit
    assert p.has_unsaved_changes() is True


def test_reloading_the_document_clears_unsaved_changes():
    # A successful save re-GETs and reloads the document; _set_doc re-baselines, so the
    # tab reads clean again.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(_doc())
    p._f["max_gain"].setText("50")
    assert p.has_unsaved_changes() is True
    p._set_doc(p._read_form(strict=False))              # emulate the post-save reload
    assert p.has_unsaved_changes() is False


def test_no_baseline_means_no_unsaved_changes():
    # Before any document is loaded there's nothing to lose, so leaving never prompts.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    assert p.has_unsaved_changes() is False


def test_request_save_dispatches_and_returns_true():
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_doc())
    p._f["max_gain"].setText("50")
    assert p.request_save() is True
    assert client.uploaded                               # the edit was pushed


def test_request_save_blocked_returns_false():
    # A capability guard (reported stage on a ≤1.5.2 agent) blocks the save — request_save
    # reports False so the host keeps the user on the tab to see the error.
    client = FakeClient(caps=["calibration"])
    p = CalibrationPanel("u", FakeHub(client))
    p._set_doc(_roles_doc())
    assert p.request_save() is False
    assert client.uploaded == []


def test_library_grid_has_a_card_per_component_plus_add():
    from ui.calibration_panel import _ClickCard
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    cards = [p._lib_grid.itemAt(i).widget() for i in range(p._lib_grid.count())]
    clickables = [w for w in cards if isinstance(w, _ClickCard)]
    # one card per catalogued component, plus the trailing "add" card
    assert len(clickables) == len(p._catalog.ids()) + 1
    assert len(p._catalog.ids()) >= 2            # our two seeded parts are present


def test_plot_markers_use_chosen_label_and_merge_shared_frequencies():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    import copy
    d = _v2_doc()
    base = copy.deepcopy(d["signals"]["mock"])
    d["signals"] = {}
    # two signals on the very same frequency, one with a chosen label
    a = copy.deepcopy(base); a["center_freq_hz"] = 1575.42e6; a["plot_label"] = "L1"
    b = copy.deepcopy(base); b["center_freq_hz"] = 1575.42e6            # id-derived label
    c = copy.deepcopy(base); c["center_freq_hz"] = 1227.6e6; c["plot_label"] = "L2"
    d["signals"] = {"gps_l1": a, "galileo_e1": b, "gps_l2": c}
    p._set_doc(d)
    markers = p._signal_markers()
    freqs = sorted(m[1] for m in markers)
    assert freqs == [1227.6e6, 1575.42e6]            # one marker per distinct frequency
    merged = next(m for m in markers if m[1] == 1575.42e6)
    assert "L1" in merged[0] and "e1" in merged[0]   # both labels combined on one line
    lone = next(m for m in markers if m[1] == 1227.6e6)
    assert lone[0] == "L2"                           # the chosen label is used


def test_plot_label_round_trips_through_the_form():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._f["signals"]["mock"]["plabel"].setText("L1/E1")
    out = p._read_form(strict=False)
    assert out["signals"]["mock"]["plot_label"] == "L1/E1"


def test_reorder_stage_moves_a_stage_and_pins_the_source():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    order = lambda: list(p._read_form(strict=False)["chain"]["planes"])
    assert order() == ["sdr_output", "amplifier_output", "cable_output", "antenna_eirp"]
    # drag antenna_eirp onto amplifier_output → it lands just before it
    p._reorder_stage("antenna_eirp", "amplifier_output")
    assert order() == ["sdr_output", "antenna_eirp", "amplifier_output", "cable_output"]
    # the operating plane always follows the last stage
    assert p._read_form(strict=False)["chain"]["operating_plane"] == "cable_output"
    # the source is pinned: it can't move and nothing can take slot 0
    p._reorder_stage("sdr_output", "cable_output")
    assert order()[0] == "sdr_output"
    p._reorder_stage("cable_output", "sdr_output")
    assert order()[0] == "sdr_output"


def test_reorder_planes_to_lands_and_pins_source():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    order = lambda: list(p._read_form(strict=False)["chain"]["planes"])
    p._reorder_planes_to("antenna_eirp", 1)              # move the last stage up to slot 1
    assert order() == ["sdr_output", "antenna_eirp", "amplifier_output", "cable_output"]
    p._reorder_planes_to("cable_output", 0)              # clamped: never before the source
    assert order()[0] == "sdr_output"
    src = order()[0]
    p._reorder_planes_to(src, 3)                         # the source itself never moves
    assert order()[0] == src


def test_chain_drag_lifecycle_commits_order():
    from PyQt6.QtCore import QPoint
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._chain_holder.resize(1200, 200); p._chain_row.activate()
    p._chain_drag_start("antenna_eirp", p._chain_holder.mapToGlobal(QPoint(1000, 60)))
    assert p._drag is not None and p._drag["plane"] == "antenna_eirp"
    p._move_placeholder(1)                               # drop it into slot 1
    p._chain_drag_end()
    assert p._drag is None                               # drag state cleared
    order = list(p._read_form(strict=False)["chain"]["planes"])
    assert order == ["sdr_output", "antenna_eirp", "amplifier_output", "cable_output"]


def test_freq_interp_endpoint_clamped():
    from ui.calibration_panel import _interp_db
    table = [[1.1e9, -2.30], [1.6e9, -2.81]]
    assert _interp_db(table, 1.1e9) == -2.30
    assert _interp_db(table, 0.5e9) == -2.30     # below span → clamp low
    assert _interp_db(table, 2.0e9) == -2.81     # above span → clamp high
    mid = _interp_db(table, 1.35e9)
    assert -2.81 < mid < -2.30                    # interpolated
    assert _interp_db([[0, -3.0]], 5e9) == -3.0  # single point → constant


# ── stage bypass ───────────────────────────────────────────────────────────────

def _doc2():
    """The base doc plus a derived 'pad' stage after the source (operating = pad)."""
    d = _doc()
    d["chain"]["planes"]["pad"] = {"type": "derived", "from": "sdr_output", "delta_db": -10.0}
    d["chain"]["operating_plane"] = "pad"
    return d


def test_bypass_round_trips_and_is_serialized():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-stage-bypass"])))
    p._set_doc(_doc2())
    assert p._f["planes"][1].get("bypass") is False
    p._f["planes"][1]["bypass"] = True
    out = p._read_form(strict=False)
    assert out["chain"]["planes"]["pad"]["bypass"] is True


def test_bypass_loads_from_doc():
    d = _doc2(); d["chain"]["planes"]["pad"]["bypass"] = True
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-stage-bypass"])))
    p._set_doc(d)
    assert p._f["planes"][1].get("bypass") is True


def test_source_stage_bypass_is_never_serialized():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=["calibration-stage-bypass"])))
    p._set_doc(_doc2())
    p._f["planes"][0]["bypass"] = True             # the source can't be bypassed
    out = p._read_form(strict=False)
    assert "bypass" not in out["chain"]["planes"]["sdr_output"]


def test_bypassed_stage_skips_local_validation():
    from ui.calibration_panel import local_calibration_issues
    d = _doc2()
    # a bypassed derived stage with NO Δ/component is transparent → must NOT be flagged
    d["chain"]["planes"]["pad"] = {"type": "derived", "from": "sdr_output", "bypass": True}
    assert not any("pad" in i for i in local_calibration_issues(d))
    # the same stage un-bypassed IS flagged (missing Δ dB)
    d["chain"]["planes"]["pad"] = {"type": "derived", "from": "sdr_output"}
    assert any("pad" in i and "Δ" in i for i in local_calibration_issues(d))


# ── source-bias stage ──────────────────────────────────────────────────────────

def _bias_caps():
    return ["calibration-source-bias", "calibration-stage-bypass"]


def test_source_bias_survives_read_form():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_bias_caps())))
    d = _doc(); d["source_bias"] = {"power_by_freq": [[9.0e8, -8.0], [1.5e9, -10.0]]}
    p._set_doc(d)
    out = p._read_form(strict=False)
    assert out["source_bias"]["power_by_freq"] == [[9.0e8, -8.0], [1.5e9, -10.0]]


def test_source_bias_card_builds_from_doc():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_bias_caps())))
    d = _doc(); d["source_bias"] = {"power_by_freq": [[1.5e9, -10.0]]}
    p._set_doc(d)
    assert p._source_bias_card() is not None      # renders without error


def test_bias_bypass_toggle_writes_doc():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_bias_caps())))
    d = _doc(); d["source_bias"] = {"power_by_freq": [[1.5e9, -10.0]]}
    p._set_doc(d)
    p._toggle_bias_bypass(True)
    assert p._doc["source_bias"]["bypass"] is True
    p._toggle_bias_bypass(False)
    assert "bypass" not in p._doc["source_bias"]


def test_save_blocks_source_bias_without_capability():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=[])))       # agent too old
    d = _doc(); d["source_bias"] = {"power_by_freq": [[1.5e9, -10.0]]}
    p._set_doc(d)
    assert p._blocks_on_source_bias() is True


def test_save_allows_source_bias_with_capability():
    p = CalibrationPanel("u", FakeHub(FakeClient(caps=_bias_caps())))
    d = _doc(); d["source_bias"] = {"power_by_freq": [[1.5e9, -10.0]]}
    p._set_doc(d)
    assert p._blocks_on_source_bias() is False
