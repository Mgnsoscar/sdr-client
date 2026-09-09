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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import Fleet
from api import models as m
from api import ramp as _ramp
from state import PlanStore, new_plan_id
from .arm_dialog import ArmDialog
from .param_form import fmt_duration
from .plan_editor import PlanEditorDialog
from .plan_log_dialog import PlanLogDialog
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_model import (
    SEQUENCE_HOLD_EDIT_CAPABILITY, SEQUENCE_HOLD_NOW_CAPABILITY, SEQUENCE_LOG_TABLE_CAPABILITY,
    hold_runtime_supported)
from .widgets import StatusPill, natural_key

ARM_MARGIN_S = 5.0
CLOCK_WARN_SKEW_S = 1.0
DEFAULT_STOP_DURATION_S = 60.0   # fallback when a plan has no derivable minimum
# Default max-hold deadman offered when arming a Hold-aware plan (30 min; 0 = unlimited).
DEFAULT_MAX_HOLD_S = 1800.0
# Safety lead for a Proceed (mirrors sequences_panel): window B's first step fires at
# resume + its offset ≥ 0, so a small margin covers clock skew and the immediate case.
PROCEED_LEAD_S = 3.0
# A plan run is "active" (blocks re-arm / delete, keeps Stop live) while armed, running, or
# HOLDING (a single-unit hold-aware plan parked at its Hold with RF live, awaiting Proceed).
_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING, m.SequenceState.HOLDING)


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


def _plan_timing(runs: List[m.SequenceRun]) -> str:
    """A one-line 'on air HH:MM:SS · off air HH:MM:SS · duration' digest for a plan's
    active runs: on-air is the earliest run's, off-air the latest, and the plan reads
    open-ended if any run is. Empty string if no run has a parseable on-air time."""
    starts = [d for d in (_parse_iso(r.on_air_at) for r in runs) if d]
    if not starts:
        return ""
    on = min(starts)
    on_txt = on.astimezone().strftime("%H:%M:%S")
    if not all(r.on_air_end and not r.open_ended for r in runs):
        return f"on air {on_txt}  ·  off air —  ·  open-ended"
    off = max(d for d in (_parse_iso(r.on_air_end) for r in runs) if d)
    dur = (off - on).total_seconds()
    return (f"on air {on_txt}  ·  off air {off.astimezone().strftime('%H:%M:%S')}"
            f"  ·  {fmt_duration(round(dur))}")


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


def _plan_has_hold(plan: m.Plan, seqs_by_host: Optional[dict] = None) -> bool:
    """True if any of a plan's items resolves to a Hold-bearing sequence — a plan-local step
    copy, or (when ``seqs_by_host`` — a ``sequences_all`` result ``{host: [Sequence]|exc}`` — is
    supplied) the unit's STORED sequence the item references. Used for the up-front schedule /
    plan 'contains a Hold' notice so it matches what the arm path actually collapses."""
    for item in plan.items:
        if item.steps:
            if m.has_hold(item.steps):
                return True
            continue
        val = (seqs_by_host or {}).get(item.hostname)
        if isinstance(val, list):
            stored = next((s for s in val if s.id == item.sequence_id), None)
            if stored is not None and m.has_hold(stored.steps):
                return True
    return False


def _hold_aware_plan_item(plan: m.Plan, resolved: Dict[int, list],
                          supports_runtime) -> Optional[m.PlanItem]:
    """The single plan item to arm hold_aware (single-unit, operator-present), or None.

    Eligible iff the plan has EXACTLY ONE item, that item's resolved steps contain a Hold, and
    its unit runs the Hold runtime (``supports_runtime(hostname)`` True). Any other Hold-bearing
    plan returns None → the Hold is compiled out and the run passes straight through: more than
    one item (cross-unit Hold synchronisation is deferred — docs/sequence-hold-step.md §10), or a
    unit whose agent is too old for the runtime (§11). ``resolved`` maps item index → its resolved
    (uncollapsed) steps, as built by _finish_arm_preflight."""
    if len(plan.items) != 1:
        return None
    item = plan.items[0]
    steps = resolved.get(0)
    if not (steps and m.has_hold(steps)):
        return None
    return item if supports_runtime(item.hostname) else None


def _apply_step_overrides(steps: List[m.SequenceStep],
                          overrides: List[m.StepOverride]) -> List[m.SequenceStep]:
    """Bake legacy per-index StepOverrides into a step list (mirrors the agent: an override at
    index i REPLACES that step's args + replace_args). Used to preserve a steps-less item's
    overrides when we must send explicit collapsed steps for a stored-sequence Hold."""
    out = list(steps)
    for ov in overrides or []:
        if 0 <= ov.index < len(out):
            out[ov.index] = out[ov.index].model_copy(
                update={"args": list(ov.args), "replace_args": ov.replace_args})
    return out


