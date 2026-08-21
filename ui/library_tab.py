"""
LibraryTab — the shared definition library.

One fleet-wide set of scripts, tasks, and sequences: the authoring source the
sequence/plan editors read from, and — later — the set deployed to every unit.
Per-unit differences are parameters and live in plans, not here.

The tab has a library-wide header (populate/move the whole library) over the same
editing panels the unit card uses, repointed at the offline library instead of a
live unit (fleet.get("__library__") → LibraryClient):

    header:  Pull from unit… · Import… · Export…   +   integrity status
    sub-tabs:  Tasks  |  Sequences  |  Scripts

Because those panels talk to the LibraryClient, all authoring — including a
script's auto-generated parameter form — works with no unit connected. The Tasks
and Sequences panels run in "library mode": definition editing only, no
Start/Stop/run-state (a library isn't a running unit).

Operation labels routed in _on_task_done:
    lib_pull:<hostname>
"""
from __future__ import annotations

import json
from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from api import models as m
from api.client import AgentConnectionError
from api.fleet import LIBRARY_HOST
from config import UNIT_TYPES, UNIT_TYPE_LABELS, DEFAULT_UNIT_TYPE
from state import (
    LibraryStore, PlanStore, ScheduleStore, pull_library, pull_everything,
    diff_state, UnitSnapshot,
)
from .library_panels import LibraryTasksPanel
from .plans_tab import PlansTab
from .qt_adapter import DataHub
from .scripts_panel import ScriptsPanel
from .sequences_panel import SequencesPanel
from .theme import Palette


