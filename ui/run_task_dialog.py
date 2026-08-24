"""
RunTaskDialog — start one deployed task with ad-hoc parameters, without touching
the task's stored definition or the Library.

The unit's Tasks tab lists what's deployed and can Start/Stop each task with its
saved defaults. When you're bench-testing a script you often want to nudge a
parameter and run again — previously that meant editing the task in the Library,
deploying to the unit, then coming back here to Start. This dialog cuts that
loop: it reads the task's current command from the unit, renders the script's
parameter form (the same ui.param_form.ParamForm the task/step editors use)
pre-filled with the deployed values, and Start launches the task with
`replace_args=True` so the form's values replace the trailing args for this run
only. The deployed task definition is left exactly as it was.

Reads are on demand and go through the DataHub (run_async → task_done), filtered
to this dialog's host + operations: the task's command (tasks.yaml) on open, then
the script's parameters once the command names the script.
"""
from __future__ import annotations

import shlex
from typing import List, Optional

import yaml

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit, QScrollArea, QVBoxLayout,
)

from api import models as m

from .param_form import ParamForm
from .qt_adapter import DataHub
from .theme import Palette


class RunTaskDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, task_name: str,
                 running: bool = False, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.task_name = task_name
        self._running = running

        self._interp = "python3"           # from the task's command
        self._script_path = ""             # full path as configured on the unit
        self._script_name = ""             # basename, used to fetch params
        self._param_specs: List[dict] = []
        self._current_args: List[str] = []
        self._params_inflight = False
        self._starting = False
        # Per-unit power calibration: the task's opt-in signal (env) and the resolved
        # bounds for it, so the --power field shows this unit's real min/max.
        self._cal_signal_id: Optional[str] = None
        self._cal_bounds = None
        self._params_ready = False
        self._cal_ready = False

        self.setWindowTitle(f"Run '{task_name}' with parameters")
        self.setMinimumWidth(560)
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
            "Start this task once with the parameters below. This does not change "
            "the deployed task definition — it only overrides the arguments for "
            "this run.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(blurb)

        # The parameter form (scrolls when a script has many arguments).
        self._form = ParamForm()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._form)
        scroll.setMinimumHeight(120)
        root.addWidget(scroll, stretch=1)

        extra_form = QFormLayout()
        extra_form.setContentsMargins(0, 0, 0, 0)
        self._extra = QLineEdit()
        self._extra.setPlaceholderText("--flag value  (anything not in the form above)")
        self._extra.textChanged.connect(self._update_preview)
        extra_form.addRow("Additional args", self._extra)
        root.addLayout(extra_form)

        prev_lbl = QLabel("Command")
        prev_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(prev_lbl)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setFixedHeight(52)
        self._preview.setFont(QFont("monospace"))
        root.addWidget(self._preview)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        root.addWidget(self._status)

        self._buttons = QDialogButtonBox()
        self._run_btn = self._buttons.addButton(
            "Start", QDialogButtonBox.ButtonRole.AcceptRole)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_run)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        self._form.changed.connect(self._update_preview)
        if self._running:
            self._set_status(
                "This task is already running — stop it first, or starting will "
                "fail.", error=True)

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._set_status("loading task…")
        self.hub.run_async(
            f"runtask_yaml:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml(),
        )

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("runtask_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        if op == "runtask_start":
            self._starting = False
            self._buttons.setEnabled(True)
            if isinstance(result, Exception):
                self._set_status(f"start failed: {result}", error=True)
            else:
                self.hub.refresh_now(self.hostname)   # reflect the running state immediately
                self.accept()
            return

        if op == "runtask_cal":
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
            self._set_status(f"error: {result}", error=True)
            return

        if op == "runtask_yaml":
            self._parse_command(result if isinstance(result, str) else "")
        elif op == "runtask_params":
            self._params_inflight = False
            self._params_ready = True
            self._param_specs = (result or {}).get("params", [])
            self._maybe_build()

    # ── Parse the task's command → interpreter + script + args ──────────────────

    def _parse_command(self, yaml_text: str) -> None:
        try:
            doc = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError:
            doc = {}
        entry = next((t for t in doc.get("tasks", [])
                      if t.get("name") == self.task_name), None)
        if not entry:
            self._set_status(
                "Couldn't read this task's definition from the unit.", error=True)
            return

        # The task opts into calibration by setting this env to the script's signal id.
        self._cal_signal_id = (entry.get("env") or {}).get("SDR_CAL_SIGNAL_ID")

        command = list(entry.get("command", []))
        if command:
            self._interp = command[0]
        script_idx = next((i for i, a in enumerate(command)
                           if isinstance(a, str) and a.endswith(".py")), None)
        if script_idx is None:
            self._set_status(
                "This task doesn't run a .py script, so it has no parameter form. "
                "Use “Additional args” to pass options, then Start.", error=False)
            self._current_args = command[1:] if len(command) > 1 else []
            self._extra.setText(" ".join(shlex.quote(a) for a in self._current_args))
            return

        self._script_path = command[script_idx]
        self._script_name = self._script_path.rsplit("/", 1)[-1]
        self._current_args = command[script_idx + 1:]
        self._fetch_params()
        self._fetch_calibration()

    def _fetch_params(self) -> None:
        if not self._script_name or self._params_inflight:
            return
        self._params_inflight = True
        self._set_status(f"loading parameters for {self._script_name}…")
        self.hub.run_async(
            f"runtask_params:{self.hostname}:{self._script_name}",
            lambda: self.hub.fleet.get(self.hostname).get_script_params(self._script_name),
        )

    def _fetch_calibration(self) -> None:
        # If this task opts into calibration, fetch the unit's resolved bounds so the
        # --power field reflects the real range. Uncalibrated (404) → no bounds.
        if not self._cal_signal_id:
            self._cal_ready = True
            self._maybe_build()
            return
        self.hub.run_async(
            f"runtask_cal:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).get_calibration(),
        )

    def _maybe_build(self) -> None:
        if self._params_ready and self._cal_ready:
            self._build_form()

    def _build_form(self) -> None:
        # Open in the mode the deployed command used (relative if it set --gain).
        mode = "relative" if any(a in ("-Gain", "--gain") for a in self._current_args) else None
        from .param_form import calibration_caution
        caution = calibration_caution(bool(self._cal_signal_id), targeted=True,
                                      calibrated=self._cal_bounds is not None)
        self._form.set_params(self._param_specs, cal_bounds=self._cal_bounds,
                              absolute_allowed=True, default_power_mode=mode,
                              caution=caution)
        # Prefill from the deployed args; anything the form doesn't recognise
        # (positional args, flags not in the schema) drops into "Additional args".
        extra = self._form.set_values(self._current_args)
        if extra:
            self._extra.setText(" ".join(shlex.quote(e) for e in extra))
        self._set_status("")
        self._update_preview()

    # ── Preview / run ───────────────────────────────────────────────────────────

    def _override_args(self) -> List[str]:
        args = self._form.build_args()
        extra = self._extra.text().strip()
        if extra:
            try:
                args = args + shlex.split(extra)
            except ValueError:
                pass
        return args

    def _update_preview(self, *_) -> None:
        if not self._script_path:
            self._preview.setPlainText("—")
            return
        cmd = [self._interp, self._script_path] + self._override_args()
        self._preview.setPlainText(" ".join(shlex.quote(c) for c in cmd))

    def _on_run(self) -> None:
        if self._starting:
            return
        err = self._form.validate()
        if err:
            self._set_status(err, error=True)
            return
        args = self._override_args()
        # replace_args=True: the form's values become the task's trailing args for
        # this run only. With no args (script has no params, nothing added) the
        # agent falls back to the task's configured command, so Start still works.
        req = m.StartRequest(args=args, replace_args=True)
        self._starting = True
        self._buttons.setEnabled(False)
        self._set_status("starting…")
        self.hub.run_async(
            f"runtask_start:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).start_task(self.task_name, req),
        )

    # ── Misc ────────────────────────────────────────────────────────────────────

    def _set_status(self, text: str, error: bool = False) -> None:
        colour = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setStyleSheet(f"font-size: 11px; color: {colour};")
        self._status.setText(text)

    def _disconnect(self) -> None:
        try:
            self.hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