def _collapsed_arm_steps(fleet: Fleet, item: m.PlanItem):
    """The steps to send for a plan/scheduled arm, with any Hold compiled OUT (§7). A
    plan-local copy (``item.steps``) is collapsed directly; an item with NO plan-local copy
    references the unit's STORED sequence, which is fetched and collapsed ONLY if it contains a
    Hold — so a Hold never reaches the agent without ``hold_aware`` (which the scheduled/plan
    path never sets) and the run passes straight through, as the operator was told. The item's
    legacy per-arg overrides are baked into the stored steps first (they address original
    indices), so they aren't lost when we send explicit collapsed steps. Returns None to fall
    back to the stored sequence + legacy overrides (the no-Hold case, unchanged). Worker thread
    (best-effort: a fetch failure falls back to the stored sequence)."""
    if item.steps:
        return m.collapse_hold(item.steps)
    try:
        stored = fleet.get(item.hostname).get_sequence(item.sequence_id)
    except Exception:  # noqa: BLE001 — best-effort; fall back to the stored sequence as before
        return None
    if stored is not None and m.has_hold(stored.steps):
        return m.collapse_hold(_apply_step_overrides(stored.steps, item.overrides))
    return None


def _proceed_run(client, run_id: str, resume_laptop: datetime,
                 steps: Optional[List[m.SequenceStep]] = None) -> m.SequenceRun:
    """Resume a HOLDING plan run at the operator-chosen instant, translated to the unit's
    clock (like _arm_plan). The agent resolves window B relative to it; `steps` (edit-while-
    holding) re-resolves window B from the edited sequence. Worker thread. Mirrors
    sequences_panel._proceed_run."""
    resume_at = resume_laptop + timedelta(seconds=client.clock_offset_s())
    return client.proceed_sequence_run(
        run_id, m.ProceedRequest(proceed_at=resume_at.isoformat(), steps=steps))


