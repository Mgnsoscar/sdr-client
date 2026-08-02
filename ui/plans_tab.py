"""
PlansTab — the top-level Plans tab.

A plan is a cross-unit choreography: sequences from several units armed together
at one shared on-air time, each optionally running with per-task parameter
overrides. Plans live only in the GUI (state.PlanStore → plans.json); arming one
fans out a single arm per item, stamped with the plan's id/name so the resulting
runs can be regrouped for monitoring and stopped together.

  - New / Edit / Delete → manage plan definitions (PlanEditorDialog + PlanStore)
  - Export / Import      → move plan definitions between machines as YAML
  - Arm    → put every item on air at one shared T0 (now + the longest warm-up
             lead-in + margin), after a clock-skew pre-flight
  - Stop   → cancel/abort every active run belonging to the plan

Live run state is derived from Fleet.list_runs_all() grouped by plan_id, refreshed
on demand and whenever a sequence lifecycle event arrives.

All network calls go through the DataHub's run_async; results arrive on the shared
task_done signal, filtered here by operation label:
    plan_runs
    plan_preflight:<plan_id>
    plan_arm:<plan_id>
    plan_stop:<plan_id>
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from state import PlanStore, new_plan_id
from .plan_editor import PlanEditorDialog
from .qt_adapter import DataHub
from .theme import Palette
from .widgets import StatusPill

ARM_MARGIN_S = 5.0
CLOCK_WARN_SKEW_S = 1.0
_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING)


def _lead_in(steps) -> float:
    """Warm-up lead-in for a step list: the magnitude of its most-negative
    start-anchored offset (0 if none)."""
    starts = [s.offset_s for s in steps if s.anchor == "start"]
    return max(0.0, -min(starts)) if starts else 0.0


def _parse_iso(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_on_air(run: m.SequenceRun) -> bool:
    """True once the run has actually crossed its on-air time (T0) — i.e. RF is
    live, not merely warming up.

    The agent stamps on_air_actual the moment it crosses T0 on its own clock, so
    this is exact and skew-proof: a RUNNING run still in its warm-up (before T0)
    has on_air_actual unset and reads as armed, and we never compare the laptop's
    clock to a unit-clock on_air_at."""
    return run.state == m.SequenceState.RUNNING and bool(run.on_air_actual)


def plans_to_yaml(plans: List[m.Plan]) -> str:
    """Serialize plans to a portable YAML document ({plans: [...]}).

    The local plan id is dropped (a fresh one is minted on import); each item
    keeps its unit + sequence references and overrides so the plan re-creates
    wherever those units and sequences exist.
    """
    docs = []
    for p in plans:
        docs.append({
            "name": p.name,
            "description": p.description,
            "items": [it.model_dump(mode="json") for it in p.items],
        })
    return yaml.safe_dump({"plans": docs}, sort_keys=False, allow_unicode=True)


def _plan_t0(fleet: Fleet, plan: m.Plan, item_leads: Dict[int, float],
             margin: float) -> datetime:
    """Compute the plan's on-air anchor (T0) at arm time, so it can't drift into
    the past while a confirm dialog is open. Each sequence goes on air at
    T0 + its on_air_offset, and its earliest step fires that much earlier still by
    its warm-up lead. Reading each unit's OWN clock (the agent schedules against
    it), T0 must satisfy, for every item: T0 + offset - lead > that unit's now —
    i.e. T0 = max over items of (unit now + lead - offset) + margin. Falls back to
    the laptop clock for any unit whose /system is unavailable."""
    laptop_now = datetime.now(timezone.utc)
    now_by_host: Dict[str, datetime] = {}
    for host in {it.hostname for it in plan.items}:
        try:
            health = fleet.get(host).system()
            now_by_host[host] = _parse_iso(getattr(health, "utc_now", None)) or laptop_now
        except Exception:  # noqa: BLE001 — best-effort; fall back to the laptop clock
            now_by_host[host] = laptop_now
    required = laptop_now
    for i, item in enumerate(plan.items):
        base = now_by_host.get(item.hostname, laptop_now)
        eff_lead = item_leads.get(i, 0.0) - item.on_air_offset_s
        cand = base + timedelta(seconds=eff_lead)
        if cand > required:
            required = cand
    return required + timedelta(seconds=margin)


def _arm_plan(fleet: Fleet, plan: m.Plan, item_leads: Dict[int, float],
              margin: float) -> List[tuple]:
    """Arm every item of a plan around one shared on-air anchor (T0), computed here
    (not before a confirm dialog) so it stays in the future. Each sequence is armed
    at T0 + its on_air_offset, so a plan can stagger units relative to the anchor.
    Worker thread. Returns [(item, SequenceRun|None, error|None), ...]."""
    t0 = _plan_t0(fleet, plan, item_leads, margin)
    out = []
    for item in plan.items:
        on_air_at_iso = (t0 + timedelta(seconds=item.on_air_offset_s)).isoformat()
        req = m.ArmSequenceRequest(
            on_air_at=on_air_at_iso,
            open_ended=True,
            plan_id=plan.id,
            plan_name=plan.name,
            # A plan-local step copy runs as-is; older items fall back to the stored
            # sequence with legacy per-arg overrides.
            steps=(item.steps or None),
            step_overrides=([] if item.steps else item.overrides),
        )
        try:
            run = fleet.get(item.hostname).arm_sequence(item.sequence_id, req)
            out.append((item, run, None))
        except Exception as exc:  # noqa: BLE001 — reported per item
            out.append((item, None, str(exc)))
    return out


def _stop_plan(fleet: Fleet, runs: List[tuple]) -> List[tuple]:
    """Cancel/abort each (hostname, run_id) of a plan. Worker thread.
    Returns [(run_id, error|None), ...]."""
    out = []
    for hostname, run_id in runs:
        try:
            fleet.get(hostname).cancel_sequence_run(run_id)
            out.append((run_id, None))
        except Exception as exc:  # noqa: BLE001 — reported per run
            out.append((run_id, str(exc)))
    return out


class _ArmConfirmDialog(QDialog):
    """Confirm arming a plan, with a live-updating estimate of the shared on-air
    time. T0 is computed at arm time as now + the longest warm-up + margin, so the
    displayed clock time slides forward with real time — it tracks (roughly) what
    the operator will actually get when they press Arm, however long they wait."""

    def __init__(self, n_seqs: int, n_units: int, max_lead: float, margin: float,
                 skew_note: str, parent=None):
        super().__init__(parent)
        self._max_lead = max_lead
        self._margin = margin
        self.setWindowTitle("Arm plan")
        self.setMinimumWidth(420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        head = QLabel(f"Arm {n_seqs} sequence(s) across {n_units} unit(s)?")
        head.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {Palette.TEXT};")
        head.setWordWrap(True)
        outer.addWidget(head)

        self._on_air = QLabel()
        self._on_air.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(self._on_air)

        detail = QLabel(f"Shared on-air (T0) = now + {max_lead:.0f}s warm-up + "
                        f"{margin:.0f}s margin, computed the moment you press Arm.")
        detail.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        detail.setWordWrap(True)
        outer.addWidget(detail)

        if skew_note:
            warn = QLabel(skew_note.strip())
            warn.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
            warn.setWordWrap(True)
            outer.addWidget(warn)

        buttons = QDialogButtonBox()
        arm = buttons.addButton("Arm", QDialogButtonBox.ButtonRole.AcceptRole)
        arm.setObjectName("primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(250)
        self._tick()

    def _tick(self) -> None:
        est = datetime.now(timezone.utc) + timedelta(seconds=self._max_lead + self._margin)
        self._on_air.setText(f"~ {est.astimezone().strftime('%H:%M:%S')} local")


class _PlanRow(QFrame):
    """One plan: name, unit/sequence summary, active-run pill, action buttons."""

    def __init__(self, plan: m.Plan, on_air_n: int, pending_n: int,
                 on_arm, on_stop, on_edit, on_delete):
        super().__init__()
        self.plan = plan
        self.setObjectName("card")
        active = (on_air_n + pending_n) > 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout(); box.setSpacing(2)
        title = QLabel(plan.name or plan.id)
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(title)
        if plan.description:
            desc = QLabel(plan.description)
            desc.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(desc)
        units = ", ".join(i.unit_label or i.hostname for i in plan.items) or "no units"
        n_ov = sum(len(i.overrides) for i in plan.items)
        ov_txt = f"  ·  {n_ov} override(s)" if n_ov else ""
        summary = QLabel(f"{len(plan.items)} sequence(s)  ·  {units}{ov_txt}")
        summary.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        summary.setWordWrap(True)
        box.addWidget(summary)
        lay.addLayout(box, stretch=1)

        # On air (RF live) beats armed/warming: show whichever phase the plan is in.
        if on_air_n:
            word, status = f"{on_air_n} on air", "running"
        elif pending_n:
            word, status = f"{pending_n} armed", "armed"
        else:
            word, status = "idle", "idle"
        self._pill = StatusPill(word, status)
        lay.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignTop)

        self._arm = QPushButton("Arm")
        self._stop = QPushButton("Stop")
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        for b in (self._arm, self._stop, self._edit, self._delete):
            b.setFixedWidth(66)
        self._arm.setToolTip("Arm every sequence in this plan at one shared on-air time")
        self._stop.setToolTip("Stop every active run in this plan")
        self._arm.setEnabled(not active and bool(plan.items))
        self._stop.setEnabled(active)
        self._delete.setEnabled(not active)
        self._arm.clicked.connect(lambda: on_arm(plan))
        self._stop.clicked.connect(lambda: on_stop(plan))
        self._edit.clicked.connect(lambda: on_edit(plan))
        self._delete.clicked.connect(lambda: on_delete(plan))
        for b in (self._arm, self._stop, self._edit, self._delete):
            lay.addWidget(b, alignment=Qt.AlignmentFlag.AlignTop)


class PlansTab(QWidget):
    def __init__(self, fleet: Fleet, hub: DataHub, parent=None):
        super().__init__(parent)
        self.fleet = fleet
        self.hub = hub
        self._store = PlanStore()
        self._plans: List[m.Plan] = []
        self._runs_by_host: Dict[str, List[m.SequenceRun]] = {}
        self._runs_pending = False
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self.hub.event_received.connect(self._on_event)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        self._new_btn = QPushButton("New plan")
        self._new_btn.setObjectName("primary")
        self._new_btn.clicked.connect(self._on_new)
        row.addWidget(self._new_btn)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        self._export_btn = QPushButton("Export…")
        self._export_btn.setToolTip("Save all plans to a YAML file")
        self._export_btn.clicked.connect(self._on_export)
        row.addWidget(self._export_btn)
        self._import_btn = QPushButton("Import…")
        self._import_btn.setToolTip("Add plans from a YAML file (existing names are skipped)")
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._import_btn)
        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)
        row.addStretch(1)
        outer.addLayout(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._list.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        outer.addWidget(scroll, stretch=1)

    # ── Shown / refresh ────────────────────────────────────────────────────────

    def on_shown(self) -> None:
        # Reload from disk: a Restore/Deploy from the Library tab writes the same
        # plans file, so re-read it rather than trusting our cached copy.
        self._store.load()
        self._plans = self._store.plans()
        self._rebuild()
        self._refresh_runs()

    def _refresh(self) -> None:
        self._plans = self._store.plans()
        self._refresh_runs()
        self._rebuild()

    def _refresh_runs(self) -> None:
        if self._runs_pending or len(self.fleet) == 0:
            return
        self._runs_pending = True
        self.hub.run_async("plan_runs", lambda: self.fleet.list_runs_all())

    # ── Active-run grouping ────────────────────────────────────────────────────

    def _active_runs_for(self, plan: m.Plan) -> List[tuple]:
        """[(hostname, run_id), ...] of this plan's armed/running runs."""
        out = []
        for host, runs in self._runs_by_host.items():
            for r in runs:
                if r.plan_id == plan.id and r.state in _ACTIVE:
                    out.append((host, r.id))
        return out

    def _active_run_objs(self, plan: m.Plan) -> List[m.SequenceRun]:
        """This plan's armed/running SequenceRun objects (for phase counting)."""
        return [r for runs in self._runs_by_host.values() for r in runs
                if r.plan_id == plan.id and r.state in _ACTIVE]

    # ── Plan CRUD ──────────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        dlg = PlanEditorDialog(self.hub, parent=self.window())
        if dlg.exec() and dlg.result_plan is not None:
            self._store.upsert(dlg.result_plan)
            self._refresh()

    def _on_edit(self, plan: m.Plan) -> None:
        dlg = PlanEditorDialog(self.hub, plan=plan, parent=self.window())
        if dlg.exec() and dlg.result_plan is not None:
            self._store.upsert(dlg.result_plan)
            self._refresh()

    def _on_delete(self, plan: m.Plan) -> None:
        if QMessageBox.question(
            self, "Delete plan",
            f"Delete plan '{plan.name or plan.id}'?\nThis only removes the local "
            f"definition; it does not touch the units.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.delete(plan.id)
        self._refresh()

    # ── Export / import (move plans between machines) ──────────────────────────

    def _on_export(self) -> None:
        plans = self._store.plans()
        if not plans:
            QMessageBox.information(self, "Export", "There are no plans to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export plans", "plans.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(plans_to_yaml(plans))
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
            return
        QMessageBox.information(self, "Export", f"{len(plans)} plan(s) written to\n{path}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import plans", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read file:\n{exc}")
            return
        if isinstance(doc, dict):
            raw = doc.get("plans") or []
        elif isinstance(doc, list):
            raw = doc
        else:
            raw = []

        existing = {p.name for p in self._store.plans()}
        to_add: List[m.Plan] = []
        skipped: List[str] = []
        bad: List[str] = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            if name in existing:
                skipped.append(name)
                continue
            try:
                items = [m.PlanItem(**it) for it in (entry.get("items") or [])]
                plan = m.Plan(id=new_plan_id(), name=name,
                              description=entry.get("description", "") or "", items=items)
            except Exception as exc:  # noqa: BLE001 — malformed item/override schema
                bad.append(f"• {name}: {exc}")
                continue
            to_add.append(plan)
            existing.add(name)

        if not to_add:
            msg = "No new plans found in that file."
            if skipped:
                msg += f"\n\n{len(skipped)} plan(s) skipped (name already exists)."
            if bad:
                msg += "\n\n" + "\n".join(bad)
            QMessageBox.information(self, "Import", msg)
            return

        if QMessageBox.question(
            self, "Import plans",
            f"Add {len(to_add)} plan(s)?"
            + (f"\n{len(skipped)} with an existing name will be skipped." if skipped else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return
        for plan in to_add:
            self._store.upsert(plan)
        self._refresh()
        summary = f"Imported {len(to_add)} plan(s)."
        if skipped:
            summary += f"\nSkipped {len(skipped)} (name already exists)."
        if bad:
            summary += "\n\nCould not read:\n" + "\n".join(bad)
        QMessageBox.information(self, "Import complete", summary)

    # ── Arm (shared on-air, clock-skew pre-flight) ─────────────────────────────

    def _on_arm(self, plan: m.Plan) -> None:
        missing = [i for i in plan.items if i.hostname not in self.fleet]
        if missing:
            names = ", ".join(i.unit_label or i.hostname for i in missing)
            QMessageBox.warning(self, "Cannot arm plan",
                                f"These units are not in the fleet: {names}")
            return
        self._set_status(f"pre-flight for {plan.name or plan.id}…")
        hostnames = sorted({i.hostname for i in plan.items})
        self.hub.run_async(
            f"plan_preflight:{plan.id}",
            lambda: (self.fleet.clock_skew(hostnames), self.fleet.sequences_all(hostnames)),
        )

    def _finish_arm_preflight(self, plan: m.Plan, result) -> None:
        try:
            (systems, max_skew), seqs = result
        except (TypeError, ValueError):
            self._set_status("pre-flight failed", error=True)
            QMessageBox.warning(self, "Pre-flight failed", f"{result}")
            return

        # Resolve each item's sequence to its warm-up lead-in (keyed by item index,
        # since the shared on-air is computed per-item at arm time).
        seq_by: Dict[str, Dict[str, m.Sequence]] = {}
        for host, val in (seqs or {}).items():
            if isinstance(val, list):
                seq_by[host] = {s.id: s for s in val}
        item_leads: Dict[int, float] = {}
        max_eff_lead = 0.0   # how far before T0 the earliest step fires, across items
        missing_seq = []
        for i, item in enumerate(plan.items):
            # A plan-local copy carries its own steps; otherwise the source
            # sequence must still exist on the unit.
            if item.steps:
                steps = item.steps
            else:
                seq = seq_by.get(item.hostname, {}).get(item.sequence_id)
                if seq is None:
                    missing_seq.append(item.unit_label or item.hostname)
                    continue
                steps = seq.steps
            lead = _lead_in(steps)
            item_leads[i] = lead
            # A sequence placed later (on_air_offset > 0) needs less head-room before
            # the plan anchor; one placed earlier needs more.
            max_eff_lead = max(max_eff_lead, lead - item.on_air_offset_s)
        if missing_seq:
            QMessageBox.warning(
                self, "Cannot arm plan",
                "These units no longer have the plan's sequence:\n" + ", ".join(missing_seq))
            self._set_status("arm cancelled", error=True)
            return

        skew_note = ""
        if max_skew is not None and max_skew > CLOCK_WARN_SKEW_S:
            skew_note = (f"⚠ Unit clocks differ by {max_skew:.1f}s. A shared on-air "
                         f"time depends on synced clocks — units may go on air up to "
                         f"{max_skew:.1f}s apart.")

        n_units = len({i.hostname for i in plan.items})
        dlg = _ArmConfirmDialog(len(plan.items), n_units, max(0.0, max_eff_lead),
                                ARM_MARGIN_S, skew_note, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("arm cancelled")
            return

        self._set_status(f"arming {plan.name or plan.id}…")
        self.hub.run_async(
            f"plan_arm:{plan.id}",
            lambda: _arm_plan(self.fleet, plan, item_leads, ARM_MARGIN_S),
        )

    def _on_stop(self, plan: m.Plan) -> None:
        runs = self._active_runs_for(plan)
        if not runs:
            self._refresh_runs()
            return
        self._set_status(f"stopping {plan.name or plan.id}…")
        self.hub.run_async(
            f"plan_stop:{plan.id}",
            lambda: _stop_plan(self.fleet, runs),
        )

    # ── Live events ────────────────────────────────────────────────────────────

    def _on_event(self, ev) -> None:
        if isinstance(ev, m.SequenceWebhook):
            self._refresh_runs()

    # ── Result routing ─────────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        if label == "plan_runs":
            self._runs_pending = False
            by_host: Dict[str, List[m.SequenceRun]] = {}
            if isinstance(result, dict):
                for host, val in result.items():
                    by_host[host] = val if isinstance(val, list) else []
            self._runs_by_host = by_host
            self._rebuild()
            return
        if not label.startswith("plan_") or ":" not in label:
            return
        op, plan_id = label.split(":", 1)
        plan = self._store.get(plan_id)

        if op == "plan_preflight":
            if plan is None:
                return
            self._finish_arm_preflight(plan, result)
        elif op == "plan_arm":
            self._report_arm(result)
            self._refresh_runs()
        elif op == "plan_stop":
            if isinstance(result, list):
                bad = [(rid, e) for rid, e in result if e is not None]
                if bad:
                    QMessageBox.warning(self, "Stop — some runs failed",
                                        "\n".join(f"• {rid}: {e}" for rid, e in bad))
                    self._set_status("stop: some runs failed", error=True)
                else:
                    self._set_status("stopped")
            elif isinstance(result, Exception):
                self._set_status(f"stop failed: {result}", error=True)
            self._refresh_runs()

    def _report_arm(self, result) -> None:
        if isinstance(result, Exception) or not isinstance(result, list):
            self._set_status("arm failed", error=True)
            QMessageBox.warning(self, "Arm failed", f"{result}")
            return
        ok = [(it, run) for it, run, err in result if err is None]
        bad = [(it, err) for it, run, err in result if err is not None]
        # Show the actual shared on-air (T0) the units were armed to.
        on_air = ""
        for _it, run in ok:
            dt = _parse_iso(getattr(run, "on_air_at", None))
            if dt is not None:
                on_air = f", on air {dt.astimezone().strftime('%H:%M:%S')}"
                break
        if bad:
            lines = "\n".join(f"• {it.unit_label or it.hostname} / {it.sequence_name}: {err}"
                              for it, err in bad)
            QMessageBox.warning(
                self, "Arm — partial",
                f"Armed {len(ok)} of {len(result)} sequence(s).\n\nFailed:\n{lines}")
            self._set_status("arm: some units failed", error=True)
        else:
            self._set_status(f"armed {len(ok)} sequence(s){on_air}")

    # ── Rendering ──────────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        while self._list.count():
            w = self._list.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

        if not self._plans:
            empty = QLabel("No plans yet. Click “New plan” to combine sequences from "
                           "several units into one coordinated on-air.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            empty.setWordWrap(True)
            self._list.addWidget(empty)
            self._set_status("")
            return

        active_total = 0
        for plan in self._plans:
            runs = self._active_run_objs(plan)
            on_air_n = sum(1 for r in runs if _is_on_air(r))
            pending_n = len(runs) - on_air_n
            if runs:
                active_total += 1
            self._list.addWidget(_PlanRow(
                plan, on_air_n, pending_n,
                on_arm=self._on_arm, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete))
        suffix = f" · {active_total} active" if active_total else ""
        self._set_status(f"{len(self._plans)} plan(s){suffix}")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
