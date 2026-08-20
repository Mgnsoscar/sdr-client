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

from PyQt6.QtWidgets import QComboBox, QLabel, QMessageBox

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


def is_shared(types: List[str]) -> bool:
    """True when an item applies to every unit kind (empty list, or all types named)."""
    return types_to_scope(types) == SHARED


def types_without(active_type: str) -> List[str]:
    """The `types` an item should keep when it's removed from `active_type` only —
    i.e. every OTHER unit kind. With today's two kinds this is the single other type;
    it stays correct if the roster grows."""
    return [t for t in UNIT_TYPES if t != active_type]


def confirm_delete(parent, kind: str, name: str, types: List[str], active_type: str,
                   unshare) -> str:
    """Ask how to remove a library item from a per-unit-type view, and return the
    chosen action: 'delete' (caller deletes it outright), 'unshared' (already
    re-scoped off this type — caller just refreshes), or 'cancel'.

    A type-only item is a plain confirm. A SHARED item (applies to every type) offers
    to remove it from `active_type` only — keeping it on the others via
    `unshare(name, remaining_types)` — or to delete it everywhere, so a shared item
    is never silently lost from a single-type view."""
    label = UNIT_TYPE_LABELS.get(active_type, active_type)
    if not is_shared(types):
        resp = QMessageBox.question(
            parent, f"Delete {kind}",
            f"Delete {kind} '{name}' from the {label} library?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return "delete" if resp == QMessageBox.StandardButton.Yes else "cancel"

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(f"Delete shared {kind}")
    box.setText(f"'{name}' is a shared {kind} — it applies to every unit type.")
    box.setInformativeText(f"Remove it from {label} only (keep it on the other "
                           "unit types), or delete it everywhere?")
    remove_btn = box.addButton(f"Remove from {label} only",
                               QMessageBox.ButtonRole.AcceptRole)
    delete_btn = box.addButton("Delete everywhere",
                               QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(remove_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked is remove_btn:
        try:
            unshare(name, types_without(active_type))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(parent, "Could not update scope", str(exc))
            return "cancel"
        return "unshared"
    if clicked is delete_btn:
        return "delete"
    return "cancel"


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
