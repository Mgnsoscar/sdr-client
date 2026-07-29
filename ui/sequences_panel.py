"""
SequencesPanel — the Sequences sub-tab of the unit detail view.

Lists the sequences stored on this unit (GET /sequences), each with a short
timeline summary and a live run-state pill, and lets you:

  - New     → create a sequence (SequenceEditorDialog)
  - Edit    → change an existing sequence (same dialog, prefilled)
  - Start   → arm it to run now (open-ended): fires the on-air steps as soon as
              the warm-up lead-in allows, then stays on air until stopped
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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from api import models as m
from .qt_adapter import DataHub
from .sequence_editor import SequenceEditorDialog
from .theme import Palette
from .widgets import StatusPill

# Seconds of headroom added when arming "now", so the first step is safely in the
# future even with a little clock skew between the laptop and the unit.
ARM_MARGIN_S = 5.0

_ACTIVE = (m.SequenceState.ARMED, m.SequenceState.RUNNING)

Result = Tuple[str, Optional[str]]   # (run_id, error-or-None)


def _fmt_offset(offset_s: float) -> str:
    n = int(offset_s) if float(offset_s).is_integer() else round(offset_s, 1)
    return f"+{n}s" if n > 0 else (f"{n}s" if n < 0 else "0s")


def summarize(seq: m.Sequence) -> str:
    """A one-line 'on-air: … · off-air: …' digest of a sequence's steps."""
    def glyph(action) -> str:
        a = action.value if hasattr(action, "value") else str(action)
        return "▶" if a == "start" else "⏹"

    on = sorted((s for s in seq.steps if s.anchor == "start"), key=lambda s: s.offset_s)
    off = sorted((s for s in seq.steps if s.anchor == "stop"), key=lambda s: s.offset_s)
    on_txt = ", ".join(f"{glyph(s.action)} {s.task_name} {_fmt_offset(s.offset_s)}" for s in on)
    off_txt = ", ".join(f"{glyph(s.action)} {s.task_name} {_fmt_offset(s.offset_s)}" for s in off)
    parts = []
    if on_txt:
        parts.append(f"on-air: {on_txt}")
    if off_txt:
        parts.append(f"off-air: {off_txt}")
    return "  ·  ".join(parts) if parts else "no steps"


def arm_now_request(seq: m.Sequence, now: Optional[datetime] = None) -> m.ArmSequenceRequest:
    """
    Build an ArmSequenceRequest that puts the sequence on air as soon as possible:
    on_air_at = now + warm-up lead-in + margin, open-ended (no scheduled stop).

    The lead-in is the most-negative start-anchored offset (e.g. a -120s warm-up
    step needs on-air 120s out so it doesn't fire in the past — the agent rejects
    a first step scheduled before now).
    """
    now = now or datetime.now(timezone.utc)
    starts = [s.offset_s for s in seq.steps if s.anchor == "start"]
    lead_in = max(0.0, -min(starts)) if starts else 0.0
    on_air_at = now + timedelta(seconds=lead_in + ARM_MARGIN_S)
    return m.ArmSequenceRequest(
        on_air_at=on_air_at.isoformat(),
        open_ended=True,
        note="manual test",
    )


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


class _SequenceRow(QFrame):
    """One sequence: name, summary, run-state pill, and action buttons."""

    def __init__(self, seq: m.Sequence, active_run: Optional[m.SequenceRun],
                 on_start, on_stop, on_edit, on_delete):
        super().__init__()
        self.seq = seq
        self.setObjectName("card")
        active = active_run is not None and active_run.state in _ACTIVE

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        box = QVBoxLayout()
        box.setSpacing(2)
        header = QLabel(seq.name or seq.id)
        header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        box.addWidget(header)
        if seq.description:
            desc = QLabel(seq.description)
            desc.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            box.addWidget(desc)
        summary = QLabel(f"{len(seq.steps)} step(s)  ·  {summarize(seq)}")
        summary.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        summary.setWordWrap(True)
        box.addWidget(summary)
        lay.addLayout(box, stretch=1)

        state_word = active_run.state.value if active else "idle"
        self._pill = StatusPill(state_word, state_word)
        lay.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignTop)

        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        for b in (self._start, self._stop, self._edit, self._delete):
            b.setFixedWidth(66)
        self._start.setToolTip("Arm & run now (open-ended) — fires the on-air steps; "
                               "use Stop to end")
        self._stop.setToolTip("Stop this run — cancels if armed, aborts if running "
                              "(stops every task it touches)")
        self._start.setEnabled(not active)
        self._stop.setEnabled(active)
        self._delete.setEnabled(not active)   # the agent refuses to delete an active one
        self._start.clicked.connect(lambda: on_start(seq))
        self._stop.clicked.connect(lambda: on_stop(seq))
        self._edit.clicked.connect(lambda: on_edit(seq))
        self._delete.clicked.connect(lambda: on_delete(seq))
        for b in (self._start, self._stop, self._edit, self._delete):
            lay.addWidget(b, alignment=Qt.AlignmentFlag.AlignTop)


class SequencesPanel(QWidget):
    def __init__(self, hostname: str, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.hub = hub
        self._sequences: List[m.Sequence] = []
        self._runs: List[m.SequenceRun] = []
        self._seq_loaded = False
        self._runs_pending = False
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        # Live-refresh run state when a sequence lifecycle event arrives.
        self.hub.event_received.connect(self._on_event)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        self._new_btn = QPushButton("New sequence")
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

    # ── Shown / refresh ──────────────────────────────────────────────────────

    def on_shown(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("loading…")
        self.hub.run_async(
            f"seq_list:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequences(),
        )
        self._refresh_runs()

    def _refresh_runs(self) -> None:
        if self._runs_pending:
            return
        self._runs_pending = True
        self.hub.run_async(
            f"seq_runs:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_sequence_runs(),
        )

    # ── Actions ──────────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        dlg = SequenceEditorDialog(self.hub, self.hostname, parent=self.window())
        if dlg.exec():
            self._refresh()

    def _on_edit(self, seq: m.Sequence) -> None:
        dlg = SequenceEditorDialog(self.hub, self.hostname, sequence=seq, parent=self.window())
        if dlg.exec():
            self._refresh()

    def _on_start(self, seq: m.Sequence) -> None:
        req = arm_now_request(seq)
        self._set_status(f"arming {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_arm:{self.hostname}:{seq.id}",
            lambda: self.hub.fleet.get(self.hostname).arm_sequence(seq.id, req),
        )

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
        resp = QMessageBox.question(
            self, "Delete sequence",
            f"Delete sequence '{seq.name or seq.id}' from {self.hostname}?\n"
            f"This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"deleting {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_delete:{self.hostname}:{seq.id}",
            lambda: self.hub.fleet.get(self.hostname).delete_sequence(seq.id),
        )

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
                QMessageBox.warning(self, "Could not start sequence", str(result))
            else:
                self._set_status("armed")
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
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._sequences:
            if self._seq_loaded:
                empty = QLabel("No sequences on this unit yet. "
                               "Click “New sequence” to create one.")
                empty.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_FAINT};")
                self._list.addWidget(empty)
            return

        active_n = 0
        for seq in self._sequences:
            active = self._active_run_for(seq)
            if active is not None:
                active_n += 1
            self._list.addWidget(_SequenceRow(
                seq, active,
                on_start=self._on_start, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete,
            ))
        suffix = f" · {active_n} active" if active_n else ""
        self._set_status(f"{len(self._sequences)} sequence(s){suffix}")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
