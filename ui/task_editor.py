"""
TaskEditorDialog — create or edit a task from a script.

Pick a script; the dialog fetches its argparse parameters (GET /scripts/{name}/params)
and renders a typed form — a widget per argument, defaults prefilled, required
fields validated strictly. Save builds the command
    [interpreter, <scripts_dir>/<script>, --flag, value, …]
and calls create_task / update_task (which the agent writes to tasks.yaml and
reloads live). A read-only preview shows the exact command that will be created.

The parameter form itself is the shared ui.param_form.ParamForm — the same widget
the sequence step editor uses — so a task and a sequence step configure a script's
parameters identically.

Network calls go through the DataHub's run_async and come back on task_done,
filtered to this dialog's host + operations (the modal exec loop still processes
those queued signals). Reads are on demand: the script list on open, a script's
params when it's selected, and — for Edit — tasks.yaml to prefill from the
existing command.
"""
from __future__ import annotations

import shlex
from typing import Dict, List, Optional

import yaml

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QVBoxLayout,
    QWidget,
)

from api.fleet import LIBRARY_HOST
from api.models import UNIT_TYPE_SCRIPTS_DIR
from .param_form import ParamForm
from .qt_adapter import DataHub
from .scope_selector import ScopeSelector
from .theme import Palette

DEFAULT_SCRIPTS_DIR = UNIT_TYPE_SCRIPTS_DIR["broadcaster"]   # /opt/sdr-agent/scripts
DEFAULT_INTERPRETER = "python3"


class TaskEditorDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str,
                 existing_name: Optional[str] = None, default_types=None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.existing_name = existing_name        # None -> create, else edit
        self._default_types = list(default_types) if default_types else None
        self._param_specs: Dict[str, list] = {}   # script -> [param dict, ...]
        self._pending_prefill: Optional[List[str]] = None  # edit-mode command args to prefill
        self._edit_script: Optional[str] = None            # script to select once loaded
        self._params_inflight: set = set()         # scripts whose params fetch is in flight
        self._saving = False
        # Set to the task's name on a successful save, so a caller that opened this
        # dialog to create a task inline can learn which task was created.
        self.created_name: Optional[str] = None

        self.setWindowTitle("Edit task" if existing_name else "New task")
        self.setMinimumWidth(580)
        self._build()
        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self._name = QLineEdit()
        self._name.setPlaceholderText("unique task name")
        self._name.textChanged.connect(self._update_preview)
        form.addRow("Name *", self._name)

        self._script = QComboBox()
        self._script.currentTextChanged.connect(self._on_script_changed)
        form.addRow("Script *", self._script)

        self._desc = QLineEdit()
        self._desc.setPlaceholderText("optional")
        form.addRow("Description", self._desc)

        # Which unit types this task deploys to. Only meaningful for the canonical
        # library (a unit already holds just its own scoped items), so it's shown
        # in library mode only.
        self._scope: Optional[ScopeSelector] = None
        if self.hostname == LIBRARY_HOST:
            self._scope = ScopeSelector()
            # A new task opened from a unit-type view defaults to that type; editing
            # loads the task's own scope (see _load).
            if self.existing_name is None and self._default_types is not None:
                self._scope.set_from_types(self._default_types)
            form.addRow("Applies to", self._scope)
        outer.addLayout(form)

        # Dynamic parameter form (from the script's argparse/paramkit spec)
        params_box = QGroupBox("Parameters")
        pb = QVBoxLayout(params_box)
        pb.setContentsMargins(8, 8, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(160)
        self._form = ParamForm()
        self._form.changed.connect(self._update_preview)
        scroll.setWidget(self._form)
        pb.addWidget(scroll)
        outer.addWidget(params_box, stretch=1)

        # Extra args + toggles
        extra_form = QFormLayout()
        extra_form.setSpacing(8)
        self._extra = QLineEdit()
        self._extra.setPlaceholderText("extra arguments not shown above (optional)")
        self._extra.textChanged.connect(self._update_preview)
        extra_form.addRow("Additional args", self._extra)

        toggles = QHBoxLayout()
        self._autostart = QCheckBox("Autostart")
        self._restart = QCheckBox("Restart on crash")
        toggles.addWidget(self._autostart)
        toggles.addWidget(self._restart)
        toggles.addStretch(1)
        extra_form.addRow("", self._wrap(toggles))
        outer.addLayout(extra_form)

        # Advanced
        adv = QGroupBox("Advanced")
        av = QFormLayout(adv)
        av.setSpacing(8)
        self._interp = QLineEdit("python3")
        self._interp.textChanged.connect(self._update_preview)
        av.addRow("Interpreter", self._interp)
        self._scripts_dir = QLineEdit(self._default_scripts_dir())
        self._scripts_dir.textChanged.connect(self._update_preview)
        av.addRow("Script directory", self._scripts_dir)
        self._env = QPlainTextEdit()
        self._env.setPlaceholderText("KEY=value   (one per line)")
        self._env.setFixedHeight(60)
        av.addRow("Environment", self._env)
        outer.addWidget(adv)

        # Command preview
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mono = QFont("Consolas"); mono.setStyleHint(QFont.StyleHint.Monospace); mono.setPointSize(9)
        self._preview.setFont(mono)
        self._preview.setStyleSheet(
            f"background: #1E2530; color: #D6DCE5; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 6px; padding: 6px;"
        )
        outer.addWidget(self._preview)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _default_scripts_dir(self) -> str:
        """The script directory a new task starts with. On a live unit it's the Pi
        default, immediately overwritten by the unit's reported path (see the
        taskdlg_info handler). In the offline library there's no unit to ask, so a
        new task authored in a single unit-type view (Broadcaster/X410) defaults to
        that type's on-disk layout — e.g. an X410 task lands under /data, not /opt."""
        if self.hostname == LIBRARY_HOST and self.existing_name is None and self._default_types:
            known = [t for t in self._default_types if t in UNIT_TYPE_SCRIPTS_DIR]
            if len(known) == 1:
                return UNIT_TYPE_SCRIPTS_DIR[known[0]]
        return DEFAULT_SCRIPTS_DIR

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._set_status("loading scripts…")
        self.hub.run_async(
            f"taskdlg_scripts:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_scripts(),
        )
        # Ask the unit where it keeps scripts and which interpreter its tasks use, so
        # a NEW task's defaults match this unit (an X410 uses /data/... + system
        # python3, not the Pi's /opt/... ) instead of the operator re-typing them.
        # Skipped in library mode (no live unit to ask).
        if self.hostname != LIBRARY_HOST:
            self.hub.run_async(
                f"taskdlg_info:{self.hostname}",
                lambda: self.hub.fleet.get(self.hostname).info(),
            )
        if self.existing_name:
            self.hub.run_async(
                f"taskdlg_yaml:{self.hostname}",
                lambda: self.hub.fleet.get(self.hostname).get_tasks_yaml(),
            )

    def _on_script_changed(self, script: str) -> None:
        self._update_preview()
        if not script:
            return
        if script in self._param_specs:
            self._build_param_form(script)
            return
        if script in self._params_inflight:
            return   # a fetch is already on its way; its result will build the form
        self._params_inflight.add(script)
        self._set_status(f"loading parameters for {script}…")
        self.hub.run_async(
            f"taskdlg_params:{self.hostname}:{script}",
            lambda: self.hub.fleet.get(self.hostname).get_script_params(script),
        )

    def _select_script(self, name: str) -> None:
        """Programmatically select a script and fetch its params (no signal echo)."""
        if not name:
            return
        self._script.blockSignals(True)
        if self._script.findText(name) < 0:
            self._script.addItem(name)
        self._script.setCurrentText(name)
        self._script.blockSignals(False)
        self._on_script_changed(name)

    # ── Async results ────────────────────────────────────────────────────────

    def _on_task_done(self, label: str, result) -> None:
        if not label.startswith("taskdlg_"):
            return
        parts = label.split(":")
        if len(parts) < 2 or parts[1] != self.hostname:
            return
        op = parts[0]

        # A params fetch is done (success or failure) — clear its in-flight marker
        # so the script can be re-fetched later if needed.
        if op == "taskdlg_params":
            self._params_inflight.discard(":".join(parts[2:]))

        # Per-unit defaults for a NEW task: adopt the unit's scripts dir / interpreter
        # unless the operator has already changed those fields. Best-effort — a failed
        # /info just leaves the Pi defaults in place. Never touches an EDIT (its paths
        # come from the existing command via _prefill_from_yaml).
        if op == "taskdlg_info":
            if not isinstance(result, Exception) and not self.existing_name:
                sdir = getattr(result, "scripts_dir", "") or ""
                interp = getattr(result, "task_interpreter", "") or ""
                if sdir and self._scripts_dir.text().strip() == DEFAULT_SCRIPTS_DIR:
                    self._scripts_dir.setText(sdir)
                if interp and self._interp.text().strip() == DEFAULT_INTERPRETER:
                    self._interp.setText(interp)
                self._update_preview()
            return

        if op == "taskdlg_save":
            self._saving = False
            self._buttons.setEnabled(True)
            if isinstance(result, Exception):
                self._set_status(f"save failed: {result}", error=True)
            else:
                # Remember the created task's name (used when a caller opened this
                # dialog to create a task inline, e.g. from the sequence editor).
                self.created_name = self._name.text().strip()
                # Pull fresh data now so the new/edited task shows immediately,
                # instead of waiting for the next poll tick.
                self.hub.refresh_now(self.hostname)
                self.accept()
            return

        if isinstance(result, Exception):
            self._set_status(f"error: {result}", error=True)
            return

        if op == "taskdlg_scripts":
            names = result if isinstance(result, list) else []
            self._script.blockSignals(True)
            self._script.clear()
            self._script.addItems(names)
            self._script.blockSignals(False)
            self._set_status("")
            target = self._edit_script or (names[0] if names else "")
            if target:
                self._select_script(target)
        elif op == "taskdlg_params":
            script = ":".join(parts[2:])
            self._param_specs[script] = (result or {}).get("params", [])
            if script == self._script.currentText():
                self._build_param_form(script)
        elif op == "taskdlg_yaml":
            self._prefill_from_yaml(result if isinstance(result, str) else "")

    # ── Dynamic form ─────────────────────────────────────────────────────────

    def _build_param_form(self, script: str) -> None:
        self._form.set_params(self._param_specs.get(script, []))
        self._set_status("")
        # Prefill values on edit. Keyed to the edit script and deliberately NOT
        # consumed: the form for one script can be (re)built several times (a
        # redundant params result, reselecting the script), and consuming the
        # prefill after the first build left later rebuilds empty — the
        # "sometimes the fields are blank" race. Re-applying each build is
        # idempotent (set_params wipes the widgets first), and it only ever targets
        # the edit script, so it never bleeds into a different script's form.
        if self._pending_prefill is not None and script == self._edit_script:
            extra = self._form.set_values(self._pending_prefill)
            if extra:
                self._extra.setText(" ".join(shlex.quote(e) for e in extra))
        self._update_preview()

    # ── Prefill (edit) ───────────────────────────────────────────────────────

    def _prefill_from_yaml(self, text: str) -> None:
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            doc = {}
        entry = None
        for t in doc.get("tasks", []):
            if t.get("name") == self.existing_name:
                entry = t
                break
        if not entry:
            return
        self._name.setText(entry.get("name", ""))
        self._desc.setText(entry.get("description", ""))
        if self._scope is not None:
            self._scope.set_from_types(entry.get("types") or [])
        self._autostart.setChecked(bool(entry.get("autostart")))
        self._restart.setChecked(bool(entry.get("restart_on_crash")))
        env = entry.get("env") or {}
        self._env.setPlainText("\n".join(f"{k}={v}" for k, v in env.items()))
        wd = entry.get("working_dir")
        if wd:
            self._scripts_dir.setText(wd)

        command = list(entry.get("command", []))
        if command:
            self._interp.setText(command[0])
        # Find the script argument (first .py) -> its dir + basename
        script_idx = next((i for i, a in enumerate(command)
                           if isinstance(a, str) and a.endswith(".py")), None)
        if script_idx is not None:
            script_path = command[script_idx]
            script_name = script_path.rsplit("/", 1)[-1]
            script_dir = script_path[: -(len(script_name) + 1)] if "/" in script_path else ""
            if script_dir:
                self._scripts_dir.setText(script_dir)
            # Remember the target + stash the args to prefill once the form exists.
            self._edit_script = script_name
            self._pending_prefill = command[script_idx + 1:]
            # If the script list is already loaded, select now; otherwise the
            # scripts-list handler selects it when the list arrives.
            if self._script.count() > 0:
                self._select_script(script_name)

    # ── Command building / preview / save ────────────────────────────────────

    def _build_command(self) -> List[str]:
        interp = self._interp.text().strip() or "python3"
        script = self._script.currentText().strip()
        sdir = self._scripts_dir.text().strip().rstrip("/")
        if not script:
            return []
        script_path = f"{sdir}/{script}" if sdir else script
        cmd = [interp, script_path] + self._form.build_args()
        extra = self._extra.text().strip()
        if extra:
            try:
                cmd += shlex.split(extra)
            except ValueError:
                pass
        return cmd

    def _update_preview(self, *_) -> None:
        cmd = self._build_command()
        self._preview.setText(" ".join(shlex.quote(c) for c in cmd) if cmd else "—")

    def _parse_env(self) -> Optional[Dict[str, str]]:
        env: Dict[str, str] = {}
        for line in self._env.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                return None
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
        return env

    def _on_save(self) -> None:
        if self._saving:
            return
        name = self._name.text().strip()
        if not name:
            self._set_status("task name is required", error=True)
            return
        if not self._script.currentText().strip():
            self._set_status("select a script", error=True)
            return
        err = self._form.validate()
        if err:
            self._set_status(err, error=True)
            return
        env = self._parse_env()
        if env is None:
            self._set_status("environment lines must be KEY=value", error=True)
            return

        spec = {
            "name": name,
            "description": self._desc.text().strip(),
            "command": self._build_command(),
            "working_dir": self._scripts_dir.text().strip(),
            "env": env,
            "autostart": self._autostart.isChecked(),
            "restart_on_crash": self._restart.isChecked(),
        }
        if self._scope is not None:
            spec["types"] = self._scope.types()

        self._saving = True
        self._buttons.setEnabled(False)
        self._set_status("saving…")
        if self.existing_name:
            self.hub.run_async(
                f"taskdlg_save:{self.hostname}:{name}",
                lambda: self.hub.fleet.get(self.hostname).update_task(self.existing_name, spec),
            )
        else:
            self.hub.run_async(
                f"taskdlg_save:{self.hostname}:{name}",
                lambda: self.hub.fleet.get(self.hostname).create_task(spec),
            )

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _set_status(self, text: str, error: bool = False) -> None:
        color = Palette.CRASH if error else Palette.TEXT_FAINT
        self._status.setText(text)
        self._status.setStyleSheet(f"font-size: 11px; color: {color};")

    def _disconnect(self) -> None:
        try:
            self.hub.task_done.disconnect(self._on_task_done)
        except (TypeError, RuntimeError):
            pass
