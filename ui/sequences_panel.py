"""
SequencesPanel — the Sequences sub-tab of the unit detail view.

Lists the sequences stored on this unit (GET /sequences), each with a short
timeline summary and a live run-state pill, and lets you:

  - New     → create a sequence (SequenceEditorDialog)
  - Export  → save every sequence on this unit to a portable YAML file
  - Import  → create sequences from a YAML file (existing names skipped)
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
    seqio_export:<host>
    seqio_import:<host>
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m
from .qt_adapter import DataHub
from .sequence_editor import SequenceEditorDialog
from .sequence_log_dialog import SequenceLogDialog
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
    def action_of(s) -> str:
        return s.action.value if hasattr(s.action, "value") else str(s.action)

    def desc(s) -> str:
        glyph = "▶" if action_of(s) == "start" else "⏹"
        args = getattr(s, "args", None) or []
        argstr = f" {' '.join(args)}" if (action_of(s) == "start" and args) else ""
        return f"{glyph} {s.task_name}{argstr} {_fmt_offset(s.offset_s)}"

    on = sorted((s for s in seq.steps if s.anchor == "start"), key=lambda s: s.offset_s)
    off = sorted((s for s in seq.steps if s.anchor == "stop"), key=lambda s: s.offset_s)
    on_txt = ", ".join(desc(s) for s in on)
    off_txt = ", ".join(desc(s) for s in off)
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


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _arm_now(client, seq: m.Sequence) -> m.SequenceRun:
    """
    Arm a sequence to run now, basing the on-air time on the AGENT's clock rather
    than the laptop's. The agent schedules and fires steps against its own clock,
    so if the two clocks aren't in sync (e.g. a Pi with no NTP on an isolated
    ethernet link), a laptop-computed on_air_at lands at the wrong Pi-time and the
    run just sits armed. Reading the unit's utc_now first makes Start skew-proof.
    Falls back to the laptop clock if /system is unavailable. Runs on a worker
    thread (two quick calls: /system then arm).
    """
    now = None
    try:
        health = client.system()
        if health.utc_now:
            now = _parse_iso(health.utc_now)
    except Exception:  # noqa: BLE001 — best-effort; fall back to the laptop clock
        now = None
    req = arm_now_request(seq, now=now)
    return client.arm_sequence(seq.id, req)


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
        docs.append({
            "name": seq.name,
            "description": seq.description,
            "steps": [s.model_dump(mode="json") for s in seq.steps],
        })
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
                 can_edit: bool = True, can_run: bool = True):
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

        # The run-state pill and Start/Stop/Log belong to a live unit (can_run);
        # Edit/Delete are definition editing (can_edit, i.e. the Library).
        if can_run:
            state_word = active_run.state.value if active else "idle"
            self._pill = StatusPill(state_word, state_word)
            lay.addWidget(self._pill, alignment=Qt.AlignmentFlag.AlignTop)

        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        self._log = QPushButton("Log")
        self._edit = QPushButton("Edit")
        self._delete = QPushButton("Delete")
        for b in (self._start, self._stop, self._log, self._edit, self._delete):
            b.setFixedWidth(66)
        self._start.setToolTip("Arm & run now (open-ended) — fires the on-air steps; "
                               "use Stop to end")
        self._stop.setToolTip("Stop this run — cancels if armed, aborts if running "
                              "(stops every task it touches)")
        self._log.setToolTip("View this sequence's run log — the whole run's timeline "
                             "and each step's output, live")
        self._start.setEnabled(not active)
        self._stop.setEnabled(active)
        self._delete.setEnabled(not active)   # the agent refuses to delete an active one
        self._start.clicked.connect(lambda: on_start(seq))
        self._stop.clicked.connect(lambda: on_stop(seq))
        self._log.clicked.connect(lambda: on_log(seq))
        self._edit.clicked.connect(lambda: on_edit(seq))
        self._delete.clicked.connect(lambda: on_delete(seq))
        shown = []
        if can_run:
            shown += [self._start, self._stop, self._log]
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
        self._sequences: List[m.Sequence] = []
        self._runs: List[m.SequenceRun] = []
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
        dlg = SequenceEditorDialog(self.hub, self.hostname, parent=self.window())
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
        client = self.hub.fleet.get(self.hostname)
        self._set_status(f"arming {seq.name or seq.id}…")
        self.hub.run_async(
            f"seq_arm:{self.hostname}:{seq.id}",
            lambda: _arm_now(client, seq),
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

        active_n = 0
        for seq in self._sequences:
            active = self._active_run_for(seq)
            if active is not None:
                active_n += 1
            self._list.addWidget(_SequenceRow(
                seq, active,
                on_start=self._on_start, on_stop=self._on_stop,
                on_edit=self._on_edit, on_delete=self._on_delete,
                on_log=self._on_log, can_edit=self.can_edit, can_run=self.can_run,
            ))
        suffix = f" · {active_n} active" if active_n else ""
        self._set_status(f"{len(self._sequences)} sequence(s){suffix}")

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")