class LibraryTab(QWidget):
    SUBTABS = ["Tasks", "Sequences", "Scripts", "Plans"]

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        # Share the one store the fleet's LibraryClient wraps, so this header (its
        # counts / integrity) and the editing panels (via the client) read and
        # write the same library. Falls back to a private store only if the fleet
        # has none registered (e.g. an isolated test harness).
        self._store = self.hub.fleet.library_store() or LibraryStore()
        # Canonical plans + schedule live in these stores (same files the Plans and
        # Timeline tabs use). They are replicated to the units alongside the library
        # so a fresh PC can restore everything from unit IPs alone.
        self._plan_store = PlanStore()
        self._sched_store = ScheduleStore()
        self._pull_pending = False
        self._deploy_pending = False
        self._restore_mode = "replace"   # "merge" | "replace", chosen per Restore
        self._drift = {}          # hostname -> LibraryDiff (last check)
        self._drift_err = {}      # hostname -> error string (unreachable / failed)
        # The library is one store presented as a per-unit-type view: the Tasks /
        # Sequences / Scripts sub-tabs show the slice for the selected type (its own
        # items + shared ones), and new items are scoped to it automatically.
        self._active_type = DEFAULT_UNIT_TYPE
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        # Re-check a unit's definitions against the canonical library whenever it
        # (re)connects, and — if enabled — reconcile it. Definition-only, so this
        # never disturbs a live broadcast.
        self.hub.stream_status.connect(self._on_stream_status)
        self._set_status()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        row = QHBoxLayout()
        title = QLabel("Library")
        title.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {Palette.TEXT};")
        row.addWidget(title)
        row.addSpacing(12)
        self._pull_btn = QPushButton("Restore from unit…")
        self._pull_btn.setObjectName("primary")
        self._pull_btn.setToolTip("Rebuild this PC from a unit: replace the local "
                                  "library, plans and schedule with the unit's copy")
        self._pull_btn.clicked.connect(self._on_pull)
        row.addWidget(self._pull_btn)
        self._deploy_btn = QPushButton("Deploy to units…")
        self._deploy_btn.setToolTip("Push this library to the units so they all hold "
                                    "the same definitions (updates definitions only — "
                                    "never interrupts a live broadcast)")
        self._deploy_btn.clicked.connect(self._on_deploy)
        row.addWidget(self._deploy_btn)
        self._import_btn = QPushButton("Import…")
        self._import_btn.setToolTip("Load a library from a JSON file (replaces the current one)")
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._import_btn)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setToolTip("Save the whole library to a JSON file")
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)
        self._check_btn = QPushButton("Check units")
        self._check_btn.setToolTip("Compare every unit's definitions to this library "
                                   "and report which have drifted")
        self._check_btn.clicked.connect(self._on_check_units)
        row.addWidget(self._check_btn)
        row.addStretch(1)
        self._auto_reconcile = QCheckBox("Auto-reconcile on reconnect")
        self._auto_reconcile.setToolTip(
            "When a unit reconnects, automatically push additions and updates from "
            "this library to it (never removes anything, never interrupts a run). "
            "Leave off to only be notified of drift.")
        row.addWidget(self._auto_reconcile)
        outer.addLayout(row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._units_status = QLabel("")
        self._units_status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        self._units_status.setWordWrap(True)
        self._units_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self._units_status)

        # Unit-type selector — the library is viewed one unit kind at a time. Drives
        # the Tasks / Sequences / Scripts sub-tabs (Plans are cross-unit, so it's
        # hidden there). New items in a type view are scoped to that type by default.
        self._type_bar = QWidget()
        typerow = QHBoxLayout(self._type_bar)
        typerow.setContentsMargins(0, 0, 0, 0)
        typerow.setSpacing(6)
        lbl = QLabel("Library for")
        lbl.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
        typerow.addWidget(lbl)
        self._type_buttons: List[QPushButton] = []
        for t in UNIT_TYPES:
            b = QPushButton(UNIT_TYPE_LABELS.get(t, t))
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setToolTip(f"Show and manage the {UNIT_TYPE_LABELS.get(t, t)} library "
                         "(its own items + shared ones).")
            b.clicked.connect(lambda _c, ut=t: self._set_active_type(ut))
            typerow.addWidget(b)
            self._type_buttons.append(b)
        typerow.addStretch(1)
        outer.addWidget(self._type_bar)

        # Sub-tab bar
        subbar = QHBoxLayout()
        subbar.setSpacing(6)
        self._subtab_buttons: List[QPushButton] = []
        for i, name in enumerate(self.SUBTABS):
            b = QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.clicked.connect(lambda _c, idx=i: self._select_subtab(idx))
            subbar.addWidget(b)
            self._subtab_buttons.append(b)
        subbar.addStretch(1)
        outer.addLayout(subbar)

        # Editing panels, repointed at the library (LIBRARY_HOST), definition-only.
        self._stack = QStackedWidget()
        self._tasks_panel = LibraryTasksPanel(self.hub)
        self._sequences_panel = SequencesPanel(LIBRARY_HOST, self.hub,
                                               can_edit=True, can_run=False)
        self._scripts_panel = ScriptsPanel(LIBRARY_HOST, self.hub)
        self._plans_panel = PlansTab(self.hub.fleet, self.hub)
        self._stack.addWidget(self._tasks_panel)        # 0 Tasks
        self._stack.addWidget(self._sequences_panel)    # 1 Sequences
        self._stack.addWidget(self._scripts_panel)      # 2 Scripts
        self._stack.addWidget(self._plans_panel)        # 3 Plans
        outer.addWidget(self._stack, stretch=1)
        self._set_active_type(self._active_type)   # seed the type views + buttons
        self._select_subtab(0)

    # ── Unit-type view ───────────────────────────────────────────────────────────

    def _set_active_type(self, unit_type: str) -> None:
        """Point the type-scoped sub-tabs (Tasks / Sequences / Scripts) at one unit
        kind: they show that type's slice and default new items to it."""
        self._active_type = unit_type
        for b, t in zip(self._type_buttons, UNIT_TYPES):
            b.setChecked(t == unit_type)
        for panel in (self._tasks_panel, self._sequences_panel, self._scripts_panel):
            if hasattr(panel, "set_active_type"):
                panel.set_active_type(unit_type)

    def _select_subtab(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        for i, b in enumerate(self._subtab_buttons):
            b.setChecked(i == idx)
        # Plans are cross-unit; the type selector only applies to the definition tabs.
        self._type_bar.setVisible(self.SUBTABS[idx] != "Plans")
        w = self._stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()
        # Header counts / integrity may have changed from edits in the panel we're
        # leaving — reload the store and re-derive them.
        self._store.load()
        self._set_status()

    # ── Shown / refresh ────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._store.load()
        self._set_status()
        w = self._stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()

    def _refresh_panels(self) -> None:
        """Reload every editing panel — used after a whole-library replace (pull /
        import) so all three sub-tabs reflect the new contents, not just the visible
        one."""
        for w in (self._tasks_panel, self._sequences_panel, self._scripts_panel):
            if hasattr(w, "on_shown"):
                w.on_shown()

    def _set_status(self, note: str = "", error: bool = False) -> None:
        n_sc = len(self._store.scripts())
        n_t = len(self._store.tasks())
        n_seq = len(self._store.sequences())
        empty = (n_sc + n_t + n_seq) == 0
        problems = self._store.check_integrity()
        parts = []
        if note:
            parts.append(note)
        if empty:
            parts.append("Library is empty — pull it from a unit, import a file, "
                         "or add definitions in the tabs below.")
        else:
            parts.append(f"{n_sc} script(s) · {n_t} task(s) · {n_seq} sequence(s)")
        if problems:
            parts.append("⚠ " + "; ".join(problems[:3])
                         + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""))
        color = Palette.CRASH if (error or problems) else Palette.TEXT_FAINT
        self._status.setText("   ·   ".join(parts))
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    # ── Pull from a unit ───────────────────────────────────────────────────────

    def _on_pull(self) -> None:
        if self._pull_pending:
            return
        hosts = self.hub.fleet.hostnames()
        if not hosts:
            QMessageBox.information(self, "No units", "No units are configured to pull from.")
            return
        labels = []
        by_label = {}
        for h in hosts:
            try:
                label = self.hub.fleet.get(h).label
            except KeyError:
                label = h
            labels.append(label)
            by_label[label] = h
        if len(labels) == 1:
            hostname = by_label[labels[0]]
        else:
            label, ok = QInputDialog.getItem(
                self, "Pull from unit", "Snapshot the library from:", labels, 0, False)
            if not ok:
                return
            hostname = by_label[label]

        self._plan_store.load()
        self._sched_store.load()
        empty = (not self._store.library().scripts and not self._store.tasks()
                 and not self._plan_store.plans() and not self._sched_store.entries())
        if empty:
            mode = "replace"   # nothing local to lose — merge and replace are the same
        else:
            box = QMessageBox(self)
            box.setWindowTitle("Restore from unit")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"Restore from {self._label(hostname)}?")
            box.setInformativeText(
                "Merge — keep everything on this PC and add what the unit has that "
                "you don't (nothing local is lost).\n\n"
                "Replace — discard this PC's library, plans and schedule and take the "
                "unit's copy exactly (use this to rebuild a blank PC).")
            merge_btn = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
            replace_btn = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(merge_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is merge_btn:
                mode = "merge"
            elif clicked is replace_btn:
                mode = "replace"
            else:
                return

        self._restore_mode = mode
        self._pull_pending = True
        self._set_status(f"restoring from {hostname}…")
        client = self.hub.fleet.get(hostname)
        self.hub.run_async(f"lib_pull:{hostname}", lambda: pull_everything(client))

    # ── Deploy to units ────────────────────────────────────────────────────────

    def _label(self, hostname: str) -> str:
        try:
            return self.hub.fleet.get(hostname).label
        except KeyError:
            return hostname

    def _on_deploy(self) -> None:
        if self._deploy_pending:
            return
        lib = self._store.library()
        # Reload plans/schedule from disk so we replicate what the Plans and
        # Timeline tabs have most recently saved (they use these same files).
        self._plan_store.load()
        self._sched_store.load()
        plans = self._plan_store.plans()
        schedule = self._sched_store.entries()
        if not (lib.scripts or lib.tasks or lib.sequences or plans or schedule):
            QMessageBox.information(self, "Deploy", "Nothing to deploy yet.")
            return
        hosts = self.hub.fleet.hostnames()
        if not hosts:
            QMessageBox.information(self, "Deploy", "No units are configured to deploy to.")
            return

        box = QMessageBox(self)
        box.setWindowTitle("Deploy to units")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Replicate everything to {len(hosts)} unit(s)?")
        box.setInformativeText(
            f"{len(lib.scripts)} script(s) · {len(lib.tasks)} task(s) · "
            f"{len(lib.sequences)} sequence(s) · {len(plans)} plan(s) · "
            f"{len(schedule)} scheduled.\n\n"
            "Library definitions are updated in place — running tasks and active "
            "runs are never interrupted; plans and schedule are stored on each unit "
            "for recovery. Unreachable units are reported and can be redeployed later.")
        blank_scripts = [s.name for s in lib.scripts if not (s.content or "").strip()]
        if blank_scripts:
            box.setInformativeText(
                box.informativeText()
                + f"\n\n⚠ {len(blank_scripts)} script(s) in this library have no "
                  f"content ({', '.join(blank_scripts[:3])}"
                  f"{'…' if len(blank_scripts) > 3 else ''}). Deploying will blank "
                  f"those files on the units — Restore→Merge from a unit that still "
                  f"has them, or re-upload, before deploying.")
        extra_hosts = [self._label(h) for h, d in self._drift.items() if d.unit_has_extra]
        if extra_hosts:
            box.setInformativeText(
                box.informativeText()
                + f"\n\n⚠ These units hold plans/schedule not on this PC and a deploy "
                  f"will overwrite them: {', '.join(extra_hosts)}. Restore from one "
                  f"first if you want to keep them.")
        # Each unit only receives the slice of the library scoped to its Type (shared
        # items + its own kind). Flag any connected unit whose Type would receive
        # NONE of the library's tasks — the silent case where the deploy "succeeds"
        # but that unit gets nothing to run (its Type doesn't match your tasks' scope).
        type_of = {}
        for h in hosts:
            try:
                type_of[h] = self.hub.fleet.get(h).unit_type
            except KeyError:
                type_of[h] = DEFAULT_UNIT_TYPE
        zero = []
        for utype in sorted(set(type_of.values())):
            if lib.tasks and not any(m.applies_to_type(t.types, utype) for t in lib.tasks):
                names = ", ".join(self._label(h) for h in hosts if type_of[h] == utype)
                zero.append(f"{UNIT_TYPE_LABELS.get(utype, utype)} ({names})")
        if zero:
            box.setInformativeText(
                box.informativeText()
                + "\n\n⚠ None of your "
                + f"{len(lib.tasks)} task(s) are scoped to these unit types, so they "
                  "will receive 0 tasks: " + "; ".join(zero) + ".\nFix by setting the "
                  "unit's Type (Units tab → edit the unit) to match, or by marking the "
                  "tasks Shared / authoring them in that type's Library tab.")
        prune = QCheckBox("Prune — remove library definitions the library omits "
                          "(make each unit identical)")
        prune.setChecked(True)
        box.setCheckBox(prune)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        do_prune = prune.isChecked()
        self._deploy_pending = True
        self._set_status(f"deploying to {len(hosts)} unit(s)…")
        self.hub.run_async(
            "lib_deploy",
            lambda: self.hub.fleet.deploy_state_all(lib, plans, schedule, do_prune))

    def _report_deploy(self, result) -> None:
        if not isinstance(result, dict):
            self._set_status(f"deploy failed: {result}", error=True)
            QMessageBox.warning(self, "Deploy failed", f"{result}")
            return
        ok = {h: r for h, r in result.items() if not isinstance(r, Exception)}
        # Offline units were skipped by the reachability gate — that's expected, not
        # a failure. Only *other* exceptions are real deploy failures.
        offline = {h: r for h, r in result.items()
                   if isinstance(r, AgentConnectionError)}
        failed = {h: r for h, r in result.items()
                  if isinstance(r, Exception) and not isinstance(r, AgentConnectionError)}
        lib_has_tasks = bool(self._store.library().tasks)
        notes = []
        zero_task_units = []
        for h, r in ok.items():
            # Tasks the unit now holds = added + unchanged (skipped were left running).
            reload = getattr(r, "tasks_reload", None) or {}
            n_tasks = len(reload.get("added", [])) + len(reload.get("unchanged", []))
            if lib_has_tasks and n_tasks == 0:
                zero_task_units.append(self._label(h))
            parts = []
            if getattr(r, "tasks_skipped", None):
                parts.append(f"tasks kept running: {', '.join(r.tasks_skipped)}")
            if getattr(r, "sequences_skipped", None):
                parts.append(f"sequences with active runs kept: {', '.join(r.sequences_skipped)}")
            if parts:
                notes.append(f"{self._label(h)}: " + "; ".join(parts))
        status = f"deployed to {len(ok)} unit(s)"
        if offline:
            status += f" · {len(offline)} offline"
        if failed:
            status += f" · {len(failed)} failed"
        self._set_status(status, error=bool(failed))
        if failed or notes or zero_task_units:
            lines = []
            if failed:
                lines.append("Failed (redeploy when reachable):")
                lines += [f"• {self._label(h)}: {e}" for h, e in failed.items()]
            if zero_task_units:
                if lines:
                    lines.append("")
                lines.append("⚠ Received 0 tasks (their Type doesn't match your tasks' "
                             "scope — set the unit's Type, or mark tasks Shared):")
                lines += [f"• {h}" for h in zero_task_units]
            if offline:
                if lines:
                    lines.append("")
                lines.append("Skipped — offline (redeploy when back online):")
                lines += [f"• {self._label(h)}" for h in offline]
            if notes:
                if lines:
                    lines.append("")
                lines.append("Left in place because in use (nothing on air was touched):")
                lines += [f"• {n}" for n in notes]
            QMessageBox.warning(self, "Deploy — details", "\n".join(lines))
        elif offline:
            # No real failures — just some units offline. Benign, so inform (not warn).
            skipped = "\n".join(f"• {self._label(h)}" for h in offline)
            QMessageBox.information(
                self, "Deploy complete",
                f"Deployed to {len(ok)} unit(s).\n\n"
                f"{len(offline)} unit(s) skipped — offline (redeploy when back "
                f"online):\n{skipped}")
        else:
            QMessageBox.information(self, "Deploy complete", f"Deployed to {len(ok)} unit(s).")

    # ── Drift detection / reconcile-on-reconnect ───────────────────────────────

    def _on_check_units(self) -> None:
        hosts = self.hub.fleet.hostnames()
        if not hosts:
            QMessageBox.information(self, "Check units", "No units are configured.")
            return
        self._units_status.setText(f"checking {len(hosts)} unit(s)…")
        self.hub.run_async("lib_check", lambda: self.hub.fleet.snapshots_all())

    def _on_stream_status(self, hostname: str, connected: bool) -> None:
        # Only react to a (re)connect, and only for a real unit.
        if not connected or hostname == LIBRARY_HOST or hostname not in self.hub.fleet:
            return
        self.hub.run_async(f"lib_check1:{hostname}",
                           lambda h=hostname: self.hub.fleet.snapshots_all([h]))

    def _ingest_libraries(self, result) -> list:
        """Fold a {host: UnitSnapshot|Exception} result into the drift maps,
        comparing each unit's library + plan + schedule replicas to this PC's
        canonical state. Returns the hostnames that are reachable AND drifted."""
        if not isinstance(result, dict):
            return []
        canon_lib = self._store.library()
        self._plan_store.load()
        self._sched_store.load()
        canon_plans = self._plan_store.plans()
        canon_sched = self._sched_store.entries()
        drifted = []
        for host, val in result.items():
            if isinstance(val, Exception) or not isinstance(val, UnitSnapshot):
                self._drift_err[host] = str(val) if isinstance(val, Exception) else "no snapshot"
                self._drift.pop(host, None)
                continue
            self._drift_err.pop(host, None)
            # Compare against the slice this unit actually gets (shared + its kind),
            # not the whole canonical library — otherwise a broadcaster would look
            # "drifted" for every x410-only item it correctly never receives.
            try:
                utype = self.hub.fleet.get(host).unit_type
            except KeyError:
                utype = m.DEFAULT_UNIT_TYPE
            scoped = m.scoped_library(canon_lib, utype)
            d = diff_state(scoped, canon_plans, canon_sched, val)
            self._drift[host] = d
            if not d.in_sync:
                drifted.append(host)
        self._update_units_status()
        return drifted

    def _update_units_status(self) -> None:
        checked = set(self._drift) | set(self._drift_err)
        if not checked:
            self._units_status.setText("")
            return
        in_sync = [h for h, d in self._drift.items() if d.in_sync]
        drifted = [h for h, d in self._drift.items() if not d.in_sync]
        unreachable = list(self._drift_err)
        parts = [f"Units: {len(in_sync)} in sync"]
        if drifted:
            parts.append(f"{len(drifted)} drifted")
        if unreachable:
            parts.append(f"{len(unreachable)} unreachable")
        extra = [h for h in drifted if self._drift[h].unit_has_extra]
        line = "  ·  ".join(parts)
        if drifted:
            detail = "; ".join(f"{self._label(h)} ({self._drift[h].summary()})"
                               for h in drifted[:4])
            line += f"  —  {detail}" + (" …" if len(drifted) > 4 else "")
        if extra:
            line += (f"    ⚠ {len(extra)} unit(s) hold plans/schedule not on this PC "
                     f"— Restore from unit to recover them.")
        self._units_status.setText(line)
        self._units_status.setStyleSheet(
            f"font-size: 11px; color: {Palette.CRASH if drifted else Palette.TEXT_FAINT};")

    def _maybe_auto_reconcile(self, drifted: list) -> None:
        """If auto-reconcile is on, push LIBRARY additions/updates (never prune) to
        units whose library drifted, so they converge without removing anything or
        touching a live run. Plans and schedule are deliberately NOT auto-pushed: a
        wholesale replace could delete a unit's plans/schedule this PC no longer has
        — that's a Restore situation, surfaced as drift, never overwritten silently."""
        targets = [h for h in drifted
                   if h in self._drift and not self._drift[h].library_in_sync]
        if not targets or not self._auto_reconcile.isChecked() or self._deploy_pending:
            return
        lib = self._store.library()
        self.hub.run_async(
            f"lib_reconcile:{','.join(targets)}",
            lambda hs=list(targets): self.hub.fleet.deploy_all(lib, prune=False, units=hs))

    # ── Pull results ───────────────────────────────────────────────────────────

    def _drift_report_lines(self) -> list:
        """Human-readable per-host drift, for the Check-units dialog. One line per
        resource that differs, in plain language (no +/~/- shorthand)."""
        lines = []
        for host in self.hub.fleet.hostnames():
            label = self._label(host)
            if host in self._drift_err:
                lines.append(f"• {label}: unreachable ({self._drift_err[host]})")
                continue
            d = self._drift.get(host)
            if d is None:
                lines.append(f"• {label}: not checked")
                continue
            if d.in_sync:
                lines.append(f"• {label}: in sync")
                continue

            lines.append(f"• {label}: drifted")

            def add_bucket(kind, added, changed, removed, nmap=None):
                def fmt(ids):
                    return ", ".join(nmap.get(i, i) for i in ids) if nmap else ", ".join(ids)
                if added:
                    lines.append(f"      {kind} missing on the unit: {fmt(added)}")
                if changed:
                    lines.append(f"      {kind} that differ: {fmt(changed)}")
                if removed:
                    lines.append(f"      {kind} on the unit but not on this PC: {fmt(removed)}")

            add_bucket("scripts", d.scripts_add, d.scripts_change, d.scripts_remove)
            add_bucket("tasks", d.tasks_add, d.tasks_change, d.tasks_remove)
            add_bucket("sequences", d.sequences_add, d.sequences_change,
                       d.sequences_remove, d.seq_names)
            add_bucket("plans", d.plans_add, d.plans_change, d.plans_remove, d.plan_names)
            add_bucket("schedule", d.schedule_add, d.schedule_change,
                       d.schedule_remove, d.sched_names)
        return lines

    def _show_drift_details(self) -> None:
        lines = self._drift_report_lines()
        if not lines:
            return
        all_synced = all(h in self._drift and self._drift[h].in_sync
                         for h in self.hub.fleet.hostnames())
        body = "\n".join(lines)
        if all_synced:
            QMessageBox.information(self, "Check units",
                                    "All units are in sync with this PC.\n\n" + body)
        else:
            any_extra = any(d.unit_has_extra for d in self._drift.values())
            body += ("\n\n“Deploy to units…” makes the units match THIS PC "
                     "(updates their definitions in place; replaces their plans/schedule).")
            if any_extra:
                body += ("\n“Restore from unit…” brings a unit's copy back to this PC "
                         "— use it first to recover plans/schedule a unit has that this "
                         "PC doesn't (a deploy would overwrite them).")
            QMessageBox.warning(self, "Check units — drift found", body)

    def _on_task_done(self, label: str, result) -> None:
        if label == "lib_check":
            self._ingest_libraries(result)
            self._show_drift_details()
            return
        if label.startswith("lib_check1:"):
            drifted = self._ingest_libraries(result)
            self._maybe_auto_reconcile(drifted)
            return
        if label.startswith("lib_reconcile:"):
            # A quiet write-through; refresh drift for those hosts so the line settles.
            hosts = label.split(":", 1)[1].split(",")
            if isinstance(result, dict):
                oks = [h for h, r in result.items() if not isinstance(r, Exception)]
                if oks:
                    self.hub.run_async("lib_check_after",
                                       lambda hs=oks: self.hub.fleet.snapshots_all(hs))
            return
        if label == "lib_check_after":
            self._ingest_libraries(result)
            return
        if label == "lib_deploy":
            self._deploy_pending = False
            self._report_deploy(result)
            # Refresh drift after a manual deploy so the units line reflects it.
            hosts = self.hub.fleet.hostnames()
            if hosts:
                self.hub.run_async("lib_check_after",
                                   lambda hs=list(hosts): self.hub.fleet.snapshots_all(hs))
            return
        if not label.startswith("lib_pull:"):
            return
        self._pull_pending = False
        if isinstance(result, Exception) or not isinstance(result, UnitSnapshot):
            self._set_status(f"restore failed: {result}", error=True)
            return
        # Reload the current on-disk plans/schedule so a merge builds on the latest.
        self._plan_store.load()
        self._sched_store.load()
        if self._restore_mode == "merge":
            # Non-destructive: keep everything local, add only what the unit has extra.
            libc = self._store.merge(result.library)
            add_p = self._plan_store.merge(result.plans)
            add_s = self._sched_store.merge(result.schedule)
            refreshed = (f" · recovered {libc['scripts_refreshed']} empty script "
                         f"body(ies)" if libc.get("scripts_refreshed") else "")
            self._set_status(
                f"merged from unit: +{libc['scripts']} script(s) · +{libc['tasks']} "
                f"task(s) · +{libc['sequences']} sequence(s) · +{add_p} plan(s) · "
                f"+{add_s} scheduled{refreshed} (nothing local removed)")
        else:
            self._store.replace(result.library)
            self._plan_store.replace_all(result.plans)
            self._sched_store.replace_all(result.schedule)
            self._set_status(
                f"restored (replaced): {len(result.library.scripts)} script(s) · "
                f"{len(result.library.tasks)} task(s) · {len(result.library.sequences)} "
                f"sequence(s) · {len(result.plans)} plan(s) · "
                f"{len(result.schedule)} scheduled")
        self._refresh_panels()
        # The Plans sub-tab shares the plan file — reload it so it shows the restore.
        if hasattr(self._plans_panel, "on_shown"):
            self._plans_panel._store.load()
            self._plans_panel.on_shown()

    # ── Import / export the whole library ──────────────────────────────────────

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export library", "library.json", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._store.library().model_dump(), fh, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        self._set_status(f"exported to {path}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import library", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lib = m.Library(**json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read file:\n{exc}")
            return
        if QMessageBox.question(
            self, "Replace library",
            f"Import replaces the current library with:\n"
            f"{len(lib.scripts)} script(s), {len(lib.tasks)} task(s), "
            f"{len(lib.sequences)} sequence(s).\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.replace(lib)
        self._refresh_panels()
        self._set_status("imported library")
