"""Offscreen widget tests for the CalibrationPanel: the resolved summary, the
not-calibrated / invalid states, agent-rejection handling, and the Editor⇄JSON form
model (round-trip, curve-grid edits, add/remove signal). A fake hub runs the async
calls synchronously and emits task_done."""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
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
        "defaults": {"amplitude": 0.8},
        "signals": {"mock": {"amplitude": 0.8, "curves": {
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
    assert out["signals"]["mock"]["amplitude"] == 0.8
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
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    # remove sdr_output (which carries a limit + a curve + is not the operating plane)
    row = next(r for r in p._f["planes"] if r["name"].text() == "sdr_output")
    p._remove_plane(row)
    chain = p._doc["chain"]
    assert "sdr_output" not in chain["planes"]
    assert all(l["plane"] != "sdr_output" for l in chain["limits"])
    assert "sdr_output" not in p._doc["signals"]["mock"]["curves"]


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

def test_unit_type_and_default_amplitude_round_trip():
    d = _doc()
    d["unit_type"] = "x410"
    d["defaults"] = {"amplitude": 0.7}
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    out = p._read_form(strict=True)
    assert out["unit_type"] == "x410"
    assert out["defaults"]["amplitude"] == 0.7
    # editing them in the form is read back
    p._f["def_amp"].setText("0.55")
    assert p._read_form(strict=True)["defaults"]["amplitude"] == 0.55


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


# ── F: curve polish (CSV paste, sparkline, inherited-amplitude placeholder) ────────

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


def test_blank_amplitude_shows_inherited_default_placeholder():
    d = _doc()
    d["defaults"] = {"amplitude": 0.8}
    d["signals"]["mock"].pop("amplitude", None)          # inherit
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    p._set_doc(d)
    amp = p._f["signals"]["mock"]["amp"]
    assert amp.text() == ""
    assert "0.8" in amp.placeholderText()


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
    from ui.calibration_panel import _ClickCard
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    stages = [p._chain_row.itemAt(i).widget() for i in range(p._chain_row.count())]
    # every stage is a card; the trailing dashed "+ Add stage" tile is one too.
    cards = [w for w in stages if isinstance(w, _ClickCard)
             and w.objectName() != "addstage"]
    assert len(cards) == 4                       # one stage per plane


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


def test_signals_table_shows_freq_and_amplitude():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    p._populate_table({"mock": {"min_power_dbm": -12.4, "max_power_dbm": 28.2}})
    assert p._table.columnCount() == 4
    assert p._table.item(0, 0).text() == "mock"
    assert p._table.item(0, 1).text() == "1575.42"       # centre freq in MHz
    assert p._table.item(0, 2).text() == "0.8"           # amplitude
    assert "28.2" in p._table.item(0, 3).text()          # resolved --power range


def test_multifreq_signal_shows_at_run():
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    d = _v2_doc()
    d["signals"]["mock"].pop("center_freq_hz", None)     # a chirp: many frequencies
    p._set_doc(d)
    p._populate_table({"mock": {}})
    assert p._table.item(0, 1).text() == "at run"
    assert p._table.item(0, 3).text() == "per frequency"


def test_editor_table_lists_signals_before_validate():
    # The signals table is populated straight from the document so signals are
    # clickable without a Validate first; --power reads a placeholder until then.
    p = CalibrationPanel("u", FakeHub(FakeClient()))
    _seed_catalog(p)
    p._set_doc(_v2_doc())
    assert p._table.rowCount() == 1
    assert p._table.item(0, 0).text() == "mock"
    assert p._table.item(0, 3).text() == "validate to resolve"


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


def test_freq_interp_endpoint_clamped():
    from ui.calibration_panel import _interp_db
    table = [[1.1e9, -2.30], [1.6e9, -2.81]]
    assert _interp_db(table, 1.1e9) == -2.30
    assert _interp_db(table, 0.5e9) == -2.30     # below span → clamp low
    assert _interp_db(table, 2.0e9) == -2.81     # above span → clamp high
    mid = _interp_db(table, 1.35e9)
    assert -2.81 < mid < -2.30                    # interpolated
    assert _interp_db([[0, -3.0]], 5e9) == -3.0  # single point → constant
