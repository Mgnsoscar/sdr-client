"""ComponentCatalog — the client's canonical library of RF components (calibration v2):
CRUD + validation, VNA-sweep paste parsing, and the components.yaml wire format the
agent reads. Pure logic, no Qt."""
import pytest

from state.component_catalog import (
    ComponentCatalog, CatalogError, parse_sweep, validate_table,
    referenced_components, plan_unit_deploy, dump_components,
)


def _spec(d, kind="cable"):
    return {"kind": kind, "delta_db_by_freq": [[1.0e9, d]]}


# ── table validation ─────────────────────────────────────────────────────────────

def test_validate_table_sorts_and_checks():
    out = validate_table([[2.0e9, -3.0], [1.0e9, -2.0]])
    assert out == [[1.0e9, -2.0], [2.0e9, -3.0]]          # sorted by frequency


def test_validate_table_rejects_empty_and_nonmonotonic():
    with pytest.raises(CatalogError):
        validate_table([])
    with pytest.raises(CatalogError, match="strictly increase"):
        validate_table([[1e9, -2.0], [1e9, -2.5]])
    with pytest.raises(CatalogError, match="malformed"):
        validate_table([["x", -2.0]])


def test_single_point_is_a_constant():
    assert validate_table([[0, -3.0]]) == [[0.0, -3.0]]


# ── VNA sweep paste ──────────────────────────────────────────────────────────────

def test_parse_sweep_various_separators_and_header():
    text = "freq_hz, loss_db\n1.0e9, -2.0\n1.5e9\t-2.4\n2.0e9  -3.0\n\n"
    assert parse_sweep(text) == [[1.0e9, -2.0], [1.5e9, -2.4], [2.0e9, -3.0]]


def test_parse_sweep_needs_rows():
    with pytest.raises(CatalogError):
        parse_sweep("not a table\n")


# ── CRUD + persistence ───────────────────────────────────────────────────────────

def _cat(tmp_path):
    return ComponentCatalog(path=tmp_path / "components.json")


def test_put_get_remove_persist(tmp_path):
    c = _cat(tmp_path)
    c.put("cable_a", "cable", [[1e9, -2.0], [2e9, -3.0]], description="LMR-240 3m")
    got = c.get("cable_a")
    assert got["kind"] == "cable" and got["description"] == "LMR-240 3m"
    assert got["delta_db_by_freq"] == [[1e9, -2.0], [2e9, -3.0]]
    # reload from disk → survives
    assert _cat(tmp_path).get("cable_a")["kind"] == "cable"
    c.remove("cable_a")
    assert c.get("cable_a") is None and _cat(tmp_path).get("cable_a") is None


def test_ids_filtered_by_kind(tmp_path):
    c = _cat(tmp_path)
    c.put("cab", "cable", [[0, -2.0]])
    c.put("ant", "antenna", [[0, 6.0]])
    c.put("pad", "pad", [[0, -3.0]])
    assert c.ids() == ["ant", "cab", "pad"]
    assert c.ids("cable") == ["cab"]
    assert c.ids("antenna") == ["ant"]


def test_kind_is_free_text(tmp_path):
    # kind is a free-text grouping label — any non-empty string is accepted (the maths
    # never interprets it); it's only lowercased/stripped.
    c = _cat(tmp_path)
    c.put("x", "Feedline", [[0, -1.0]])
    assert c.get("x")["kind"] == "feedline"
    c.put("y", "", [[0, -1.0]])                      # blank → defaults to 'cable'
    assert c.get("y")["kind"] == "cable"


def test_put_rejects_empty_id(tmp_path):
    c = _cat(tmp_path)
    with pytest.raises(CatalogError, match="needs an id"):
        c.put("  ", "cable", [[0, -1.0]])


def test_rename_moves_entry_and_guards(tmp_path):
    c = _cat(tmp_path)
    c.put("old", "cable", [[1e9, -2.0]], description="d")
    c.put("other", "antenna", [[1e9, 6.0]])
    c.rename("old", "new")
    assert "old" not in c.ids() and "new" in c.ids()
    assert c.get("new")["description"] == "d"
    c.rename("missing", "whatever")                  # no-op, no raise
    with pytest.raises(CatalogError, match="already exists"):
        c.rename("new", "other")                     # collision
    with pytest.raises(CatalogError, match="needs an id"):
        c.rename("new", "  ")


def test_get_and_components_return_copies(tmp_path):
    c = _cat(tmp_path)
    c.put("cab", "cable", [[1e9, -2.0]])
    got = c.get("cab")
    got["delta_db_by_freq"][0][1] = 999            # mutate the copy
    assert c.get("cab")["delta_db_by_freq"] == [[1e9, -2.0]]   # store unaffected


# ── wire format round-trip ───────────────────────────────────────────────────────

