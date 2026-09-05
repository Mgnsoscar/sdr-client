"""
ComponentCatalog — the client's canonical library of RF components (calibration v2).

Cables, antennas and pads are characterized once here — each as a signed
``delta_db``-vs-frequency table (a VNA sweep: negative = loss, positive = gain) — and
reused across every unit. A unit's calibration.json references a component by id from a
``derived`` plane; the catalog is uploaded to each unit's data store as
``components.yaml`` so the agent resolves the reference at transmit time. See the
agent's docs/calibration-v2.md.

This is a small, Qt-free CRUD wrapper over a local JSON file. The wire format it
uploads/parses is the ``{schema_version, components: {id: {...}}}`` shape the agent
reads.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from paths import data_file

logger = logging.getLogger(__name__)

DEFAULT_COMPONENTS_FILE = data_file("components.json")
COMPONENTS_WIRE_NAME = "components.yaml"     # the reserved name in the unit's data store
KINDS = ("cable", "antenna", "pad")


class CatalogError(ValueError):
    """A malformed component (bad kind, or a bad delta_db_by_freq table)."""


def validate_table(raw) -> List[List[float]]:
    """Validate + sort a ``[[freq_hz, delta_db], …]`` table: at least one point, each a
    numeric (freq, delta) pair, strictly increasing in frequency. Returns a fresh sorted
    list of ``[freq, delta]`` pairs (signed dB; one point = a constant hop)."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise CatalogError("needs at least one (frequency, dB) point")
    pts: List[Tuple[float, float]] = []
    for row in raw:
        try:
            f, d = row
            pts.append((float(f), float(d)))
        except (TypeError, ValueError):
            raise CatalogError(f"malformed point: {row!r}")
    pts.sort(key=lambda fd: fd[0])
    for i in range(1, len(pts)):
        if pts[i][0] <= pts[i - 1][0]:
            raise CatalogError(
                f"frequencies must strictly increase (repeated near {pts[i][0]:g} Hz)")
    return [[f, d] for f, d in pts]


def parse_sweep(text: str) -> List[List[float]]:
    """Parse pasted VNA-sweep text into a delta table. Each non-empty line is a
    ``frequency, dB`` pair (comma / tab / whitespace separated); frequency in Hz, dB
    signed. Blank lines and a leading header row of non-numbers are skipped."""
    rows: List[List[float]] = []
    for line in (text or "").splitlines():
        line = line.strip().replace(",", " ").replace("\t", " ")
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            rows.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue                                # header / stray line → skip
    if not rows:
        raise CatalogError("no 'frequency dB' rows found in the pasted text")
    return validate_table(rows)


def _norm(cid: str, spec: dict) -> dict:
    """Validate + normalize one component spec. ``kind`` is a free-text label (grouping
    only — the resolver never interprets it), so any non-empty string is accepted;
    KINDS are just the suggested defaults the editor offers."""
    kind = (spec.get("kind") or "cable").strip().lower() or "cable"
    out = {"kind": kind, "delta_db_by_freq": validate_table(spec.get("delta_db_by_freq"))}
    if (spec.get("description") or "").strip():
        out["description"] = spec["description"].strip()
    return out


class ComponentCatalog:
    def __init__(self, path: Path = DEFAULT_COMPONENTS_FILE):
        self._path = Path(path)
        self._comps: Dict[str, dict] = {}
        self.load()

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> Dict[str, dict]:
        if not self._path.exists() or self._path.stat().st_size == 0:
            self._comps = {}
            return self.components()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._comps = self._ingest(data.get("components") if isinstance(data, dict) else data)
        except (OSError, ValueError, TypeError, CatalogError) as exc:
            logger.error("Could not read components from %s: %s", self._path, exc)
            self._comps = {}
        return self.components()

    def _save(self) -> None:
        data = {"schema_version": 1, "components": self._comps}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    @staticmethod
    def _ingest(raw) -> Dict[str, dict]:
        """Validate a ``{id: spec}`` map, dropping (with a log) any bad entry rather than
        failing the whole load — one broken component shouldn't hide the rest."""
        out: Dict[str, dict] = {}
        for cid, spec in (raw or {}).items():
            if not isinstance(spec, dict):
                continue
            try:
                out[str(cid)] = _norm(str(cid), spec)
            except CatalogError as exc:
                logger.warning("Skipping component %s: %s", cid, exc)
        return out

    # ── Accessors / mutations ────────────────────────────────────────────────

    def components(self) -> Dict[str, dict]:
        """A deep-ish copy of {id: spec}, so callers can't mutate the store in place."""
        return {cid: {**s, "delta_db_by_freq": [list(p) for p in s["delta_db_by_freq"]]}
                for cid, s in self._comps.items()}

    def ids(self, kind: Optional[str] = None) -> List[str]:
        return sorted(cid for cid, s in self._comps.items()
                      if kind is None or s.get("kind") == kind)

    def get(self, cid: str) -> Optional[dict]:
        s = self._comps.get(cid)
        return None if s is None else {**s, "delta_db_by_freq": [list(p) for p in s["delta_db_by_freq"]]}

    def put(self, cid: str, kind: str, delta_db_by_freq, description: str = "") -> None:
        cid = (cid or "").strip()
        if not cid:
            raise CatalogError("a component needs an id")
        self._comps[cid] = _norm(cid, {"kind": kind, "description": description,
                                       "delta_db_by_freq": delta_db_by_freq})
        self._save()

    def remove(self, cid: str) -> None:
        if self._comps.pop(cid, None) is not None:
            self._save()

    def rename(self, old: str, new: str) -> None:
        """Rename a component id, preserving insertion order. No-op if `old` is absent;
        raises if `new` is empty or already taken (by a different component)."""
        new = (new or "").strip()
        if old == new:
            return
        if not new:
            raise CatalogError("a component needs an id")
        if old not in self._comps:
            return
        if new in self._comps:
            raise CatalogError(f"a component named {new!r} already exists")
        self._comps = {(new if k == old else k): v for k, v in self._comps.items()}
        self._save()

    def replace_all(self, comps: Dict[str, dict]) -> None:
        self._comps = self._ingest(comps)
        self._save()

    def merge(self, comps: Dict[str, dict]) -> int:
        """Add components this catalog doesn't already have (by id). Returns how many
        were added — used when pulling a unit's catalog into a fresh client."""
        added = 0
        for cid, spec in self._ingest(comps).items():
            if cid not in self._comps:
                self._comps[cid] = spec
                added += 1
        if added:
            self._save()
        return added

    # ── Wire format (components.yaml, what the agent reads) ───────────────────

    def to_wire(self) -> str:
        """The catalog as ``components.yaml`` text to upload to a unit's data store."""
        return yaml.safe_dump({"schema_version": 1, "components": self._comps},
                              sort_keys=True, allow_unicode=True)

    @staticmethod
    def parse_wire(text: str) -> Dict[str, dict]:
        """Parse a unit's uploaded ``components.yaml`` back into {id: spec} (validated)."""
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            raise CatalogError("components file is not an object")
        return ComponentCatalog._ingest(doc.get("components"))


