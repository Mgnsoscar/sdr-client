"""
TaskEditorDialog — create or edit a task from a script.

Pick a script; the dialog fetches its argparse parameters (GET /scripts/{name}/params)
and renders a typed form — a widget per argument, defaults prefilled, required
fields validated strictly. Save builds the command
    [interpreter, <scripts_dir>/<script>, --flag, value, …]
and calls create_task / update_task (which the agent writes to tasks.yaml and
reloads live). A read-only preview shows the exact command that will be created.

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
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea,
    QVBoxLayout, QWidget,
)

from .qt_adapter import DataHub
from .theme import Palette

DEFAULT_SCRIPTS_DIR = "/opt/sdr-agent/scripts"


class TaskEditorDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str,
                 existing_name: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.existing_name = existing_name        # None -> create, else edit
        self._param_specs: Dict[str, list] = {}   # script -> [param dict, ...]
        self._param_widgets: Dict[str, tuple] = {}  # dest -> (widget, spec)
        self._pending_prefill: Optional[List[str]] = None  # command args awaiting the form
        self._edit_script: Optional[str] = None            # script to select once loaded
        self._saving = False

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
        outer.addLayout(form)

        # Dynamic parameter form (from the script's argparse spec)
        params_box = QGroupBox("Parameters")
        pb = QVBoxLayout(params_box)
        pb.setContentsMargins(8, 8, 8, 8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(160)
        self._params_host = QWidget()
        self._params_form = QFormLayout(self._params_host)
        self._params_form.setSpacing(6)
        scroll.setWidget(self._params_host)
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
        self._scripts_dir = QLineEdit(DEFAULT_SCRIPTS_DIR)
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

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._set_status("loading scripts…")
        self.hub.run_async(
            f"taskdlg_scripts:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).list_scripts(),
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

        if op == "taskdlg_save":
            self._saving = False
            self._buttons.setEnabled(True)
            if isinstance(result, Exception):
                self._set_status(f"save failed: {result}", error=True)
            else:
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
        # Clear existing rows
        while self._params_form.count():
            item = self._params_form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._param_widgets.clear()

        specs = self._param_specs.get(script, [])
        if not specs:
            note = QLabel("This script declares no argparse parameters.")
            note.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
            self._params_form.addRow(note)
        for spec in specs:
            widget = self._widget_for(spec)
            self._param_widgets[spec["dest"]] = (widget, spec)
            label = self._label_for(spec)
            self._params_form.addRow(label, widget)

        self._set_status("")
        # If we're editing, prefill values from the pending command now that the
        # form exists.
        if self._pending_prefill is not None:
            self._apply_prefill(self._pending_prefill)
            self._pending_prefill = None
        self._update_preview()

    def _label_for(self, spec: dict) -> QLabel:
        flag = spec["flags"][0] if spec["flags"] else spec["dest"]
        text: str = flag + (" *" if spec.get("required") else "")

        if text.startswith("-"): text = text[1:]
        elif text.startswith("--"): text = text[2:]
        text = text.replace("-", " ")

        lbl = QLabel(text)
        if spec.get("help"):
            lbl.setToolTip(spec["help"])
        return lbl

    def _widget_for(self, spec: dict) -> QWidget:
        default = spec.get("default")
        if spec.get("is_flag"):
            w = QCheckBox()
            w.setChecked(bool(default))
            w.stateChanged.connect(self._update_preview)
        elif spec.get("choices"):
            w = QComboBox()
            w.addItems([str(c) for c in spec["choices"]])
            if default is not None and str(default) in [str(c) for c in spec["choices"]]:
                w.setCurrentText(str(default))
            w.currentTextChanged.connect(self._update_preview)
        else:
            w = QLineEdit()
            if default is not None:
                w.setText(str(default))
            hint = spec.get("type") or "text"
            w.setPlaceholderText(f"{hint}{' (required)' if spec.get('required') else ''}")
            w.textChanged.connect(self._update_preview)
        if spec.get("help"):
            w.setToolTip(spec["help"])
        return w

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

    def _apply_prefill(self, args: List[str]) -> None:
        flag_to_dest = {}
        for dest, (w, spec) in self._param_widgets.items():
            for f in spec["flags"]:
                flag_to_dest[f] = dest
        extra: List[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            dest = flag_to_dest.get(a)
            if dest is None:
                extra.append(a)
                i += 1
                continue
            w, spec = self._param_widgets[dest]
            if spec.get("is_flag"):
                if isinstance(w, QCheckBox):
                    w.setChecked(True)
                i += 1
            elif i + 1 < len(args):
                val = args[i + 1]
                if isinstance(w, QComboBox):
                    w.setCurrentText(val)
                elif isinstance(w, QLineEdit):
                    w.setText(val)
                i += 2
            else:
                i += 1
        if extra:
            self._extra.setText(" ".join(shlex.quote(e) for e in extra))

    # ── Command building / preview / save ────────────────────────────────────

    def _build_command(self) -> List[str]:
        interp = self._interp.text().strip() or "python3"
        script = self._script.currentText().strip()
        sdir = self._scripts_dir.text().strip().rstrip("/")
        if not script:
            return []
        script_path = f"{sdir}/{script}" if sdir else script
        cmd = [interp, script_path]
        for dest, (w, spec) in self._param_widgets.items():
            flag = spec["flags"][0] if spec["flags"] else None
            if spec.get("is_flag"):
                if isinstance(w, QCheckBox) and w.isChecked() and flag:
                    cmd.append(flag)
            else:
                val = (w.currentText() if isinstance(w, QComboBox) else w.text()).strip()
                if val == "":
                    continue
                if flag:
                    cmd += [flag, val]
                else:
                    cmd.append(val)
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
        # ── strict validation ──
        name = self._name.text().strip()
        if not name:
            self._set_status("task name is required", error=True)
            return
        if not self._script.currentText().strip():
            self._set_status("select a script", error=True)
            return
        missing = []
        bad_type = []
        for dest, (w, spec) in self._param_widgets.items():
            if spec.get("is_flag") or isinstance(w, QComboBox):
                continue
            val = w.text().strip()
            flag = spec["flags"][0] if spec["flags"] else dest
            if spec.get("required") and val == "":
                missing.append(flag)
            elif val != "" and spec.get("type") in ("int", "float"):
                try:
                    (int if spec["type"] == "int" else float)(val)
                except ValueError:
                    bad_type.append(f"{flag} ({spec['type']})")
        if missing:
            self._set_status("required parameter(s) missing: " + ", ".join(missing), error=True)
            return
        if bad_type:
            self._set_status("invalid value for: " + ", ".join(bad_type), error=True)
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