def test_wire_round_trip(tmp_path):
    c = _cat(tmp_path)
    c.put("cable_a", "cable", [[1e9, -2.0], [2e9, -3.0]], description="A")
    c.put("patch", "antenna", [[1e9, 5.0], [2e9, 7.0]])
    text = c.to_wire()
    back = ComponentCatalog.parse_wire(text)
    assert set(back) == {"cable_a", "patch"}
    assert back["cable_a"]["delta_db_by_freq"] == [[1e9, -2.0], [2e9, -3.0]]
    assert back["patch"]["kind"] == "antenna"


def test_merge_only_adds_missing(tmp_path):
    c = _cat(tmp_path)
    c.put("cab", "cable", [[0, -2.0]])
    added = c.merge({"cab": {"kind": "cable", "delta_db_by_freq": [[0, -9.0]]},
                     "new": {"kind": "pad", "delta_db_by_freq": [[0, -3.0]]}})
    assert added == 1
    assert c.get("cab")["delta_db_by_freq"] == [[0.0, -2.0]]    # existing kept
    assert c.get("new") is not None


def test_load_skips_a_broken_component(tmp_path):
    p = tmp_path / "components.json"
    p.write_text('{"components": {"ok": {"kind":"cable","delta_db_by_freq":[[0,-2]]},'
                 ' "bad": {"kind":"cable","delta_db_by_freq":[]}}}', encoding="utf-8")
    c = ComponentCatalog(path=p)
    assert c.get("ok") is not None and c.get("bad") is None


def test_fleet_shares_one_catalog():
    # The fleet hands out ONE shared catalog instance (created lazily), so the Library
    # tab and every unit's calibration panel read/write the same parts. No Qt, no unit.
    from api.fleet import Fleet
    f = Fleet()
    a = f.component_catalog()
    b = f.component_catalog()
    assert a is b


# ── deploy planning (fleet-wide push of the catalog to units) ───────────────────

def test_referenced_components_reads_chain():
    doc = {"chain": {"planes": {
        "sdr_output": {"type": "measured"},
        "cable": {"type": "derived", "from": "sdr_output", "component": "cab_a"},
        "ant": {"type": "derived", "from": "cable", "component": "ant_a"},
        "pad": {"type": "derived", "from": "ant", "delta_db": -1.0}}}}
    assert referenced_components(doc) == {"cab_a", "ant_a"}
    assert referenced_components(None) == set()
    assert referenced_components({}) == set()


def test_referenced_components_counts_measurement_deembed():
    # A measurement de-embed cable (plane-level, per-signal curve, or source-bias) is a bench
    # artifact but the resolver still evaluates it, so deleting it from the library must KEEP it on
    # any unit that measured through it — hence it counts as referenced. Inline tables reference no
    # catalog id and are ignored.
    doc = {
        "chain": {"planes": {
            "sdr_output": {"type": "measured", "measurement_deembed": "plane_cable"},
            "amp": {"type": "derived", "from": "sdr_output", "component": "amp_a"}}},
        "signals": {
            "gps": {"curves": {"sdr_output": {"measurement_deembed": "sig_cable"}},
                    "limiting": {"kind": "own", "curve": {"points": [[40, -20]]},
                                 "measurement_deembed": "own_cable"}},   # separate-measurement cable
            "gal": {"curves": {"sdr_output": {"measurement_deembed": [[0, -1.0]]}}}},  # inline → ignored
        "source_bias": {"power_by_freq": [[1e9, 0.0]], "measurement_deembed": "bias_cable"},
    }
    assert referenced_components(doc) == {"amp_a", "plane_cable", "sig_cable", "own_cable", "bias_cable"}


def test_deleted_deembed_cable_is_kept_on_a_unit_that_uses_it():
    # The owner's case: a measurement cable deleted from the shared library must survive on a unit
    # whose calibration still de-embeds it. plan_unit_deploy keeps a referenced part the library
    # dropped (from the unit's own copy) even when pruning.
    library = {"amp_a": {"kind": "amp", "delta_db_by_freq": [[0, 20.0]]}}   # cable deleted from lib
    on_unit = {"amp_a": {"kind": "amp", "delta_db_by_freq": [[0, 20.0]]},
               "sig_cable": {"kind": "cable", "delta_db_by_freq": [[0, -0.5]]}}
    doc = {"signals": {"gps": {"curves": {"sdr_output": {"measurement_deembed": "sig_cable"}}}}}
    upload, info = plan_unit_deploy(library, on_unit, referenced_components(doc), prune=True)
    assert "sig_cable" in upload                                # kept on the unit despite the delete
    assert upload["sig_cable"] == on_unit["sig_cable"]          # …from the unit's own copy
    assert "sig_cable" in info["kept_referenced"]
    assert not info["dangling"]


