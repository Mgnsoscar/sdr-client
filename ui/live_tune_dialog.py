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

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout,
)

from .param_form import ParamForm, fmt_value, power_mode_of_args
from .qt_adapter import DataHub
from .theme import Palette


class LiveTuneDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, task_name: str, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.task_name = task_name
        self._script_name = ""
        self._live_specs: List[dict] = []
        self._loading = True          # suppress the dirty marker while we seed
        self._applying = False
        self._dirty = False
        # Per-unit power calibration: reflect the real --power range while retuning.
        self._cal_signal_id = None
        self._cal_bounds = None
        self._params_ready = False
        self._cal_ready = False

        self.setWindowTitle(f"Tune '{task_name}' (live)")
        self.setMinimumWidth(520)
        self._build()

        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        blurb = QLabel(
            "Adjust the values, then press Update (or Enter) to apply them to the "
            "running task. The deployed task definition is not modified.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(blurb)

        self._form = ParamForm()
        self._form.changed.connect(self._mark_dirty)
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
        self._update_btn = self._buttons.addButton(
            "Update", QDialogButtonBox.ButtonRole.ApplyRole)
        self._update_btn.clicked.connect(self._apply)
        # Make Update the dialog's default button so Enter applies (rather than
        # Close closing the dialog); Close must not steal the default.
        self._update_btn.setDefault(True)
        self._update_btn.setAutoDefault(True)
        close = self._buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setDefault(False)
            close.setAutoDefault(False)
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

        if op == "livetune_cal":
            # 404 (uncalibrated) → schema range; offline → last-known cached bounds.
            from api.client import AgentConnectionError
            from state.calibration_cache import get_calibration_cache
            cache = get_calibration_cache()
            self._cal_bounds = None
            if isinstance(result, dict) and result.get("valid"):
                cache.put(self.hostname, result)
                self._cal_bounds = (result.get("signals") or {}).get(self._cal_signal_id)
            elif isinstance(result, AgentConnectionError):
                cached = cache.get(self.hostname)
                if cached:
                    self._cal_bounds = (cached.get("signals") or {}).get(self._cal_signal_id)
            self._cal_ready = True
            self._maybe_build()
            return

        if isinstance(result, Exception):
            self._set_result(f"error: {result}", error=True)
            return

        if op == "livetune_yaml":
            self._parse_command(result if isinstance(result, str) else "")
        elif op == "livetune_params":
            specs = (result or {}).get("params", [])
            self._live_specs = [s for s in specs if s.get("live")]
            self._params_ready = True
            self._maybe_build()
        elif op == "livetune_get":
            self._seed_values(result if isinstance(result, dict) else {})

    def _parse_command(self, yaml_text: str) -> None:
        try:
            doc = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError:
            doc = {}
        entry = next((t for t in doc.get("tasks", [])
                      if t.get("name") == self.task_name), None)
        self._cal_signal_id = (entry.get("env") or {}).get("SDR_CAL_SIGNAL_ID") if entry else None
        command = list(entry.get("command", [])) if entry else []
        # Open in the mode the deployed command used (absolute if it set --power, relative
        # if --gain), so a task running in one mode doesn't open showing the other's control.
        self._default_power_mode = power_mode_of_args(command)
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
        self._fetch_calibration()

    def _fetch_calibration(self) -> None:
        if not self._cal_signal_id:
            self._cal_ready = True
            self._maybe_build()
            return
        self.hub.run_async(
            f"livetune_cal:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )

    def _maybe_build(self) -> None:
        if not (self._params_ready and self._cal_ready):
            return
        self._form.set_params(self._live_specs, cal_bounds=self._cal_bounds,
                              absolute_allowed=True,
                              default_power_mode=getattr(self, "_default_power_mode", None))
        if not self._live_specs:
            self._set_result("This task declares no live parameters.")
            self._form.setEnabled(False)
            return
        self._fetch_current()

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
        self._dirty = False
        self._set_result("Ready — adjust a value and press Update to apply it.")

    # ── Apply on Update / Enter ──────────────────────────────────────────────────

    def _mark_dirty(self) -> None:
        """A value changed but hasn't been sent yet. Just a hint — nothing is
        applied until Update (or Enter)."""
        if self._loading:
            return
        self._dirty = True
        self._set_result("Unsaved changes — press Update to apply.")

    def _apply(self) -> None:
        if self._applying:
            return                      # a set is already in flight; ignore
        err = self._form.validate()
        if err:
            self._set_result(err, error=True)
            return
        values = self._form.values()
        if not values:
            return
        self._applying = True
        self._dirty = False
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