def _arm_plan(fleet: Fleet, plan: m.Plan, t0: datetime,
              duration_s: Optional[float], hold_aware: bool = False,
              max_hold_s: float = DEFAULT_MAX_HOLD_S) -> List[tuple]:
    """Arm every item of a plan around one operator-chosen on-air anchor (T0). Each
    sequence is armed at T0 + its on_air_offset (absolute UTC), so a plan can stagger
    units relative to the anchor. When duration_s is set every sequence runs that
    long from its own on-air (skew-robust, and stop-anchored steps then fire);
    otherwise it's open-ended and runs until stopped. Worker thread.

    hold_aware=True is the single-unit operator-present case (exactly one Hold-bearing item,
    checked by the caller): the item's steps are sent VERBATIM (the Hold intact, not collapsed),
    the arm is hold_aware + open-ended, and the run parks at the Hold awaiting Proceed
    (docs/sequence-hold-step.md §6–§7). hold_aware=False (the default, multi-unit / unattended)
    compiles any Hold OUT so the run passes straight through, and never sends hold_aware (§7).
    Returns [(item, SequenceRun|None, error|None), ...]."""
    out = []
    _offsets: dict = {}   # per-unit clock skew, fetched once per host

    def _off(host: str) -> float:
        if host not in _offsets:
            _offsets[host] = fleet.get(host).clock_offset_s()
        return _offsets[host]

    for item in plan.items:
        # Translate the laptop-UTC anchor to THIS unit's clock (as single-sequence arm
        # does), so a skewed unit still goes on air at the intended wall-clock time.
        on_air_at_iso = (t0 + timedelta(seconds=item.on_air_offset_s + _off(item.hostname))
                         ).isoformat()
        if hold_aware:
            # The Hold is real here: send the steps AS AUTHORED (Hold intact, NOT collapsed). The
            # agent refuses step_overrides alongside a Hold, so a steps-less item's legacy overrides
            # are baked into the stored sequence's steps and sent inline (mirrors the collapse path's
            # override-baking, minus the collapse). If the stored fetch fails, fall back to the
            # stored sequence with no overrides rather than risk an override+Hold refusal.
            if item.steps:
                hold_steps = list(item.steps)
            else:
                try:
                    stored = fleet.get(item.hostname).get_sequence(item.sequence_id)
                except Exception:  # noqa: BLE001 — best-effort; the agent still has the stored seq
                    stored = None
                hold_steps = (_apply_step_overrides(stored.steps, item.overrides)
                              if stored is not None else None)
            req = m.ArmSequenceRequest(
                on_air_at=on_air_at_iso,
                open_ended=True,               # window B is scheduled at Proceed, not now
                on_air_duration_s=None,
                plan_id=plan.id,
                plan_name=plan.name,
                steps=hold_steps,
                step_overrides=[],             # never sent with a Hold (baked in above)
                hold_aware=True,
                max_hold_s=max_hold_s,
            )
        else:
            # A Hold has no effect in a plan run this way (multi-unit / unattended-style):
            # compile it out to a straight-through two-anchor list before arming — for a
            # plan-local copy AND a stored-sequence reference — and never send hold_aware (§7).
            armed_steps = _collapsed_arm_steps(fleet, item)
            req = m.ArmSequenceRequest(
                on_air_at=on_air_at_iso,
                open_ended=(duration_s is None),
                on_air_duration_s=(duration_s if duration_s is not None else None),
                plan_id=plan.id,
                plan_name=plan.name,
                # A plan-local (or collapsed stored) step copy runs as-is; a no-Hold item with
                # no copy falls back to the stored sequence with legacy per-arg overrides.
                steps=(armed_steps or None),
                step_overrides=([] if armed_steps else item.overrides),
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
    """One plan: name, unit/sequence summary, active-run pill, action buttons.

    A single-unit hold-aware plan run (docs/sequence-hold-step.md §6) surfaces the same run
    controls as the Library row: while HOLDING the Arm button becomes Proceed, a RUNNING
    hold-aware run offers Hold-now (Fast-Forward-to-Hold), and a HOLDING run's own Edit button
    retargets the post-hold window before proceeding (rather than opening the plan editor — one
    Edit button, not two). The Hold controls are hidden for every ordinary (multi-unit /
    non-hold) plan."""

    def __init__(self, plan: m.Plan, runs: List[m.SequenceRun], on_air_n: int,
                 pending_n: int, on_arm, on_stop, on_edit, on_delete, on_log,
                 holding: bool = False, can_ff: bool = False, can_edit_wb: bool = False,
                 on_proceed=None, on_hold_now=None, on_edit_wb=None,
                 on_export=None, export_ok: bool = False):
        super().__init__()
        self.plan = plan
        self.setObjectName("card")
        active = (on_air_n + pending_n) > 0 or holding

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout(); box.setSpacing(2)
        title = QLabel(plan.name or plan.id)
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(title)
        if plan.description:
            from .desc_widget import CollapsibleDescription
            box.addWidget(CollapsibleDescription(plan.description))
        # Once armed, show the run's timing (on-air/off-air/duration); otherwise the
        # plan's composition (units + overrides).
        timing = _plan_timing(runs) if active else ""
        if timing:
            summary_text = timing
        else:
            units = ", ".join(i.unit_label or i.hostname for i in plan.items) or "no units"
            n_ov = sum(len(i.overrides) for i in plan.items)
            ov_txt = f"  ·  {n_ov} override(s)" if n_ov else ""
            summary_text = f"{len(plan.items)} sequence(s)  ·  {units}{ov_txt}"
        summary = QLabel(summary_text)
        summary.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        summary.setWordWrap(True)
        box.addWidget(summary)
        # The plan's minimum on-air window — the longest of its sequences' minimums —
        # always visible so it's legible without opening the plan. Derived from each
        # item's plan-local steps; 0 (nothing to show) for legacy step-less items.
        min_dur = max((_ramp.min_on_air_duration(it.steps) for it in plan.items if it.steps),
                      default=0.0)
        if min_dur > 0:
            mind = QLabel(f"min duration  {fmt_duration(round(min_dur))}")
            mind.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(mind)
        lay.addLayout(box, stretch=1)

        # Holding (parked at a Hold, RF live) beats on-air beats armed/warming.
        if holding:
            word, status = "holding", "holding"
        elif on_air_n:
            word, status = f"{on_air_n} on air", "running"
        elif pending_n:
            word, status = f"{pending_n} armed", "armed"
        else:
            word, status = "idle", "idle"
        self._pill = StatusPill(word, status)
        lay.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignTop)

        # While HOLDING the Arm button becomes Proceed (schedule window B / resume).
        self._arm = QPushButton("Proceed" if holding else "Arm")
        self._stop = QPushButton("Stop")
        self._log = QPushButton("Log")
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        # Fast-Forward-to-Hold, shown only when applicable.
        self._hold_now = QPushButton("Hold now")
        self._hold_now.setToolTip("Jump to the Hold now — skip the rest of the run-up and hold the "
                                  "signal at its current value (then Proceed when ready)")
        self._hold_now.setVisible(can_ff)
        # Export a ran log to a spreadsheet (pick one of the plan's last runs).
        can_export = export_ok and on_export is not None
        self._export = QPushButton("Export…")
        self._export.setToolTip("Export a run's log to a spreadsheet — one row per state change, "
                                "every parameter (and power quantity) in its own column; one sheet "
                                "per unit")
        self._export.setVisible(can_export)
        # Minimum (not fixed) width so a longer label ("Proceed" > "Arm") grows instead of clipping.
        for b in (self._arm, self._stop, self._log, self._edit, self._delete):
            b.setMinimumWidth(66)
        self._hold_now.setMinimumWidth(72)
        self._export.setMinimumWidth(66)
        self._arm.setToolTip(
            "Proceed — schedule the post-hold window (the down-ramp) and resume the run"
            if holding else
            "Arm every sequence in this plan at one shared on-air time")
        self._stop.setToolTip("Stop every active run in this plan")
        self._log.setToolTip("View every sequence's run log across the plan's units, live")
        # Proceed is enabled while holding; Arm only when nothing is active.
        self._arm.setEnabled(holding or (not active and bool(plan.items)))
        self._stop.setEnabled(active)
        self._log.setEnabled(bool(plan.items))
        self._delete.setEnabled(not active)
        # Edit is available only when the plan is HOLDING (retarget window B) or NOT active
        # (edit the definition). While a run is armed/on-air it's disabled — there's nothing to
        # edit mid-flight (the run captured its steps at arm; window B is editable once holding).
        self._edit.setEnabled(holding or not active)
        if holding and on_proceed is not None:
            self._arm.clicked.connect(lambda: on_proceed(plan))
        else:
            self._arm.clicked.connect(lambda: on_arm(plan))
        self._stop.clicked.connect(lambda: on_stop(plan))
        self._log.clicked.connect(lambda: on_log(plan))
        self._delete.clicked.connect(lambda: on_delete(plan))
        # One Edit button: while HOLDING (with edit-while-holding support) it retargets the
        # post-hold (window-B) steps before Proceed; otherwise it opens the plan editor.
        if can_edit_wb and on_edit_wb is not None:
            self._edit.setToolTip("Edit the post-hold steps (the down-ramp / cool-down) before you "
                                  "Proceed — e.g. retarget the down-ramp to where lock was lost")
            self._edit.clicked.connect(lambda: on_edit_wb(plan))
        else:
            self._edit.setToolTip("Edit this plan" if not active else
                                  "Stop the run (or wait for it to finish) to edit the plan")
            self._edit.clicked.connect(lambda: on_edit(plan))
        if can_ff and on_hold_now is not None:
            self._hold_now.clicked.connect(lambda: on_hold_now(plan))
        if can_export:
            self._export.clicked.connect(lambda: on_export(plan))
        shown = [self._arm]
        if can_ff:
            shown.append(self._hold_now)     # only while a run-up is in progress
        shown += [self._stop, self._log]
        if can_export:
            shown.append(self._export)       # export a ran log to a spreadsheet
        shown += [self._edit, self._delete]
        for b in shown:
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
        self._runs_sig: object = None   # last-rendered active-run signature
        # Per-run edited window-B steps (edit-while-holding): run_id → full edited step list,
        # sent as ProceedRequest.steps on the next Proceed and cleared once applied.
        self._wb_edits: dict = {}
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self.hub.event_received.connect(self._on_event)
        # Safety net: fold the poller's periodic run snapshot in, so a finished run
        # stops showing "running" even if its 'sequence_stopped' webhook was missed
        # (an SSE blip). Webhooks are the fast path; this is the guaranteed backstop.
        self.hub.fast_update.connect(self._on_fast_update)

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
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search plans…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(200)
        self._search.textChanged.connect(lambda _=0: self._rebuild())
        row.addWidget(self._search)
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
            self._sync_units()
            self._refresh()

    def _on_edit(self, plan: m.Plan) -> None:
        dlg = PlanEditorDialog(self.hub, plan=plan, parent=self.window())
        if dlg.exec() and dlg.result_plan is not None:
            self._store.upsert(dlg.result_plan)
            self._sync_units()
            self._refresh()

    def _on_log(self, plan: m.Plan) -> None:
        # Non-modal so the operator can watch the plan's logs while working elsewhere.
        dlg = PlanLogDialog(self.hub, plan, parent=self.window())
        dlg.setModal(False)
        dlg.show()

    def _on_export(self, plan: m.Plan) -> None:
        """Export a ran plan's log — pick one of the plan's last runs; one sheet per unit. v1
        plans are single-unit; the picker lists runs of the first item's sequence stamped with
        this plan's id."""
        from .run_export import RunExportDialog
        if not plan.items:
            return
        targets = [(it.hostname, it.unit_label or it.hostname) for it in plan.items]
        RunExportDialog(self.hub, targets, plan.items[0].sequence_id, plan.name or plan.id,
                        plan_id=plan.id, parent=self.window()).exec()

    def _on_delete(self, plan: m.Plan) -> None:
        if QMessageBox.question(
            self, "Delete plan",
            f"Delete plan '{plan.name or plan.id}'?\nThis removes the plan here and "
            f"on every reachable unit (all units mirror the PC).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._store.delete(plan.id)
        self._sync_units()
        self._refresh()

    def _sync_units(self) -> None:
        """Replicate the just-edited plans (and schedule) out to every unit so
        their copies stay identical to the PC — the PC is the source of truth."""
        self.hub.sync_state_to_units()

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
        self._sync_units()
        self._refresh()
        summary = f"Imported {len(to_add)} plan(s)."
        if skipped:
            summary += f"\nSkipped {len(skipped)} (name already exists)."
        if bad:
            summary += "\n\nCould not read:\n" + "\n".join(bad)
        QMessageBox.information(self, "Import complete", summary)

    # ── Arm (shared on-air, clock-skew pre-flight) ─────────────────────────────

    def _on_arm(self, plan: m.Plan) -> None:
        if getattr(self, "_arm_busy", False):
            return                       # a preflight is already in flight — no double-arm
        missing = [i for i in plan.items if i.hostname not in self.fleet]
        if missing:
            names = ", ".join(i.unit_label or i.hostname for i in missing)
            QMessageBox.warning(self, "Cannot arm plan",
                                f"These units are not in the fleet: {names}")
            return
        self._arm_busy = True
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

        # Per-unit clock offset vs the laptop (unit_now − laptop_now), so an
        # operator-picked absolute T0 stays valid on each unit even if the laptop
        # clock is off. Best-effort: units without a clock reading contribute 0.
        laptop_now = datetime.now(timezone.utc)
        clock_off: Dict[str, float] = {}
        for host, val in (systems or {}).items():
            unit_now = _parse_iso(getattr(val, "utc_now", None)) if val is not None else None
            clock_off[host] = (unit_now - laptop_now).total_seconds() if unit_now else 0.0

        # Resolve each item's steps → warm-up lead-in and minimum on-air duration.
        seq_by: Dict[str, Dict[str, m.Sequence]] = {}
        for host, val in (seqs or {}).items():
            if isinstance(val, list):
                seq_by[host] = {s.id: s for s in val}
        # Earliest instant T0 is valid: for every item the earliest step must land
        # in the unit's future, i.e. T0 ≥ now + (unit clock offset + lead − offset).
        max_eff_lead = 0.0
        plan_min_dur = 0.0   # longest sequence's minimum on-air window, across items
        missing_seq = []
        has_hold = False     # any item carries an operator-gated Hold
        resolved: Dict[int, list] = {}   # item index → its resolved (uncollapsed) steps
        for idx, item in enumerate(plan.items):
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
            resolved[idx] = steps
            if m.has_hold(steps):
                has_hold = True
            # Timing is derived from the straight-through (Hold-collapsed) sequence in BOTH
            # branches: the Hold marker is start-anchored and window B re-anchors to start at
            # the same offset, so the warm-up lead-in and minimum window are identical whether
            # the Hold is honoured (single-unit) or compiled out (multi-unit / schedule).
            csteps = m.collapse_hold(steps)
            eff = _lead_in(csteps) + clock_off.get(item.hostname, 0.0) - item.on_air_offset_s
            max_eff_lead = max(max_eff_lead, eff)
            plan_min_dur = max(plan_min_dur, _ramp.min_on_air_duration(csteps))
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

        # A single-unit, single-sequence plan whose one item holds — on a unit running the Hold
        # RUNTIME — is armed hold_aware (operator-present): the run parks at the Hold and awaits
        # Proceed, exactly like the Library path (docs/sequence-hold-step.md §6–§7). Any OTHER
        # Hold-bearing plan compiles the Hold out and runs straight through: more than one item
        # (cross-unit Hold sync is deferred — §10), or a unit whose agent can't run the runtime.
        hold_item = _hold_aware_plan_item(plan, resolved, self._unit_hold_runtime_ok)

        if hold_item is not None:
            self._arm_hold_aware_plan(plan, hold_item, resolved[0],
                                      max(0.0, max_eff_lead), skew_note)
            return

        if has_hold:
            if len(plan.items) > 1:
                why = ("This plan arms more than one sequence together, and coordinating an "
                       "operator-gated Hold across units isn't supported yet")
            else:
                why = ("This plan's unit can't run an operator-gated Hold (its agent needs the "
                       "sequence-hold capability, agent 1.17+)")
            if QMessageBox.warning(
                self, "Plan contains a Hold",
                f"{why}, so the Hold is disabled here: the sequence runs straight through "
                "without pausing (the down-ramp starts immediately after the up-ramp). To use "
                "the operator-gated pause, arm a single-sequence plan on a capable unit, or run "
                "the sequence from its unit's Library tab.\n\nArm the plan anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
                self._set_status("arm cancelled")
                return

        n_units = len({i.hostname for i in plan.items})
        default_dur = plan_min_dur if plan_min_dur > 0 else DEFAULT_STOP_DURATION_S
        heading = f"Arm {len(plan.items)} sequence(s) across {n_units} unit(s)"
        dlg = ArmDialog(heading, max(0.0, max_eff_lead) + ARM_MARGIN_S,
                        default_dur, plan_min_dur, skew_note, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("arm cancelled")
            return

        t0 = dlg.on_air_at()
        duration_s = dlg.stop_duration_s()
        self._set_status(f"arming {plan.name or plan.id}…")
        self.hub.run_async(
            f"plan_arm:{plan.id}",
            lambda: _arm_plan(self.fleet, plan, t0, duration_s),
        )

    def _arm_hold_aware_plan(self, plan: m.Plan, item: m.PlanItem, steps: list,
                             safety_lead_s: float, skew_note: str) -> None:
        """Arm a single-unit, single-sequence plan whose item holds — operator-present, so the
        run parks at the Hold and awaits Proceed (the plan mirror of sequences_panel's
        _arm_hold_aware). Always open-ended; the down-ramp (window B) is scheduled at Proceed
        (docs/sequence-hold-step.md §6.2)."""
        hold_off = next((s.offset_s for s in steps
                         if m._step_action(s) == m.StepAction.HOLD.value), 0.0)
        wa = fmt_duration(round(max(0.0, hold_off + _lead_in(steps))))
        label = item.unit_label or item.hostname
        dlg = ArmDialog(
            f"Arm plan “{plan.name or plan.id}” (holds at the Hold)",
            safety_lead_s + ARM_MARGIN_S, DEFAULT_STOP_DURATION_S, 0.0, skew_note, parent=self,
            body_note=(f"This plan's sequence pauses at the Hold and awaits you. It runs to the "
                       f"Hold on {label} in ~{wa}, then holds the signal exactly until you "
                       f"Proceed (the post-hold window — the down-ramp — is scheduled when you "
                       f"proceed). This is a single-unit, operator-present run; the schedule and "
                       f"multi-unit plans run straight through a Hold."),
            show_stop=False, max_hold_default_s=DEFAULT_MAX_HOLD_S)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("arm cancelled")
            return
        t0 = dlg.on_air_at()
        max_hold = dlg.max_hold_s()
        self._set_status(f"arming {plan.name or plan.id} (holds at the Hold)…")
        self.hub.run_async(
            f"plan_arm:{plan.id}",
            lambda: _arm_plan(self.fleet, plan, t0, None,
                              hold_aware=True, max_hold_s=max_hold),
        )

    # ── Proceed / Hold-now / Edit-while-holding (single-unit hold-aware plan runs) ──────

    def _hold_run_for(self, plan: m.Plan, states) -> tuple:
        """(hostname, SequenceRun) of this plan's first run in one of `states`, else
        (None, None). A hold-aware plan is single-unit/single-item, so there is at most one."""
        for host, runs in self._runs_by_host.items():
            for r in runs:
                if r.plan_id == plan.id and r.state in states:
                    return host, r
        return None, None

    def _ff_run_for(self, plan: m.Plan) -> tuple:
        """(hostname, SequenceRun) of this plan's RUNNING hold-aware run that hasn't reached its
        Hold yet (can be fast-forwarded), else (None, None)."""
        for host, runs in self._runs_by_host.items():
            for r in runs:
                if (r.plan_id == plan.id and r.state == m.SequenceState.RUNNING
                        and getattr(r, "hold_aware", False) and r.held_actual is None):
                    return host, r
        return None, None

    def _unit_supports(self, host: Optional[str], capability: str) -> bool:
        if not host:
            return False
        try:
            return bool(self.fleet.get(host).supports(capability))
        except Exception:  # noqa: BLE001
            return False

    def _unit_hold_runtime_ok(self, host: Optional[str]) -> bool:
        """True iff the unit runs the Hold RUNTIME (sequence-hold + >= 1.17.0), so a hold-aware
        plan arm parks rather than being refused (docs/sequence-hold-step.md §11)."""
        if not host:
            return False
        try:
            return hold_runtime_supported(self.fleet.get(host))
        except Exception:  # noqa: BLE001
            return False

    def _hold_status_text(self, run: m.SequenceRun) -> str:
        """Live one-liner for the Proceed dialog: elapsed run time · time held · the remaining
        max-hold allowance (mirrors sequences_panel._hold_status_text)."""
        now = datetime.now(timezone.utc)
        parts: List[str] = []
        base = run.on_air_actual or run.started_actual or run.on_air_at
        base_dt = _parse_iso(base)
        if base_dt is not None:
            parts.append(f"elapsed {fmt_duration(round(max(0.0, (now - base_dt).total_seconds())))}")
        held_dt = _parse_iso(run.held_actual)
        if held_dt is not None:
            held = (now - held_dt).total_seconds()
            parts.append(f"held {fmt_duration(round(max(0.0, held)))}")
            if run.max_hold_s and run.max_hold_s > 0:
                rem = run.max_hold_s - held
                parts.append(f"auto-stops in {fmt_duration(round(rem))}" if rem > 0
                             else "auto-stop imminent")
        return "  ·  ".join(parts)

    def _on_proceed(self, plan: m.Plan) -> None:
        """Resume a HOLDING plan run: pick the resume instant (the same timing control as arm)
        and schedule window B (docs/sequence-hold-step.md §6.3)."""
        host, run = self._hold_run_for(plan, (m.SequenceState.HOLDING,))
        if run is None:
            self._set_status("no holding run to proceed", error=True)
            self._refresh_runs()
            return
        dlg = ArmDialog(
            f"Proceed “{plan.name or plan.id}”", PROCEED_LEAD_S, DEFAULT_STOP_DURATION_S,
            0.0, parent=self, accept_label="Proceed", title="Proceed",
            body_note=("Choose when the post-hold window (the down-ramp) starts. The held "
                       "signal stays exactly where it is until then."),
            show_stop=False, status_provider=lambda r=run: self._hold_status_text(r))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("proceed cancelled")
            return
        resume = dlg.on_air_at()
        edited = self._wb_edits.get(run.id)          # edit-while-holding, if any
        client = self.fleet.get(host)
        self._set_status(f"proceeding {plan.name or plan.id}"
                         + (" with edited post-hold steps…" if edited else "…"))
        self.hub.run_async(
            f"plan_proceed:{plan.id}",
            lambda: _proceed_run(client, run.id, resume, steps=edited),
        )

    def _on_hold_now(self, plan: m.Plan) -> None:
        """Fast-Forward-to-Hold: jump a RUNNING hold-aware plan run straight to its Hold now,
        skipping the rest of the run-up (docs/sequence-hold-step.md §5.4)."""
        host, run = self._ff_run_for(plan)
        if run is None:
            self._set_status("no running hold-aware run to fast-forward", error=True)
            self._refresh_runs()
            return
        if QMessageBox.question(
            self, "Hold now",
            f"Jump “{plan.name or plan.id}” to its Hold now?\n\nThe rest of the run-up is skipped "
            f"and the signal holds its CURRENT value until you Proceed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            return
        client = self.fleet.get(host)
        self._set_status(f"holding {plan.name or plan.id} now…")
        self.hub.run_async(
            f"plan_holdnow:{plan.id}",
            lambda: client.hold_now_sequence_run(run.id),
        )

    def _on_edit_wb(self, plan: m.Plan) -> None:
        """Edit-while-holding: revise the post-hold (window-B) steps of a HOLDING plan run,
        held per-run until the next Proceed applies them (docs/sequence-hold-step.md §6.4)."""
        host, run = self._hold_run_for(plan, (m.SequenceState.HOLDING,))
        if run is None:
            self._set_status("no holding run to edit", error=True)
            self._refresh_runs()
            return
        # The plan item whose sequence this run is executing (single-item for a hold-aware plan).
        item = next((it for it in plan.items if it.sequence_id == run.sequence_id),
                    plan.items[0] if plan.items else None)
        if item is None:
            self._set_status("no plan item to edit", error=True)
            return
        # Seed the editor with the pending edit for this run, else the plan-local steps, else the
        # unit's stored sequence (a steps-less item references it).
        pending = self._wb_edits.get(run.id)
        steps = pending if pending is not None else (list(item.steps) if item.steps else None)
        if steps is None:
            try:
                stored = self.fleet.get(host).get_sequence(item.sequence_id)
            except Exception:  # noqa: BLE001
                stored = None
            steps = list(stored.steps) if stored is not None else []
        seed = m.Sequence(id=item.sequence_id, name=item.sequence_name or item.sequence_id,
                          steps=steps)
        from .hold_edit_dialog import HoldEditDialog
        dlg = HoldEditDialog(self.hub, host, seed, parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_steps is None:
            return
        self._wb_edits[run.id] = dlg.result_steps
        self._set_status("post-hold steps edited — Proceed to apply")

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

    def _runs_signature(self) -> frozenset:
        """A cheap fingerprint of the active-run picture — used to rebuild the plan
        rows only when run state actually changes, not on every poll tick."""
        return frozenset(
            (host, r.id, r.state, bool(getattr(r, "on_air_actual", None)))
            for host, rs in self._runs_by_host.items() for r in rs)

    def _on_fast_update(self, snap) -> None:
        runs = getattr(snap, "runs", None)
        if not isinstance(runs, dict) or not runs:
            return
        # Merge per host — a scoped refresh carries only one unit's runs, so we must
        # not wipe the others.
        for host, val in runs.items():
            self._runs_by_host[host] = val if isinstance(val, list) else []
        if self._runs_signature() != self._runs_sig and self.isVisible():
            self._rebuild()   # recomputes and stores the signature

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
            self._arm_busy = False       # preflight done; the modal dialog guards the rest
            if plan is None:
                return
            self._finish_arm_preflight(plan, result)
        elif op == "plan_arm":
            self._report_arm(result)
            self._refresh_runs()
        elif op == "plan_proceed":
            if isinstance(result, Exception):
                self._set_status("proceed failed", error=True)
                QMessageBox.warning(self, "Could not proceed", str(result))
            else:
                self._set_status("proceeding — post-hold window scheduled")
            self._refresh_runs()
        elif op == "plan_holdnow":
            if isinstance(result, Exception):
                self._set_status("hold-now failed", error=True)
                QMessageBox.warning(self, "Could not fast-forward to the Hold", str(result))
            else:
                self._set_status("holding — fast-forwarded to the Hold")
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
        # Drop any edit-while-holding revisions for runs that are no longer HOLDING
        # (proceeded — the edit was applied — or aborted).
        if self._wb_edits:
            holding_ids = {r.id for rs in self._runs_by_host.values() for r in rs
                           if r.state == m.SequenceState.HOLDING}
            self._wb_edits = {rid: v for rid, v in self._wb_edits.items() if rid in holding_ids}

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
            self._runs_sig = self._runs_signature()
            return

        query = self._search.text().strip().lower()
        # Stable alphanumeric order — so editing a plan never reorders the list.
        plans = sorted(self._plans, key=lambda p: natural_key(p.name or p.id))
        active_total = 0
        shown = 0
        for plan in plans:
            if query and query not in (plan.name or "").lower() \
                    and query not in (plan.description or "").lower():
                continue
            runs = self._active_run_objs(plan)
            holding_n = sum(1 for r in runs if r.state == m.SequenceState.HOLDING)
            on_air_n = sum(1 for r in runs if _is_on_air(r))
            pending_n = len(runs) - on_air_n - holding_n
            if runs:
                active_total += 1
            # A single-unit hold-aware plan run surfaces the Hold controls (Proceed / Hold-now /
            # Edit…), each gated on the run's state and the unit's advertised capability.
            holding = holding_n > 0
            hold_host, _hr = self._hold_run_for(plan, (m.SequenceState.HOLDING,))
            ff_host, _fr = self._ff_run_for(plan)
            can_ff = ff_host is not None and self._unit_supports(ff_host, SEQUENCE_HOLD_NOW_CAPABILITY)
            can_edit_wb = holding and self._unit_supports(hold_host, SEQUENCE_HOLD_EDIT_CAPABILITY)
            export_ok = any(self._unit_supports(it.hostname, SEQUENCE_LOG_TABLE_CAPABILITY)
                            for it in plan.items)
            shown += 1
            self._list.addWidget(_PlanRow(
                plan, runs, on_air_n, pending_n,
                on_arm=self._on_arm, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete, on_log=self._on_log,
                holding=holding, can_ff=can_ff, can_edit_wb=can_edit_wb,
                on_proceed=self._on_proceed, on_hold_now=self._on_hold_now,
                on_edit_wb=self._on_edit_wb, on_export=self._on_export, export_ok=export_ok))
        if shown == 0 and query:
            empty = QLabel(f"No plans match “{query}”.")
            empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        suffix = f" · {active_total} active" if active_total else ""
        if query:
            self._set_status(f"{shown} plan(s) match · {len(self._plans)} total{suffix}")
        else:
            self._set_status(f"{len(self._plans)} plan(s){suffix}")
        self._runs_sig = self._runs_signature()   # rows now match this run picture

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
