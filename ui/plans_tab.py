"""
PlansTab — the top-level Plans tab.

A plan is a cross-unit choreography: sequences from several units armed together
at one shared on-air time, each optionally running with per-task parameter
overrides. Plans live only in the GUI (state.PlanStore → plans.json); arming one
fans out a single arm per item, stamped with the plan's id/name so the resulting
runs can be regrouped for monitoring and stopped together.

  - New / Edit / Delete → manage plan definitions (PlanEditorDialog + PlanStore)
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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from state import PlanStore
from .plan_editor import PlanEditorDialog
from .qt_adapter import DataHub
from .theme import Palette
from .widgets import StatusPill

ARM_MARGIN_S = 5.0
CLOCK_WARN_SKEW_S = 1.0
_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING)


def _lead_in(seq: m.Sequence) -> float:
    """Warm-up lead-in for a sequence: the magnitude of its most-negative
    start-anchored offset (0 if none)."""
    starts = [s.offset_s for s in seq.steps if s.anchor == "start"]
    return max(0.0, -min(starts)) if starts else 0.0


def _arm_plan(fleet: Fleet, plan: m.Plan, on_air_at_iso: str) -> List[tuple]:
    """Arm every item of a plan at the shared on-air time. Worker thread.
    Returns [(item, SequenceRun|None, error|None), ...]."""
    out = []
    for item in plan.items:
        req = m.ArmSequenceRequest(
            on_air_at=on_air_at_iso,
            open_ended=True,
            plan_id=plan.id,
            plan_name=plan.name,
            step_overrides=item.overrides,
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


class _PlanRow(QFrame):
    """One plan: name, unit/sequence summary, active-run pill, action buttons."""

    def __init__(self, plan: m.Plan, active_n: int,
                 on_arm, on_stop, on_edit, on_delete):
        super().__init__()
        self.plan = plan
        self.setObjectName("card")
        active = active_n > 0

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

        word = f"{active_n} on air" if active else "idle"
        self._pill = StatusPill(word, "running" if active else "idle")
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

        # Resolve each item's sequence to compute the longest warm-up lead-in.
        seq_by: Dict[str, Dict[str, m.Sequence]] = {}
        for host, val in (seqs or {}).items():
            if isinstance(val, list):
                seq_by[host] = {s.id: s for s in val}
        max_lead = 0.0
        missing_seq = []
        for item in plan.items:
            seq = seq_by.get(item.hostname, {}).get(item.sequence_id)
            if seq is None:
                missing_seq.append(item.unit_label or item.hostname)
                continue
            max_lead = max(max_lead, _lead_in(seq))
        if missing_seq:
            QMessageBox.warning(
                self, "Cannot arm plan",
                "These units no longer have the plan's sequence:\n" + ", ".join(missing_seq))
            self._set_status("arm cancelled", error=True)
            return

        on_air_at = datetime.now(timezone.utc) + timedelta(seconds=max_lead + ARM_MARGIN_S)
        local = on_air_at.astimezone().strftime("%H:%M:%S")

        skew_note = ""
        if max_skew is not None and max_skew > CLOCK_WARN_SKEW_S:
            skew_note = (f"\n\n⚠ Unit clocks differ by {max_skew:.1f}s. A shared on-air "
                         f"time depends on synced clocks — units may go on air up to "
                         f"{max_skew:.1f}s apart.")

        if QMessageBox.question(
            self, "Arm plan",
            f"Arm {len(plan.items)} sequence(s) across "
            f"{len({i.hostname for i in plan.items})} unit(s)?\n\n"
            f"Shared on-air (T0): {local} local  "
            f"(now + {max_lead:.0f}s warm-up + {ARM_MARGIN_S:.0f}s margin)."
            f"{skew_note}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            self._set_status("arm cancelled")
            return

        on_air_iso = on_air_at.isoformat()
        self._set_status(f"arming {plan.name or plan.id}…")
        self.hub.run_async(
            f"plan_arm:{plan.id}",
            lambda: _arm_plan(self.fleet, plan, on_air_iso),
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
        ok = [it for it, run, err in result if err is None]
        bad = [(it, err) for it, run, err in result if err is not None]
        if bad:
            lines = "\n".join(f"• {it.unit_label or it.hostname} / {it.sequence_name}: {err}"
                              for it, err in bad)
            QMessageBox.warning(
                self, "Arm — partial",
                f"Armed {len(ok)} of {len(result)} sequence(s).\n\nFailed:\n{lines}")
            self._set_status("arm: some units failed", error=True)
        else:
            self._set_status(f"armed {len(ok)} sequence(s)")

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
            active_n = len(self._active_runs_for(plan))
            if active_n:
                active_total += 1
            self._list.addWidget(_PlanRow(
                plan, active_n,
                on_arm=self._on_arm, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete))
        suffix = f" · {active_total} active" if active_total else ""
        self._set_status(f"{len(self._plans)} plan(s){suffix}")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
