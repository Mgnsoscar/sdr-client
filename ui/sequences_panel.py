"""
SequencesPanel — the Sequences sub-tab of the unit detail view.

Lists the sequences stored on this unit (GET /sequences), each with a short
timeline summary and a live run-state pill, and lets you:

  - New     → create a sequence (SequenceEditorDialog)
  - Export  → save every sequence on this unit to a portable YAML file
  - Import  → create sequences from a YAML file (existing names skipped)
  - Edit    → change an existing sequence (same dialog, prefilled)
  - Arm     → pick an on-air time (a chosen slot or as-soon-as-possible) and an
              optional stop, then fire the on-air steps; open-ended unless a stop
              is set
  - Stop    → cancel the armed run / abort the running one (stops every task the
              sequence touches)
  - Delete  → remove the sequence (disabled while a run is active)

Run state is tracked by fetching GET /sequence-runs alongside the sequence list,
and refreshed live whenever a sequence lifecycle event arrives on the SSE stream.

All network calls go through the DataHub's run_async (off the GUI thread); their
results arrive on the shared task_done signal, filtered here to this host + ops.

Operation labels (parsed back in _on_task_done):
    seq_list:<host>
    seq_runs:<host>
    seq_arm:<host>:<seq_id>
    seq_stop:<host>:<seq_id>
    seq_delete:<host>:<seq_id>
    seqio_export:<host>
    seqio_import:<host>
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from api import ramp as _ramp
from config import UNIT_TYPE_LABELS, DEFAULT_UNIT_TYPE
from .arm_dialog import ArmDialog
from .param_form import fmt_duration
from .qt_adapter import DataHub
from .scope_selector import scope_chip, confirm_delete
from .sequence_editor import SequenceEditorDialog
from .sequence_log_dialog import SequenceLogDialog
from .theme import Palette
from .hold_edit_dialog import HoldEditDialog
from .timeline_model import (
    SEQUENCE_HOLD_CAPABILITY, SEQUENCE_HOLD_EDIT_CAPABILITY, SEQUENCE_HOLD_NOW_CAPABILITY,
    hold_runtime_supported)
from .widgets import StatusPill, natural_key

_SEQ_FILTER_ALL = "__all__"

# Seconds of headroom added when arming "now", so the first step is safely in the
# future even with a little clock skew between the laptop and the unit.
ARM_MARGIN_S = 5.0
DEFAULT_STOP_DURATION_S = 60.0   # fallback when a sequence has no derivable minimum
# Default max-hold deadman offered when arming a Hold-aware run (30 min; 0 = unlimited).
DEFAULT_MAX_HOLD_S = 1800.0
# Safety lead for a Proceed: window B's first step fires at (resume + its offset ≥ 0),
# so a small margin covers clock skew and the immediate-proceed case.
PROCEED_LEAD_S = 3.0

# A run is "active" (blocks re-arm / delete, keeps Stop live) while armed, running, or
# HOLDING (parked at a Hold with RF live, awaiting Proceed).
_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING, m.SequenceState.HOLDING)


def _hold_offset_of(seq: m.Sequence) -> Optional[float]:
    """The on-air offset of the sequence's Hold marker (window A's length), or None
    if it has no Hold."""
    for s in seq.steps:
        if m._step_action(s) == m.StepAction.HOLD.value:
            return float(s.offset_s)
    return None

Result = Tuple[str, Optional[str]]   # (run_id, error-or-None)


def _lead_in(seq: m.Sequence) -> float:
    """Warm-up lead-in: how far before on-air the earliest start-anchored step fires
    (the magnitude of its most-negative offset), so on-air is scheduled far enough
    out that no step lands in the past. 0 if there are no start-anchored steps."""
    starts = [s.offset_s for s in seq.steps if s.anchor == "start"]
    return max(0.0, -min(starts)) if starts else 0.0


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _run_timing(run: m.SequenceRun) -> str:
    """A one-line 'on air HH:MM:SS · off air HH:MM:SS · duration' digest of an armed
    run — the useful facts once a sequence is on the air, shown instead of its steps."""
    on = _parse_iso(run.on_air_at).astimezone().strftime("%H:%M:%S")
    if run.open_ended or not run.on_air_end:
        return f"on air {on}  ·  off air —  ·  open-ended"
    off_dt = _parse_iso(run.on_air_end)
    off = off_dt.astimezone().strftime("%H:%M:%S")
    dur = (off_dt - _parse_iso(run.on_air_at)).total_seconds()
    return f"on air {on}  ·  off air {off}  ·  {fmt_duration(round(dur))}"


def _arm_at(client, seq: m.Sequence, t0_laptop: datetime,
            duration_s: Optional[float], hold_aware: bool = False,
            max_hold_s: float = DEFAULT_MAX_HOLD_S) -> m.SequenceRun:
    """
    Arm a sequence to go on air at the operator-chosen wall-clock instant t0 (given
    in laptop UTC), translating it to the AGENT's clock so RF goes live at that same
    wall-clock time even if the unit clock is skewed (e.g. a Pi with no NTP on an
    isolated ethernet link). The agent fires steps against its own clock, so we send
    on_air_at = t0 + (unit clock − laptop clock); the offset cancels when the unit
    interprets it, and relative timing (warm-up leads) stays exact. Falls back to no
    adjustment if /system is unavailable. When duration_s is set the run is bounded
    (and stop-anchored steps fire); otherwise it's open-ended. Worker thread.

    hold_aware=True (interactive Library/operator-present arm of a Hold-bearing
    sequence) parks the run at the Hold; the agent resolves only window A and awaits
    Proceed. It is always open-ended (window B is scheduled at proceed). See
    docs/sequence-hold-step.md §6.2.
    """
    on_air_at = t0_laptop + timedelta(seconds=client.clock_offset_s())
    req = m.ArmSequenceRequest(
        on_air_at=on_air_at.isoformat(),
        open_ended=(hold_aware or duration_s is None),
        on_air_duration_s=(None if hold_aware else duration_s),
        note="manual test",
        hold_aware=hold_aware,
        max_hold_s=max_hold_s,
    )
    return client.arm_sequence(seq.id, req)


def _proceed_run(client, run_id: str, resume_laptop: datetime,
                 steps: Optional[List[m.SequenceStep]] = None) -> m.SequenceRun:
    """Resume a HOLDING run at the operator-chosen instant, translating it to the
    agent's clock (like _arm_at). The agent resolves window B relative to it. When
    `steps` is given (edit-while-holding), the agent re-resolves window B from that
    edited sequence instead of the one stored at arm. Worker thread."""
    resume_at = resume_laptop + timedelta(seconds=client.clock_offset_s())
    return client.proceed_sequence_run(
        run_id, m.ProceedRequest(proceed_at=resume_at.isoformat(), steps=steps))


def _abort_runs(client, run_ids: List[str]) -> List[Result]:
    """Cancel/abort each run id; runs on a worker thread."""
    out: List[Result] = []
    for rid in run_ids:
        try:
            client.cancel_sequence_run(rid)
            out.append((rid, None))
        except Exception as exc:  # noqa: BLE001 — reported per run
            out.append((rid, str(exc)))
    return out


def sequences_to_yaml(seqs: List[m.Sequence]) -> str:
    """Serialize sequences to a portable YAML document ({sequences: [...]}).

    Only the definition travels — name, description, and steps — not the unit's
    generated id or any run state, so the file re-creates cleanly on any unit.
    """
    docs = []
    for seq in seqs:
        # mode="json" turns the StepAction enum into a plain string so PyYAML's
        # safe_dump can render it (and the file stays human-readable).
        doc = {
            "name": seq.name,
            "description": seq.description,
            "steps": [s.model_dump(mode="json") for s in seq.steps],
        }
        if seq.types:                       # omit when shared, to keep files tidy
            doc["types"] = list(seq.types)
        docs.append(doc)
    return yaml.safe_dump({"sequences": docs}, sort_keys=False, allow_unicode=True)


def _import_sequences(client, requests) -> List[Result]:
    """Create each sequence via the client (skipping name conflicts). Worker thread."""
    existing = {s.name for s in client.list_sequences()}
    out: List[Result] = []
    for name, req in requests:
        if name in existing:
            out.append((name, "skipped (name already exists)"))
            continue
        try:
            client.create_sequence(req)
            existing.add(name)
            out.append((name, None))
        except Exception as exc:  # noqa: BLE001 — CreateSequence errors, reported per item
            out.append((name, str(exc)))
    return out


class _SequenceRow(QFrame):
    """One sequence: name, summary, run-state pill, and action buttons."""

    def __init__(self, seq: m.Sequence, active_run: Optional[m.SequenceRun],
                 on_start, on_stop, on_edit, on_delete, on_log,
                 can_edit: bool = True, can_run: bool = True,
                 show_scope: bool = False, on_proceed=None, on_hold_now=None,
                 hold_now_ok: bool = False, on_edit_wb=None, edit_wb_ok: bool = False):
        super().__init__()
        self.seq = seq
        self.setObjectName("card")
        active = active_run is not None and active_run.state in _ACTIVE
        holding = active_run is not None and active_run.state == m.SequenceState.HOLDING
        # A RUNNING hold-aware run that hasn't reached its Hold yet can be fast-forwarded.
        can_ff = (hold_now_ok and on_hold_now is not None and active_run is not None
                  and active_run.state == m.SequenceState.RUNNING
                  and getattr(active_run, "hold_aware", False)
                  and active_run.held_actual is None)
        # While HOLDING, the post-hold (window-B) steps can be edited before proceeding.
        can_edit_wb = holding and edit_wb_ok and on_edit_wb is not None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout()
        box.setSpacing(2)
        header = QLabel(seq.name or seq.id)
        header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(header)
        if seq.description:
            from .desc_widget import CollapsibleDescription
            box.addWidget(CollapsibleDescription(seq.description))
        # Show the run's timing once armed (on a live unit), otherwise just a step
        # count — the full step list is noise here (edit the sequence to see it).
        if can_run and active:
            summary_text = _run_timing(active_run)
        else:
            summary_text = f"{len(seq.steps)} step(s)"
        summary = QLabel(summary_text)
        summary.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        summary.setWordWrap(True)
        box.addWidget(summary)
        # The shortest on-air window this sequence fits in, always visible so it's
        # legible without opening the sequence. A sequence with no stop-anchored
        # steps has no minimum, so nothing is shown.
        min_dur = _ramp.min_on_air_duration(seq.steps)
        if min_dur > 0:
            mind = QLabel(f"min duration  {fmt_duration(round(min_dur))}")
            mind.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(mind)
        lay.addLayout(box, stretch=1)

        # Library view: show which unit types this sequence targets.
        if show_scope:
            lay.addWidget(scope_chip(seq.types),
                          alignment=Qt.AlignmentFlag.AlignVCenter)

        # The run-state pill and Arm/Stop/Log belong to a live unit (can_run);
        # Edit/Delete are definition editing (can_edit, i.e. the Library).
        if can_run:
            state_word = active_run.state.value if active else "idle"
            self._pill = StatusPill(state_word, state_word)
            lay.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignTop)

        # While HOLDING the Arm button becomes Proceed (schedule window B / resume).
        self._start = QPushButton("Proceed" if holding else "Arm")
        self._stop = QPushButton("Stop")
        self._log = QPushButton("Log")
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        # Fast-Forward-to-Hold: jump a running hold-aware run to its Hold now.
        self._hold_now = QPushButton("Hold now")
        self._hold_now.setToolTip("Jump to the Hold now — skip the rest of the run-up and hold the "
                                  "signal at its current value (then Proceed when ready)")
        self._hold_now.setVisible(can_ff)
        # Edit-while-holding: retarget the post-hold steps before proceeding.
        self._edit_wb = QPushButton("Edit…")
        self._edit_wb.setToolTip("Edit the post-hold steps (the down-ramp / cool-down) before you "
                                 "Proceed — e.g. retarget the down-ramp to where lock was lost")
        self._edit_wb.setVisible(can_edit_wb)
        # Minimum (not fixed) width: the row stays aligned at 100%, but a button grows to fit a
        # longer label ("Proceed" > "Arm") or a wider fallback font at fractional scaling instead
        # of clipping it to an ellipsis.
        for b in (self._start, self._stop, self._log, self._edit, self._delete):
            b.setMinimumWidth(66)
        self._hold_now.setMinimumWidth(72)
        self._edit_wb.setMinimumWidth(60)
        self._start.setToolTip(
            "Proceed — schedule the post-hold window (the down-ramp) and resume the run"
            if holding else
            "Arm — pick an on-air time (or as-soon-as-possible) and "
            "an optional stop, then fire the on-air steps")
        self._stop.setToolTip("Stop this run — cancels if armed, aborts if running "
                              "(stops every task it touches)")
        self._log.setToolTip("View this sequence's run log — the whole run's timeline "
                             "and each step's output, live")
        # Proceed is enabled while holding; Arm is enabled only when nothing is active.
        self._start.setEnabled(holding or not active)
        self._stop.setEnabled(active)
        self._delete.setEnabled(not active)   # the agent refuses to delete an active one
        if holding and on_proceed is not None:
            self._start.clicked.connect(lambda: on_proceed(seq))
        else:
            self._start.clicked.connect(lambda: on_start(seq))
        self._stop.clicked.connect(lambda: on_stop(seq))
        self._log.clicked.connect(lambda: on_log(seq))
        self._edit.clicked.connect(lambda: on_edit(seq))
        self._delete.clicked.connect(lambda: on_delete(seq))
        if can_ff:
            self._hold_now.clicked.connect(lambda: on_hold_now(seq))
        if can_edit_wb:
            self._edit_wb.clicked.connect(lambda: on_edit_wb(seq))
        shown = []
        if can_run:
            shown += [self._start]
            if can_ff:
                shown.append(self._hold_now)     # only while a run-up is in progress
            if can_edit_wb:
                shown.append(self._edit_wb)      # only while holding
            shown += [self._stop, self._log]
        if can_edit:
            shown += [self._edit, self._delete]
        for b in shown:
            lay.addWidget(b, alignment=Qt.AlignmentFlag.AlignTop)


class SequencesPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None,
                 can_edit: bool = True, can_run: bool = True):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        # Two capabilities, set by the surface:
        #   Library  → can_edit=True,  can_run=False  (author definitions offline)
        #   Unit card→ can_edit=False, can_run=True   (run what's deployed, no editing)
        self.can_edit = can_edit
        self.can_run = can_run
        self._active_type = DEFAULT_UNIT_TYPE   # library view: set by the unit-type selector
        self._sequences: List[m.Sequence] = []
        self._runs: List[m.SequenceRun] = []
        # Per-run edited window-B steps (edit-while-holding): run_id -> full edited step list,
        # sent as ProceedRequest.steps on the next Proceed and cleared once applied.
        self._wb_edits: dict = {}
        self._seq_loaded = False
        self._runs_pending = False
        self._export_path: Optional[str] = None
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self.hub.task_done.connect(self._on_io_done)
        # Live-refresh run state when a sequence lifecycle event arrives.
        if self.can_run:
            self.hub.event_received.connect(self._on_event)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        # Authoring controls (New / Export / Import) only when this surface can edit
        # definitions. A unit card is run-only: definitions come from the Library.
        if self.can_edit:
            self._new_btn = QPushButton("New sequence")
            self._new_btn.setObjectName("primary")
            self._new_btn.clicked.connect(self._on_new)
            row.addWidget(self._new_btn)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        row.addWidget(self._refresh_btn)
        if self.can_edit:
            self._export_btn = QPushButton("Export…")
            self._export_btn.setToolTip("Save every sequence to a YAML file")
            self._export_btn.clicked.connect(self._on_export)
            row.addWidget(self._export_btn)
            self._import_btn = QPushButton("Import…")
            self._import_btn.setToolTip("Create sequences from a YAML file "
                                        "(existing names are skipped)")
            self._import_btn.clicked.connect(self._on_import)
            row.addWidget(self._import_btn)
        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        row.addWidget(self._status)
        row.addStretch(1)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search sequences…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(200)
        self._search.textChanged.connect(lambda _=0: self._rebuild())
        row.addWidget(self._search)

        # In the Library the unit-type view is driven by the tab's selector
        # (set_active_type); a unit card shows only its own deployed sequences.
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

    # ── Shown / refresh ──────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"seq_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequences(),
        )
        if self.can_run:
            self._refresh_runs()

    def _refresh_runs(self) -> None:
        if not self.can_run or self._runs_pending:
            return
        self._runs_pending = True
        self.hub.run_async(
            f"seq_runs:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequence_runs(),
        )

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        # New sequences default to the active unit type; the editor's scope picker
        # can widen them to Shared.
        dlg = SequenceEditorDialog(self.hub, self.hostname,
                                   default_types=[self._active_type] if self.can_edit else None,
                                   parent=self.window())
        if dlg.exec():
            self._refresh()

    def _on_edit(self, seq: m.Sequence) -> None:
        dlg = SequenceEditorDialog(self.hub, self.hostname, sequence=seq, parent=self.window())
        if dlg.exec():
            self._refresh()

    def _on_log(self, seq: m.Sequence) -> None:
        # Non-modal so the operator can watch the run log while working elsewhere.
        dlg = SequenceLogDialog(self.hub, self.hostname, seq, parent=self.window())
        dlg.setModal(False)
        dlg.show()

    def _on_start(self, seq: m.Sequence) -> None:
        hold_off = _hold_offset_of(seq)
        if hold_off is not None:
            self._arm_hold_aware(seq, hold_off)
            return
        # Pick the on-air time (and optional stop) the same way plans are armed.
        min_dur = _ramp.min_on_air_duration(seq.steps)
        default_dur = min_dur if min_dur > 0 else DEFAULT_STOP_DURATION_S
        dlg = ArmDialog(f"Arm sequence “{seq.name or seq.id}”",
                        _lead_in(seq) + ARM_MARGIN_S, default_dur, min_dur,
                        parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("arm cancelled")
            return
        t0 = dlg.on_air_at()
        duration_s = dlg.stop_duration_s()
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"arming {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_arm:{self.hostname}:{seq.id}",
            lambda: _arm_at(client, seq, t0, duration_s),
        )

    def _arm_hold_aware(self, seq: m.Sequence, hold_off: float) -> None:
        """Arm a Hold-bearing sequence from the Library/operator-present surface: the
        run pauses at the Hold and awaits Proceed. Gated on the unit's agent running the
        Hold RUNTIME (advertises `sequence-hold` AND is >= 1.17.0, via hold_runtime_supported):
        1.16.0 advertised the capability for the data model only and refuses a hold-aware arm,
        so the version check turns that into a clean client-side block rather than an
        agent-side refusal. See docs/sequence-hold-step.md §6.2/§11."""
        if not self._hold_runtime_ok():
            QMessageBox.warning(
                self, "Hold not supported here",
                f"“{seq.name or seq.id}” contains a Hold (an operator-gated pause), but "
                f"{self.hostname}'s agent doesn't support it (needs the sequence-hold "
                f"capability, agent 1.17+). Update the unit's agent, or remove the Hold.")
            self._set_status("arm blocked — agent lacks sequence-hold", error=True)
            return
        wa = fmt_duration(round(max(0.0, hold_off + _lead_in(seq))))
        dlg = ArmDialog(
            f"Arm sequence “{seq.name or seq.id}”",
            _lead_in(seq) + ARM_MARGIN_S, DEFAULT_STOP_DURATION_S, 0.0, parent=self,
            body_note=(f"This sequence pauses at the Hold and awaits you. It runs to the "
                       f"Hold in ~{wa}, then holds the signal exactly until you Proceed "
                       f"(the post-hold window — the down-ramp — is scheduled when you "
                       f"proceed). Run it from here; the schedule runs straight through a Hold."),
            show_stop=False, max_hold_default_s=DEFAULT_MAX_HOLD_S)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("arm cancelled")
            return
        t0 = dlg.on_air_at()
        max_hold = dlg.max_hold_s()
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"arming {seq.name or seq.id} (holds at the Hold)…")
        self.hub.run_async(
            f"seq_arm:{self.hostname}:{seq.id}",
            lambda: _arm_at(client, seq, t0, None, hold_aware=True, max_hold_s=max_hold),
        )

    def _on_proceed(self, seq: m.Sequence) -> None:
        """Resume a HOLDING run: pick the resume instant (the same timing control as arm)
        and schedule window B. See docs/sequence-hold-step.md §6.3."""
        run = next((r for r in self._runs if r.sequence_id == seq.id
                    and r.state == m.SequenceState.HOLDING), None)
        if run is None:
            self._set_status("no holding run to proceed", error=True)
            self._refresh_runs()
            return
        dlg = ArmDialog(
            f"Proceed “{seq.name or seq.id}”", PROCEED_LEAD_S, DEFAULT_STOP_DURATION_S,
            0.0, parent=self, accept_label="Proceed", title="Proceed",
            body_note=("Choose when the post-hold window (the down-ramp) starts. The held "
                       "signal stays exactly where it is until then."),
            show_stop=False, status_provider=lambda r=run: self._hold_status_text(r))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._set_status("proceed cancelled")
            return
        resume = dlg.on_air_at()
        edited = self._wb_edits.get(run.id)          # edit-while-holding, if any
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"proceeding {seq.name or seq.id}"
                         + (" with edited post-hold steps…" if edited else "…"))
        self.hub.run_async(
            f"seq_proceed:{self.hostname}:{seq.id}",
            lambda: _proceed_run(client, run.id, resume, steps=edited),
        )

    def _on_edit_wb(self, seq: m.Sequence) -> None:
        """Edit-while-holding: revise the post-hold (window-B) steps of a HOLDING run,
        held per-run until the next Proceed applies them (docs/sequence-hold-step.md §6.4)."""
        run = next((r for r in self._runs if r.sequence_id == seq.id
                    and r.state == m.SequenceState.HOLDING), None)
        if run is None:
            self._set_status("no holding run to edit", error=True)
            self._refresh_runs()
            return
        # Seed the editor with any pending edit for this run, else the stored definition.
        pending = self._wb_edits.get(run.id)
        seed = seq.model_copy(update={"steps": pending}) if pending else seq
        dlg = HoldEditDialog(self.hub, self.hostname, seed, parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_steps is None:
            return
        self._wb_edits[run.id] = dlg.result_steps
        self._set_status("post-hold steps edited — Proceed to apply")

    def _on_hold_now(self, seq: m.Sequence) -> None:
        """Fast-Forward-to-Hold: jump a RUNNING hold-aware run straight to its Hold now,
        skipping the rest of the run-up. See docs/sequence-hold-step.md §5.4."""
        run = next((r for r in self._runs if r.sequence_id == seq.id
                    and r.state == m.SequenceState.RUNNING
                    and getattr(r, "hold_aware", False) and r.held_actual is None), None)
        if run is None:
            self._set_status("no running hold-aware run to fast-forward", error=True)
            self._refresh_runs()
            return
        if QMessageBox.question(
            self, "Hold now",
            f"Jump “{seq.name or seq.id}” to its Hold now?\n\nThe rest of the run-up is skipped and "
            f"the signal holds its CURRENT value until you Proceed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            return
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"holding {seq.name or seq.id} now…")
        self.hub.run_async(
            f"seq_holdnow:{self.hostname}:{seq.id}",
            lambda: client.hold_now_sequence_run(run.id),
        )

    def _hold_status_text(self, run: m.SequenceRun) -> str:
        """Live one-liner for the Proceed dialog: elapsed run time · time held · the
        remaining max-hold allowance."""
        now = datetime.now(timezone.utc)
        parts: List[str] = []
        base = run.on_air_actual or run.started_actual or run.on_air_at
        try:
            if base:
                parts.append(f"elapsed {fmt_duration(round(max(0.0, (now - _parse_iso(base)).total_seconds())))}")
            if run.held_actual:
                held = (now - _parse_iso(run.held_actual)).total_seconds()
                parts.append(f"held {fmt_duration(round(max(0.0, held)))}")
                if run.max_hold_s and run.max_hold_s > 0:
                    rem = run.max_hold_s - held
                    parts.append(f"auto-stops in {fmt_duration(round(rem))}" if rem > 0
                                 else "auto-stop imminent")
        except (ValueError, TypeError):
            return ""
        return "  ·  ".join(parts)

    def _supports(self, capability: str) -> bool:
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(client, "supports", lambda _c: False)(capability))

    def _hold_runtime_ok(self) -> bool:
        """True iff this unit's agent runs the Hold RUNTIME (sequence-hold + >= 1.17.0), so a
        hold-aware arm parks rather than being refused (docs/sequence-hold-step.md §11)."""
        try:
            client = self.hub.fleet.get(self.hostname)
        except Exception:  # noqa: BLE001
            return False
        return hold_runtime_supported(client)

    def _on_stop(self, seq: m.Sequence) -> None:
        run_ids = [r.id for r in self._runs
                   if r.sequence_id == seq.id and r.state in _ACTIVE]
        if not run_ids:
            self._refresh_runs()
            return
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"stopping {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_stop:{self.hostname}:{seq.id}",
            lambda: _abort_runs(client, run_ids),
        )

    def _on_delete(self, seq: m.Sequence) -> None:
        label = seq.name or seq.id
        # In the Library (per-type view) a shared sequence can be removed from just
        # this unit type; a unit card is a plain confirm (its sequences aren't scoped).
        if self.can_edit:
            action = confirm_delete(self, "sequence", label, seq.types,
                                    self._active_type,
                                    lambda _n, new_types: self._unshare_sequence(seq, new_types))
            if action == "cancel":
                return
            if action == "unshared":
                self._refresh()
                return
        else:
            resp = QMessageBox.question(
                self, "Delete sequence",
                f"Delete sequence '{label}' from {self.hostname}?\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if resp != QMessageBox.StandardButton.Yes:
                return
        self._set_status(f"deleting {label}…")
        self.hub.run_async(
            f"seq_delete:{self.hostname}:{seq.id}",
            lambda: self.hub.fleet.get(self.hostname).delete_sequence(seq.id),
        )

    def _unshare_sequence(self, seq: m.Sequence, new_types: list) -> None:
        """Re-scope a shared sequence off the active type (keep it on the others),
        via update_sequence with a request rebuilt from the sequence."""
        req = m.CreateSequenceRequest(name=seq.name, description=seq.description,
                                      steps=seq.steps, types=list(new_types))
        self.hub.fleet.get(self.hostname).update_sequence(seq.id, req)

    # ── Export / import (deploy a sequence set across units) ─────────────────

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export sequences", "sequences.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        self._export_path = path
        self._set_status("exporting…")
        # Pull a fresh list so the file reflects the unit, not a stale in-memory view.
        self.hub.run_async(
            f"seqio_export:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequences())

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import sequences", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read file:\n{exc}")
            return
        if isinstance(doc, dict):
            raw = doc.get("sequences") or []
        elif isinstance(doc, list):
            raw = doc
        else:
            raw = []

        requests: List[Tuple[str, m.CreateSequenceRequest]] = []
        bad_parse: List[str] = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            try:
                req = m.CreateSequenceRequest(
                    name=name,
                    description=entry.get("description", "") or "",
                    steps=entry.get("steps", []) or [],
                    types=entry.get("types") or [],
                )
            except Exception as exc:  # noqa: BLE001 — malformed step schema
                bad_parse.append(f"• {name}: {exc}")
                continue
            requests.append((name, req))

        if not requests:
            msg = "No valid sequences found in that file."
            if bad_parse:
                msg += "\n\n" + "\n".join(bad_parse)
            QMessageBox.information(self, "Import", msg)
            return

        prompt = (f"Create {len(requests)} sequence(s) on {self.hostname}?\n"
                  f"Existing sequences with the same name are skipped.")
        if bad_parse:
            prompt += f"\n\n{len(bad_parse)} entry(ies) could not be read and will be ignored."
        if QMessageBox.question(self, "Import sequences", prompt) != \
                QMessageBox.StandardButton.Yes:
            return
        client = self.hub.fleet.get(self.hostname)
        self._set_status("importing…")
        self.hub.run_async(f"seqio_import:{self.hostname}",
                           lambda: _import_sequences(client, requests))

    def _on_io_done(self, label: str, result) -> None:
        parts = label.split(":")
        if not label.startswith("seqio_") or len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]
        if op == "seqio_export":
            target = self._export_path
            self._export_path = None
            if isinstance(result, Exception) or not target:
                self._set_status("export failed", error=True)
                QMessageBox.warning(self, "Export failed", f"{result}")
                return
            seqs = result if isinstance(result, list) else []
            try:
                with open(target, "w", encoding="utf-8", newline="") as fh:
                    fh.write(sequences_to_yaml(seqs))
            except OSError as exc:
                self._set_status("export failed", error=True)
                QMessageBox.warning(self, "Export failed", f"Could not write file:\n{exc}")
                return
            self._set_status(f"exported {len(seqs)} sequence(s)")
            QMessageBox.information(
                self, "Export", f"{len(seqs)} sequence(s) written to\n{target}")
        elif op == "seqio_import":
            if isinstance(result, Exception) or not isinstance(result, list):
                self._set_status("import failed", error=True)
                QMessageBox.warning(self, "Import failed", f"{result}")
                return
            ok = [n for n, e in result if e is None]
            bad = [(n, e) for n, e in result if e is not None]
            msg = f"Created {len(ok)} sequence(s)."
            if bad:
                msg += "\n\nSkipped / failed:\n" + "\n".join(f"• {n}: {e}" for n, e in bad)
            QMessageBox.information(self, "Import complete", msg)
            self._refresh()

    # ── Live events ──────────────────────────────────────────────────────────

    def _on_event(self, ev) -> None:
        # Only sequence lifecycle events change run state; ignore the rest.
        if not isinstance(ev, m.SequenceWebhook):
            return
        try:
            uid = self.hub.fleet.get(self.hostname).unit_id
        except KeyError:
            return
        if getattr(ev, "unit_id", None) == uid:
            self._refresh_runs()

    # ── Result routing ───────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("seq_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "seq_list":
            if isinstance(result, Exception):
                self._set_status(f"error: {result}", error=True)
                self._sequences = []
            else:
                self._sequences = result if isinstance(result, list) else []
            self._seq_loaded = True
            self._rebuild()
        elif op == "seq_runs":
            self._runs_pending = False
            if not isinstance(result, Exception):
                self._runs = result if isinstance(result, list) else []
                self._rebuild()
        elif op == "seq_arm":
            if isinstance(result, Exception):
                self._set_status("arm failed", error=True)
                QMessageBox.warning(self, "Could not arm sequence", str(result))
            else:
                self._set_status("armed")
            self._refresh_runs()
        elif op == "seq_proceed":
            if isinstance(result, Exception):
                self._set_status("proceed failed", error=True)
                QMessageBox.warning(self, "Could not proceed", str(result))
            else:
                self._set_status("proceeding — post-hold window scheduled")
            self._refresh_runs()
        elif op == "seq_holdnow":
            if isinstance(result, Exception):
                self._set_status("hold-now failed", error=True)
                QMessageBox.warning(self, "Could not fast-forward to the Hold", str(result))
            else:
                self._set_status("holding — fast-forwarded to the Hold")
            self._refresh_runs()
        elif op == "seq_stop":
            if isinstance(result, list):
                bad = [(rid, e) for rid, e in result if e is not None]
                if bad:
                    lines = "\n".join(f"• {rid}: {e}" for rid, e in bad)
                    QMessageBox.warning(self, "Stop — some runs failed", lines)
                    self._set_status("stop: some runs failed", error=True)
                else:
                    self._set_status("stopped")
            elif isinstance(result, Exception):
                self._set_status(f"stop failed: {result}", error=True)
            self._refresh_runs()
        elif op == "seq_delete":
            if isinstance(result, Exception):
                self._set_status(f"delete failed: {result}", error=True)
            self._refresh()

    # ── Rendering ────────────────────────────────────────────────────────────

    def _active_run_for(self, seq: m.Sequence) -> Optional[m.SequenceRun]:
        for r in self._runs:
            if r.sequence_id == seq.id and r.state in _ACTIVE:
                return r
        return None

    def _rebuild(self) -> None:
        # Drop any edit-while-holding revisions for runs that are no longer HOLDING
        # (proceeded — the edit was applied — or aborted).
        if self._wb_edits:
            holding_ids = {r.id for r in self._runs if r.state == m.SequenceState.HOLDING}
            self._wb_edits = {rid: v for rid, v in self._wb_edits.items() if rid in holding_ids}

        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._sequences:
            if self._seq_loaded:
                if self.can_edit:
                    msg = "No sequences in the library yet. Click “New sequence” to create one."
                else:
                    msg = ("No sequences deployed to this unit. Add them in the Library "
                           "and deploy.")
                empty = QLabel(msg)
                empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                empty.setWordWrap(True)
                self._list.addWidget(empty)
            return

        # Library view is scoped to the selected unit type (its own + shared); a unit
        # card is already scoped to itself, so it shows everything it holds.
        want = self._active_type if self.can_edit else _SEQ_FILTER_ALL
        query = self._search.text().strip().lower()
        # Stable alphanumeric order — so editing a sequence never reorders the list.
        seqs = sorted(self._sequences, key=lambda s: natural_key(s.name or s.id))
        active_n = 0
        shown = 0
        # The two Hold capabilities are per-unit — resolve them once, not per row.
        hold_now_ok = self.can_run and self._supports(SEQUENCE_HOLD_NOW_CAPABILITY)
        edit_wb_ok = self.can_run and self._supports(SEQUENCE_HOLD_EDIT_CAPABILITY)
        for seq in seqs:
            if want != _SEQ_FILTER_ALL and not m.applies_to_type(seq.types, want):
                continue
            if query and query not in (seq.name or "").lower() \
                    and query not in (seq.description or "").lower():
                continue
            active = self._active_run_for(seq)
            if active is not None:
                active_n += 1
            self._list.addWidget(_SequenceRow(
                seq, active,
                on_start=self._on_start, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete,
                on_log=self._on_log, can_edit=self.can_edit, can_run=self.can_run,
                show_scope=self.can_edit, on_proceed=self._on_proceed,
                on_hold_now=self._on_hold_now, hold_now_ok=hold_now_ok,
                on_edit_wb=self._on_edit_wb, edit_wb_ok=edit_wb_ok,
            ))
            shown += 1
        if shown == 0:
            if query:
                scope = "" if want == _SEQ_FILTER_ALL else f"{UNIT_TYPE_LABELS.get(want, want)} "
                msg = f"No {scope}sequences match “{query}”."
            elif want != _SEQ_FILTER_ALL:
                msg = (f"No {UNIT_TYPE_LABELS.get(want, want)} sequences yet. "
                       "Click “New sequence” to add one (set its scope to Shared "
                       "in the editor to apply it to all units).")
            else:
                msg = None
            if msg:
                empty = QLabel(msg)
                empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                empty.setWordWrap(True)
                self._list.addWidget(empty)
        active_txt = f" · {active_n} active" if active_n else ""
        if query:
            count_txt = f"{shown} sequence(s) match · {len(self._sequences)} total"
        elif want == _SEQ_FILTER_ALL:
            count_txt = f"{len(self._sequences)} sequence(s)"
        else:
            count_txt = (f"{shown} sequence(s) for {UNIT_TYPE_LABELS.get(want, want)} "
                         f"· {len(self._sequences)} total")
        self._set_status(f"{count_txt}{active_txt}")

    def set_active_type(self, unit_type: str) -> None:
        self._active_type = unit_type
        if self.can_edit:
            self._rebuild()

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