def test_plan_prune_keeps_referenced_and_prunes_unused():
    # ant_a was deleted from the shared library, but the unit's calibration still uses it
    # → it must persist on the unit; an unrelated old_pad the unit doesn't use is pruned.
    library = {"cab_a": _spec(-2.0)}
    on_unit = {"cab_a": _spec(-2.0), "ant_a": _spec(6.0, "antenna"), "old_pad": _spec(-1.0, "pad")}
    upload, info = plan_unit_deploy(library, on_unit, referenced={"cab_a", "ant_a"}, prune=True)
    assert set(upload) == {"cab_a", "ant_a"}
    assert info["kept_referenced"] == ["ant_a"]
    assert info["pruned"] == ["old_pad"]
    assert info["dangling"] == [] and info["added"] == [] and info["updated"] == []


def test_plan_flags_dangling_reference():
    # referenced by a calibration but present neither in the library nor on the unit
    _, info = plan_unit_deploy(library={}, on_unit={}, referenced={"ant_a"}, prune=True)
    assert info["dangling"] == ["ant_a"]


def test_plan_no_prune_is_union_with_library_winning():
    library = {"cab_a": _spec(-3.0)}                       # cab_a's value changed in the library
    on_unit = {"cab_a": _spec(-2.0), "extra": _spec(0.0)}  # extra is not in the library
    upload, info = plan_unit_deploy(library, on_unit, referenced=set(), prune=False)
    assert set(upload) == {"cab_a", "extra"}              # nothing removed without prune
    assert upload["cab_a"] == _spec(-3.0)                 # library value wins on shared ids
    assert info["updated"] == ["cab_a"] and info["pruned"] == []


def test_plan_reports_added():
    library = {"cab_a": _spec(-2.0), "new_ant": _spec(5.0, "antenna")}
    _, info = plan_unit_deploy(library, on_unit={"cab_a": _spec(-2.0)}, referenced=set(), prune=True)
    assert info["added"] == ["new_ant"]


def test_dump_components_round_trips():
    comps = {"cab_a": _spec(-2.0), "ant_a": _spec(6.0, "antenna")}
    assert ComponentCatalog.parse_wire(dump_components(comps)) == comps


class _FakeUnit:
    """Minimal client for exercising Fleet._deploy_components_to."""
    def __init__(self, cal, comps_text):
        self._cal = cal
        self._comps_text = comps_text
        self.uploaded = None

    def get_calibration(self):
        if self._cal is None:
            from api.client import AgentHTTPError
            raise AgentHTTPError("u", 404, "no calibration document")
        return self._cal

    def get_components(self):
        return self._comps_text

    def upload_components(self, text):
        self.uploaded = text
        return {"saved": "components.yaml"}


def test_deploy_components_to_keeps_referenced_part():
    from api.fleet import Fleet
    cal = {"document": {"chain": {"planes": {
        "sdr": {"type": "measured"},
        "ant": {"type": "derived", "from": "sdr", "component": "ant_a"}}}}}
    unit = _FakeUnit(cal, dump_components({"ant_a": _spec(6.0, "antenna"),
                                           "junk": _spec(-9.0)}))
    # library no longer has ant_a (deleted) — but the unit's calibration references it.
    info = Fleet._deploy_components_to(unit, library={"cab_a": _spec(-2.0)}, prune=True,
                                       cal=Fleet._fetch_calibration(unit))
    assert unit.uploaded is not None
    pushed = ComponentCatalog.parse_wire(unit.uploaded)
    assert set(pushed) == {"cab_a", "ant_a"}             # ant_a persisted, junk pruned
    assert info["kept_referenced"] == ["ant_a"] and info["pruned"] == ["junk"]


def test_deploy_components_to_skips_upload_when_unchanged():
    from api.fleet import Fleet
    same = {"cab_a": _spec(-2.0)}
    unit = _FakeUnit({"document": {"chain": {"planes": {"sdr": {"type": "measured"}}}}},
                     dump_components(same))
    info = Fleet._deploy_components_to(unit, library=same, prune=True,
                                       cal=Fleet._fetch_calibration(unit))
    assert unit.uploaded is None                          # nothing changed → no re-upload
    assert info["added"] == [] and info["pruned"] == []


def test_deploy_components_to_handles_uncalibrated_unit():
    from api.fleet import Fleet
    unit = _FakeUnit(None, "")                            # 404 calibration, no components yet
    info = Fleet._deploy_components_to(unit, library={"cab_a": _spec(-2.0)}, prune=True,
                                       cal=Fleet._fetch_calibration(unit))
    assert ComponentCatalog.parse_wire(unit.uploaded) == {"cab_a": _spec(-2.0)}
    assert info["added"] == ["cab_a"]
