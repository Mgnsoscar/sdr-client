"""
ScopeSelector — a small combo box for choosing which unit types a library item
(script / task / sequence) applies to.

The library is one canonical set shared by a heterogeneous fleet; every item
carries a `types` list. With today's two unit kinds the meaningful choices are
exactly three — Shared (all kinds), Broadcaster only, or X410 only — so a combo
is clearer than a checkbox per type. "applies to every kind" collapses to Shared
(an empty list), which is also how an item that names all known types is shown.

If the roster of unit types ever grows past two, swap this for a checkbox group;
the types/scope conversion helpers here stay valid.
"""
from __future__ import annotations

from typing import List

from PyQt6.QtWidgets import QComboBox, QLabel

from config import UNIT_TYPES, UNIT_TYPE_LABELS
from .theme import Palette

SHARED = "__shared__"


def types_to_scope(types: List[str]) -> str:
    """Collapse a stored `types` list to a single scope key for the combo. Empty,
    or a list naming every known unit type, is Shared; a single type is itself."""
    ts = [t for t in (types or []) if t in UNIT_TYPES]
    if not ts or set(ts) >= set(UNIT_TYPES):
        return SHARED
    return ts[0]


def scope_to_types(scope: str) -> List[str]:
    """The `types` list to store for a chosen scope. Shared → [] (applies to all)."""
    return [] if scope == SHARED else [scope]


def scope_label(types: List[str]) -> str:
    """A short human label for an item's scope — 'Shared', 'Broadcaster', 'X410'."""
    scope = types_to_scope(types)
    return "Shared" if scope == SHARED else UNIT_TYPE_LABELS.get(scope, scope)


def scope_chip(types: List[str]) -> QLabel:
    """A small pill showing a library item's unit-type scope. Shared reads muted;
    a type-scoped item stands out in the accent colour."""
    chip = QLabel(scope_label(types))
    shared = types_to_scope(types) == SHARED
    bg = Palette.BORDER if shared else Palette.ACCENT
    fg = Palette.TEXT_FAINT if shared else "#0E1116"
    chip.setStyleSheet(
        f"background: {bg}; color: {fg}; border-radius: 8px; padding: 1px 8px; "
        f"font-size: 10px; font-weight: 600;")
    return chip


class ScopeSelector(QComboBox):
    """A combo of Shared + each unit type. Read/write via scope()/set_from_types()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.addItem("Shared (all units)", SHARED)
        for t in UNIT_TYPES:
            self.addItem(f"{UNIT_TYPE_LABELS.get(t, t)} only", t)
        self.setToolTip(
            "Which units this applies to. Shared deploys to every unit; a single "
            "type deploys only to units of that kind.")

    def set_from_types(self, types: List[str]) -> None:
        idx = self.findData(types_to_scope(types))
        if idx >= 0:
            self.setCurrentIndex(idx)

    def scope(self) -> str:
        return self.currentData() or SHARED

    def types(self) -> List[str]:
        return scope_to_types(self.scope())
