"""
LiveTuneDialog — adjust a running task's live parameters in place.

Parameters a script declares ``live=True`` (paramkit) can be retuned while the
task runs — gain, frequency, amplitude, … This dialog renders just those params
(the shared ui.param_form.ParamForm, so ranges/units/presets come for free),
seeds them with the task's current values, and pushes each change to the unit as
you make it (debounced). The unit reports back the value the device actually took
— a gain quantised to the nearest step, say — which is shown in the result line,
alongside anything it rejected.

Nothing here edits the deployed task definition; it only tunes the live run.

Async reads/writes go through the DataHub (run_async → task_done), filtered to
this dialog's host + operations: the task's command (tasks.yaml) → the script's
schema (live params only) → the current live values → then set-params on change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout,
)

from .param_form import ParamForm, fmt_value
from .qt_adapter import DataHub
from .theme import Palette

APPLY_DEBOUNCE_MS = 350


class LiveTuneDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, task_name: str, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.task_name = task_name
        self._script_name = ""
        self._live_specs: List[dict] = []
        self._loading = True          # suppress auto-apply while we seed the form
        self._applying = False

        self.setWindowTitle(f"Tune '{task_name}' (live)")
        self.setMinimumWidth(520)
        self._build()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(APPLY_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply)

        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        blurb = QLabel(
            "Changes below are applied to the running task immediately. The "
            "deployed task definition is not modified.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(blurb)

        self._form = ParamForm()
        self._form.changed.connect(self._on_changed)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._form)
        scroll.setMinimumHeight(120)
        root.addWidget(scroll, stretch=1)

        self._result = QLabel("")
        self._result.setWordWrap(True)
        self._result.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(self._result)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self.accept)
        root.addWidget(self._buttons)

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._set_result("loading task…")
        self.hub.run_async(
            f"livetune_yaml:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml(),
        )

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("livetune_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "livetune_set":
            self._applying = False
            self._show_set_result(result)
            return

        if isinstance(result, Exception):
            self._set_result(f"error: {result}", error=True)
            return

        if op == "livetune_yaml":
            self._parse_command(result if isinstance(result, str) else "")
        elif op == "livetune_params":
            specs = (result or {}).get("params", [])
            self._live_specs = [s for s in specs if s.get("live")]
            self._form.set_params(self._live_specs)
            if not self._live_specs:
                self._set_result("This task declares no live parameters.")
                self._form.setEnabled(False)
                return
            self._fetch_current()
        elif op == "livetune_get":
            self._seed_values(result if isinstance(result, dict) else {})

    def _parse_command(self, yaml_text: str) -> None:
        try:
            doc = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError:
            doc = {}
        entry = next((t for t in doc.get("tasks", [])
                      if t.get("name") == self.task_name), None)
        command = list(entry.get("command", [])) if entry else []
        script_idx = next((i for i, a in enumerate(command)
                           if isinstance(a, str) and a.endswith(".py")), None)
        if script_idx is None:
            self._set_result("This task doesn't run a paramkit script.", error=True)
            self._form.setEnabled(False)
            return
        self._script_name = command[script_idx].rsplit("/", 1)[-1]
        self.hub.run_async(
            f"livetune_params:{self.hostname}:{self._script_name}",
            lambda: self.hub.fleet.get(self.hostname).get_script_params(self._script_name),
        )

    def _fetch_current(self) -> None:
        self.hub.run_async(
            f"livetune_get:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_task_params(self.task_name),
        )

    def _seed_values(self, snapshot: Dict[str, Any]) -> None:
        # Prefer the value the device actually took; fall back to the requested.
        current = {}
        current.update(snapshot.get("current") or {})
        current.update(snapshot.get("applied") or {})
        args: List[str] = []
        for spec in self._live_specs:
            name = spec.get("name") or spec.get("dest")
            if name in current and current[name] is not None:
                flag = spec["flags"][0] if spec.get("flags") else None
                if flag:
                    args += [flag, fmt_value(current[name])]
        self._loading = True
        self._form.set_values(args)
        self._loading = False
        self._set_result("Ready — adjust a value to apply it live.")

    # ── Apply on change ──────────────────────────────────────────────────────────

    def _on_changed(self) -> None:
        if self._loading:
            return
        self._debounce.start()

    def _apply(self) -> None:
        if self._applying:
            self._debounce.start()      # a set is in flight; retry shortly
            return
        err = self._form.validate()
        if err:
            self._set_result(err, error=True)
            return
        values = self._form.values()
        if not values:
            return
        self._applying = True
        self._set_result("applying…")
        self.hub.run_async(
            f"livetune_set:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).set_task_params(
                self.task_name, values, 1.0),
        )

    def _show_set_result(self, result) -> None:
        if isinstance(result, Exception):
            self._set_result(f"apply failed: {result}", error=True)
            return
        if not isinstance(result, dict):
            self._set_result("apply: unexpected response", error=True)
            return
        applied = result.get("applied") or {}
        rejected = result.get("rejected") or {}
        pending = result.get("pending") or []
        bits = []
        if applied:
            bits.append("applied " + ", ".join(
                f"{k}={_fmt(applied[k])}" for k in applied if k not in pending))
        if pending:
            bits.append("pending: " + ", ".join(pending))
        if rejected:
            bits.append("rejected " + "; ".join(
                f"{k} ({v})" for k, v in rejected.items()))
        self._set_result("  ·  ".join(b for b in bits if b) or "no change",
                         error=bool(rejected))

    # ── Misc ────────────────────────────────────────────────────────────────────

    def _set_result(self, text: str, error: bool = False) -> None:
        colour = Palette.CRASH if error else Palette.TEXT_FAINT
        self._result.setStyleSheet(f"font-size: 11px; color: {colour};")
        self._result.setText(text)

    def _disconnect(self) -> None:
        try:
            self.hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass


def _fmt(v: Any) -> str:
    return fmt_value(v) if isinstance(v, (int, float)) else str(v)
