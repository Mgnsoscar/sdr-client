"""ComponentCatalog — the client's canonical library of RF components (calibration v2):
CRUD + validation, VNA-sweep paste parsing, and the components.yaml wire format the
agent reads. Pure logic, no Qt."""
import pytest

from state.component_catalog import (
    ComponentCatalog, CatalogError, parse_sweep, validate_table,
)


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


def test_put_rejects_bad_kind_and_empty_id(tmp_path):
    c = _cat(tmp_path)
    with pytest.raises(CatalogError, match="unknown kind"):
        c.put("x", "widget", [[0, -1.0]])
    with pytest.raises(CatalogError, match="needs an id"):
        c.put("  ", "cable", [[0, -1.0]])


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