# ── Fleet deploy planning (push the catalog to each unit) ──────────────────────────

def dump_components(comps: Dict[str, dict]) -> str:
    """Serialise a plain {id: spec} map to ``components.yaml`` wire text (what a unit
    stores). Mirrors ``ComponentCatalog.to_wire`` for an ad-hoc dict."""
    return yaml.safe_dump({"schema_version": 1, "components": comps},
                          sort_keys=True, allow_unicode=True)


def referenced_components(calibration_doc: Optional[dict]) -> set:
    """The component ids a unit's calibration references. These must never be stripped from a
    unit — the calibration wouldn't resolve, and the unit would refuse to transmit. Counts:
      * the transmit chain's derived-plane ``component`` fields;
      * every measurement DE-EMBED that names a catalog component (a string id, not an inline
        table) — on a measured plane, on a signal's OWN measured curve, or on the source bias.
        A de-embed cable is a bench artifact, but the resolver still evaluates it at resolve time,
        so deleting it from the shared library must KEEP it on any unit that measured through it."""
    doc = calibration_doc or {}
    out: set = set()

    def _add(v):
        if isinstance(v, str) and v:
            out.add(v)

    planes = ((doc.get("chain") or {}).get("planes") or {})
    for p in planes.values():
        if isinstance(p, dict):
            _add(p.get("component"))
            _add(p.get("measurement_deembed"))            # plane-level de-embed
    for sig in (doc.get("signals") or {}).values():
        if not isinstance(sig, dict):
            continue
        for curve in (sig.get("curves") or {}).values():
            if isinstance(curve, dict):
                _add(curve.get("measurement_deembed"))    # per-signal curve de-embed
    sb = doc.get("source_bias")
    if isinstance(sb, dict):
        _add(sb.get("measurement_deembed"))               # source-bias de-embed
    return out


def plan_unit_deploy(library: Dict[str, dict], on_unit: Dict[str, dict],
                     referenced: set, prune: bool) -> Tuple[Dict[str, dict], dict]:
    """Work out the ``components.yaml`` a unit should hold after a deploy, plus a summary
    of what changes — so the deploy can report it and nothing surprises the operator.

    The shared library is the source of truth: its components are added/updated on the
    unit. A component the library no longer has but the unit's calibration STILL
    references is KEPT on the unit (so it keeps resolving) — even when pruning. Pruning
    additionally drops unit components that are neither in the library nor referenced;
    without pruning, everything already on the unit is left in place.

    Returns ``(upload, info)`` where ``upload`` is the {id: spec} to send and ``info``
    groups ids by outcome: added / updated / pruned / kept_referenced / dangling."""
    library = dict(library or {})
    on_unit = dict(on_unit or {})
    if prune:
        upload = dict(library)
        for rid in referenced:                     # keep a referenced part the library dropped
            if rid not in upload and rid in on_unit:
                upload[rid] = on_unit[rid]
    else:
        upload = {**on_unit, **library}            # library wins on shared ids; keep the rest
    info = {
        "added": sorted(c for c in upload if c not in on_unit),
        "updated": sorted(c for c in upload if c in on_unit and upload[c] != on_unit[c]),
        "pruned": sorted(c for c in on_unit if c not in upload),
        # kept only because a calibration still uses it (the library dropped it):
        "kept_referenced": sorted(rid for rid in referenced
                                  if rid in upload and rid not in library),
        # referenced but resolvable nowhere (library and unit both lack it): broken chain.
        "dangling": sorted(rid for rid in referenced if rid not in upload),
        "count": len(upload),
    }
    return upload, info
