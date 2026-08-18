"""
RampEditorDialog — author a parameter ramp step.

A ramp sweeps one numeric parameter from a start value to a stop value over time,
defined by any two of {step, hold, duration} (or, when it fills the on-air window,
just one of {step, hold} — the duration comes from the plan/schedule). A
window-filling ramp can still be inset from each edge. Stored parametrically and
expanded on the unit at arm time (see api.ramp / agent.ramp).

Two targets:
  - Tune (default): sweep a LIVE parameter of a duration task already running in
    the sequence — expands to `tune` fires (set_params). Only the sequence's
    duration tasks and their live numeric params are selectable.
  - Run the task each step: sweep any numeric parameter by re-invoking the task
    once per point (e.g. an attenuator-set script) — expands to `run` fires, so no
    running task is needed. Any unit task is selectable, and the OTHER params get
    fixed values via a parameter form.

It returns a RunItem with action="ramp" via .result_item, like StepEditorDialog.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import ramp as _ramp

from . import timeline_model as tlm
from .duration_spin import DurationSpinBox
from .param_form import ParamForm, fmt_duration, fmt_value
from .theme import Palette


def _is_numeric(spec: dict) -> bool:
    """True for a float/int parameter — the only kinds a ramp can sweep."""
    return spec.get("kind") in ("number", "integer") or spec.get("type") in ("int", "float")


def _is_integer(spec: dict) -> bool:
    return spec.get("kind") == "integer" or spec.get("type") == "int"

_MODES_SINGLE = [
    ("steps_hold",     "Number of steps + hold time  → duration"),
    ("steps_duration", "Number of steps + duration  → hold time"),
    ("step_hold",      "Step size + hold time  → duration"),
    ("step_duration",  "Step size + duration  → hold time"),
    ("duration_hold",  "Duration + hold time  → number of steps"),
]
_MODES_BOTH = [
    ("steps_window", "Number of steps  (duration from schedule)"),
    ("step_window",  "Step size  (duration from schedule)"),
    ("hold_window",  "Hold time  (duration from schedule)"),
]
_FIELDS = {
    "steps_hold":     ("steps", "hold"),
    "steps_duration": ("steps", "duration"),
    "step_hold":      ("step", "hold"),
    "step_duration":  ("step", "duration"),
    "duration_hold":  ("duration", "hold"),
    "steps_window":   ("steps",),
    "step_window":    ("step",),
    "hold_window":    ("hold",),
}


def _mode_for_ramp(r: dict, both: bool) -> str:
    """Pick the 'Define by' mode that matches a saved ramp, so editing it restores
    the way it was authored (step-size vs step-count, hold vs duration) instead of
    defaulting to the first mode and dropping its values."""
    has_steps = r.get("steps") is not None
    has_step = r.get("step") is not None
    has_hold = r.get("hold_s") is not None
    has_dur = r.get("duration_s") is not None
    if both:
        return "steps_window" if has_steps else "step_window" if has_step else "hold_window"
    if has_steps and has_dur:
        return "steps_duration"
    if has_step and has_hold:
        return "step_hold"
    if has_step and has_dur:
        return "step_duration"
    if has_dur and has_hold:
        return "duration_hold"
    if has_step:
        return "step_hold"
    return "steps_hold"   # steps + hold, and the default for a fresh ramp


def _num(text: str) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _sequence_tasks(editor) -> list:
    """Tasks a tune/ramp step may target — those started as duration bars in the
    current sequence. Falls back to all tasks for an editor that can't report them."""
    getter = getattr(editor, "sequence_task_names", None)
    return getter() if getter is not None else editor.available_tasks()


def _clean(x: float) -> float:
    """Strip binary floating-point noise (2.3000000000000007 → 2.3) before display,
    keeping up to 12 significant figures — ample for any real parameter value."""
    try:
        return float(f"{float(x):.12g}")
    except (TypeError, ValueError):
        return x


class RampEditorDialog(QDialog):
    REMOVE = 2

    def __init__(self, item, editor, new: bool, parent=None):
        super().__init__(parent)
        self._src = item
        self._editor = editor
        self._new = new
        self.result_item: Optional[object] = None
        self._current_script = ""
        self._all_params: List[dict] = []    # every param of the current script
        self._num_params: List[dict] = []    # numeric params (run mode ramps any)
        self._live_params: List[dict] = []   # live numeric params (tune mode)
        # Run mode fires the task per point; forced on when the sequence has no
        # duration task to tune. Seeded from the saved ramp.
        self._has_dur = bool(_sequence_tasks(self._editor))
        self._run_mode = ((getattr(self._src, "ramp", None) or {}).get("mode") == "run"
                          or not self._has_dur)
        self._ready = False   # suppress preview callbacks until every widget exists

        self.setWindowTitle("New ramp" if new else "Edit ramp")
        self.setMinimumWidth(460)
        self._build()

        if editor._hub is not None:
            editor._hub.task_done.connect(self._on_params)
        self.finished.connect(lambda _=0: self._disconnect())
        self._select_task(self._task.currentText(), initial=True)

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)
        form = QFormLayout()
        form.setSpacing(8)
        r = dict(getattr(self._src, "ramp", None) or {})

        # --- create every widget first; connect signals only afterwards, so an
        #     early setText/setCurrentText during build can't fire a preview
        #     callback before the widgets it reads exist. ---
        # Tune mode acts on a running task (only the sequence's duration tasks are
        # selectable); run mode fires the task each point (any task). Forced on when
        # there's no duration task to tune.
        self._run_chk = QCheckBox("Run a task at each ramp step (fires it per point — any task)")
        self._run_chk.setChecked(self._run_mode)
        self._run_chk.setEnabled(self._has_dur)
        if not self._has_dur:
            self._run_chk.setToolTip("No duration task in this sequence to tune, so a ramp "
                                     "must run a task each step.")
        form.addRow("", self._run_chk)

        self._task = QComboBox()
        self._populate_tasks()
        form.addRow("Task", self._task)

        self._param = QComboBox()
        form.addRow("Parameter", self._param)

        self._start = QLineEdit(_fmt(r.get("start")));  self._start.setPlaceholderText("start value")
        self._stop = QLineEdit(_fmt(r.get("stop")));    self._stop.setPlaceholderText("stop value")
        form.addRow("From", self._start)
        form.addRow("To", self._stop)

        self._anchor = QComboBox()
        self._anchor.addItem("On-air (T0)", "start")
        self._anchor.addItem("Off-air", "stop")
        self._anchor.addItem("Fill on-air window", "both")
        self._anchor.setCurrentIndex(
            {"start": 0, "stop": 1, "both": 2}.get(getattr(self._src, "anchor", "start"), 0))

        self._offset = _spin(float(getattr(self._src, "offset", 0.0)))
        self._offset_end = _spin(float(getattr(self._src, "offset_end", 0.0)))
        form.addRow("Anchor", self._anchor)
        self._off_lbl = _row(form, "Offset from anchor", self._offset)
        self._offend_lbl = _row(form, "End offset from off-air", self._offset_end)

        self._mode = QComboBox()
        form.addRow("Define by", self._mode)

        self._steps = QLineEdit();    self._steps.setPlaceholderText("count (equal increments)")
        self._step = QLineEdit();     self._step.setPlaceholderText("increment per step")
        self._hold = QLineEdit();     self._hold.setPlaceholderText("seconds per step")
        self._duration = QLineEdit(); self._duration.setPlaceholderText("seconds")
        self._row_steps = _row(form, "Number of steps", self._steps)
        self._row_step = _row(form, "Step size", self._step)
        self._row_hold = _row(form, "Hold time", self._hold)
        self._row_duration = _row(form, "Duration", self._duration)
        if r.get("steps") is not None:
            self._steps.setText(str(int(r.get("steps"))))
        if r.get("step") is not None:
            self._step.setText(_fmt(r.get("step")))
        if r.get("hold_s") is not None:
            self._hold.setText(_fmt(r.get("hold_s")))
        if r.get("duration_s") is not None:
            self._duration.setText(_fmt(r.get("duration_s")))

        outer.addLayout(form)

        # Run mode: fixed values for the task's OTHER parameters (the ramped one is
        # driven by From/To). Hidden in tune mode.
        self._form_lbl = QLabel("Other parameters (fixed each step):")
        self._form_lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._form_lbl)
        self._form = ParamForm()
        self._form_scroll = QScrollArea()
        self._form_scroll.setWidgetResizable(True)
        self._form_scroll.setWidget(self._form)
        self._form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._form_scroll.setMinimumHeight(80)
        self._form_scroll.setMaximumHeight(170)
        self._form_scroll.setStyleSheet(
            f"QScrollArea {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; }}")
        outer.addWidget(self._form_scroll)

        self._warn = QLabel("")
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet(f"font-size: 11px; color: {Palette.ARMED};")
        self._warn.setVisible(False)
        outer.addWidget(self._warn)

        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._preview)

        # Collapsible per-step listing.
        self._steps_btn = QPushButton("▸ Show steps")
        self._steps_btn.setFlat(True); self._steps_btn.setCheckable(True)
        self._steps_btn.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {Palette.ACCENT}; border: none; }}")
        self._steps_btn.toggled.connect(self._toggle_steps)
        outer.addWidget(self._steps_btn)
        self._steps_view = QPlainTextEdit()
        self._steps_view.setReadOnly(True)
        self._steps_view.setFont(QFont("monospace"))
        self._steps_view.setFixedHeight(140)
        self._steps_view.setVisible(False)
        outer.addWidget(self._steps_view)

        buttons = QDialogButtonBox()
        if not self._new:
            rm = QPushButton("Remove"); rm.setStyleSheet(f"color: {Palette.CRASH};")
            buttons.addButton(rm, QDialogButtonBox.ButtonRole.DestructiveRole)
            rm.clicked.connect(lambda: self.done(self.REMOVE))
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Now everything exists — wire the change signals.
        self._run_chk.toggled.connect(self._sync_target_mode)
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        self._param.currentTextChanged.connect(lambda _t: self._on_param_changed())
        for w in (self._start, self._stop, self._steps, self._step, self._hold, self._duration):
            w.textChanged.connect(self._update_preview)
        self._offset.valueChanged.connect(self._update_preview)
        self._offset_end.valueChanged.connect(self._update_preview)
        self._anchor.currentIndexChanged.connect(self._sync_anchor)
        self._mode.currentIndexChanged.connect(self._sync_mode)

        # Restore the mode this ramp was authored in (else _sync_anchor defaults to
        # the first mode, hiding the fields the saved ramp actually uses).
        self._init_mode = _mode_for_ramp(r, self._is_both())
        self._ready = True
        self._apply_mode_visibility()
        self._sync_anchor()   # populate modes + show/hide rows + preview

    # ── Target (tune vs run) wiring ──────────────────────────────────────────

    def _tasks_for_mode(self) -> list:
        return self._editor.available_tasks() if self._run_mode else _sequence_tasks(self._editor)

    def _populate_tasks(self) -> None:
        want = self._task.currentText().strip() or (self._src.task_name or "")
        self._task.blockSignals(True)
        self._task.clear()
        self._task.addItems(self._tasks_for_mode())
        if want and self._task.findText(want) >= 0:
            self._task.setCurrentText(want)
        elif self._task.count():
            self._task.setCurrentIndex(0)
        self._task.blockSignals(False)

    def _apply_mode_visibility(self) -> None:
        self._form_lbl.setVisible(self._run_mode)
        self._form_scroll.setVisible(self._run_mode)

    def _sync_target_mode(self) -> None:
        self._run_mode = self._run_chk.isChecked()
        self._apply_mode_visibility()
        self._populate_tasks()
        self._select_task(self._task.currentText())   # rebuild params + fixed-form for the mode

    def _active_params(self) -> List[dict]:
        return self._num_params if self._run_mode else self._live_params

    @staticmethod
    def _pname(s: dict) -> str:
        return s.get("name") or s.get("dest")

    def _ramped_spec(self) -> Optional[dict]:
        name = self._param.currentText().strip()
        for s in self._active_params():
            if self._pname(s) == name:
                return s
        return None

    def _rebuild_run_form(self) -> None:
        if not self._run_mode:
            self._form.set_params([])
            return
        ramped = self._param.currentText().strip()
        self._form.set_params([s for s in self._all_params if self._pname(s) != ramped])
        args = list(getattr(self._src, "args", []) or [])
        if args:
            self._form.set_values(args)

    def _on_param_changed(self) -> None:
        self._rebuild_run_form()   # the ramped param leaves the fixed-value form
        self._update_preview()

    def _update_warning(self) -> None:
        task = self._task.currentText().strip()
        if self._run_mode and self._has_dur and task in _sequence_tasks(self._editor):
            self._warn.setText(
                "⚠ This task also runs as a duration step here; re-running it each point "
                "may collide with that. Consider tuning it instead (uncheck the box).")
            self._warn.setVisible(True)
        else:
            self._warn.setVisible(False)

    # ── Anchor / mode wiring ─────────────────────────────────────────────────

    def _is_both(self) -> bool:
        return self._anchor.currentData() == "both"

    def _sync_anchor(self) -> None:
        both = self._is_both()
        # A window-filling ramp is inset from BOTH edges; a single-anchor ramp has
        # one offset from its anchor.
        self._off_lbl.setText("Start offset from on-air" if both else "Offset from anchor")
        self._offend_lbl.setVisible(both)
        self._offset_end.setVisible(both)
        # First populate uses the saved ramp's authored mode; later anchor switches
        # keep whatever the user had selected.
        want = self._mode.currentData() or getattr(self, "_init_mode", None)
        self._mode.blockSignals(True)
        self._mode.clear()
        for key, label in (_MODES_BOTH if both else _MODES_SINGLE):
            self._mode.addItem(label, key)
        idx = self._mode.findData(want)
        self._mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._mode.blockSignals(False)
        self._sync_mode()

    def _sync_mode(self) -> None:
        fields = _FIELDS.get(self._mode.currentData(), ())
        for name, widget, label in (("steps", self._steps, self._row_steps),
                                     ("step", self._step, self._row_step),
                                     ("hold", self._hold, self._row_hold),
                                     ("duration", self._duration, self._row_duration)):
            on = name in fields
            widget.setVisible(on)
            label.setVisible(on)
        self._update_preview()

    # ── Task → live params ───────────────────────────────────────────────────

    def _select_task(self, task: str, initial: bool = False) -> None:
        task = (task or "").strip()
        script, _ = self._editor.script_for_task(task)
        self._current_script = script
        cache = self._editor.param_cache()
        if not script:
            self._set_params([])
            return
        if script in cache:
            self._set_params(cache[script])
            return
        if self._editor._hub is not None and script not in self._editor._params_inflight:
            self._editor._params_inflight.add(script)
            self._editor._hub.run_async(
                f"rampdlg_params:{self._editor._hostname}:{script}",
                lambda s=script: self._editor._hub.fleet.get(self._editor._hostname).get_script_params(s))

    def _on_params(self, label: str, result) -> None:
        if not label.startswith("rampdlg_params:"):
            return
        parts = label.split(":", 2)
        if len(parts) < 3 or parts[1] != self._editor._hostname:
            return
        script = parts[2]
        self._editor._params_inflight.discard(script)
        if isinstance(result, Exception):
            return
        self._editor.param_cache()[script] = (result or {}).get("params", [])
        if script == self._current_script:
            self._set_params(self._editor.param_cache()[script])

    def _set_params(self, specs: List[dict]) -> None:
        self._all_params = list(specs or [])
        self._num_params = [s for s in self._all_params if _is_numeric(s)]   # run mode
        self._live_params = [s for s in self._num_params if s.get("live")]   # tune mode
        want = (getattr(self._src, "ramp", None) or {}).get("param")
        self._param.blockSignals(True)
        self._param.clear()
        self._param.addItems([self._pname(s) for s in self._active_params()])
        if want and self._param.findText(want) >= 0:
            self._param.setCurrentText(want)
        self._param.blockSignals(False)
        self._rebuild_run_form()
        self._update_preview()

    # ── Preview ──────────────────────────────────────────────────────────────

    def _spec_from_form(self) -> dict:
        fields = _FIELDS.get(self._mode.currentData(), ())
        spec = {"param": self._param.currentText().strip(),
                "start": _num(self._start.text()), "stop": _num(self._stop.text())}
        if "steps" in fields:
            n = _num(self._steps.text())
            spec["steps"] = int(n) if n is not None else None
        if "step" in fields:
            spec["step"] = _num(self._step.text())
        if "hold" in fields:
            spec["hold_s"] = _num(self._hold.text())
        if "duration" in fields:
            spec["duration_s"] = _num(self._duration.text())
        if self._run_mode:
            rs = self._ramped_spec() or {}
            flags = rs.get("flags") or []
            spec["mode"] = "run"
            spec["flag"] = flags[0] if flags else None
            spec["integer"] = _is_integer(rs)
        return spec

    def _param_unit(self) -> str:
        s = self._ramped_spec()
        return (s.get("unit") or "") if s else ""

    def _update_preview(self, *_) -> None:
        if not self._ready:
            return
        spec = self._spec_from_form()
        self._update_warning()
        if not self._active_params():
            self._set_preview(
                "This task has no numeric parameters to ramp." if self._run_mode
                else "This task's script has no live numeric parameters.", error=True)
            return
        if spec["start"] is None or spec["stop"] is None:
            self._set_preview("Enter numeric From/To values.")
            return

        unit = self._param_unit()
        u = f" {unit}" if unit else ""
        lines = [f"Ramp {spec['param'] or '(param)'}:  "
                 f"{fmt_value(spec['start'])}{u} → {fmt_value(spec['stop'])}{u}"]

        if self._is_both():
            lines.append(f"Anchor: fill on-air window  (start {_off(self._offset.value())}, "
                         f"end {_off(self._offset_end.value())} from off-air)")
            fields = _FIELDS.get(self._mode.currentData(), ())
            if "steps" in fields:
                given, val = "steps", self._steps.text().strip()
            elif "step" in fields:
                given, val = "step size", self._step.text().strip()
            else:
                given, val = "hold", self._hold.text().strip()
            lines.append(f"{given} = {val or '—'} · duration set by the schedule")
            self._set_preview("\n".join(lines))
            self._refresh_steps_view(spec, both=True)
            return

        anchor = "on-air" if self._anchor.currentData() == "start" else "off-air"
        lines.append(f"Anchor: {anchor}, offset {_off(self._offset.value())}")
        try:
            res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                     step=spec.get("step"), hold_s=spec.get("hold_s"),
                                     duration_s=spec.get("duration_s"))
        except (ValueError, TypeError) as exc:
            lines.append("⚠ " + str(exc))
            self._set_preview("\n".join(lines), error=True)
            self._steps_view.setPlainText("")
            return
        step = abs(res.values[1] - res.values[0]) if len(res.values) > 1 else 0
        lines.append(f"{len(res.values)} points · {res.n_intervals} steps · "
                     f"step size {fmt_value(_clean(step))}{u} · hold {fmt_duration(res.hold_s)} · "
                     f"duration {fmt_duration(res.duration_s)}")
        self._set_preview("\n".join(lines))
        self._refresh_steps_view(spec, both=False)

    def _set_preview(self, text: str, error: bool = False) -> None:
        self._preview.setStyleSheet(
            f"font-size: 11px; color: {Palette.CRASH if error else Palette.TEXT_FAINT};")
        self._preview.setText(text)

    # ── Per-step listing ─────────────────────────────────────────────────────

    def _toggle_steps(self, on: bool) -> None:
        self._steps_btn.setText("▾ Hide steps" if on else "▸ Show steps")
        self._steps_view.setVisible(on)
        if on:
            self._update_preview()   # populate now that it's visible

    def _refresh_steps_view(self, spec: dict, both: bool) -> None:
        """List each concrete tune point (index, time offset, value) into the
        collapsible view. For a window-filling ('both') ramp only the value
        sequence is known here — the times come from the schedule at arm time."""
        if not self._steps_view.isVisible():
            return
        if spec.get("start") is None or spec.get("stop") is None:
            self._steps_view.setPlainText("")
            return
        unit = self._param_unit()
        u = f" {unit}" if unit else ""
        try:
            if both:
                # steps or step fix the value sequence regardless of the window; a
                # hold-only ramp's point count depends on the (scheduled) window.
                if spec.get("steps") is None and spec.get("step") is None:
                    self._steps_view.setPlainText(
                        "Hold-time ramp: the number of points depends on the on-air\n"
                        "window, resolved when the sequence is scheduled.")
                    return
                res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                         step=spec.get("step"), window_s=1.0)
                lines = ["   #   value      (times set by the schedule window)"]
                for i, v in enumerate(res.values):
                    lines.append(f"  {i:>3}   {fmt_value(_clean(v))}{u}")
                self._steps_view.setPlainText("\n".join(lines))
                return
            res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                     step=spec.get("step"), hold_s=spec.get("hold_s"),
                                     duration_s=spec.get("duration_s"))
        except (ValueError, TypeError) as exc:
            self._steps_view.setPlainText(str(exc))
            return
        anchor = self._anchor.currentData()
        fires = _ramp.place_ramp(anchor, float(self._offset.value()), res)
        lines = [f"{'#':>3}  {'time':>9}   value"]
        for i, (fa, foff, v) in enumerate(fires):
            tag = "T0" if fa == "start" else "off"
            lines.append(f"{i:>3}  {tag + _off(foff):>9}   {fmt_value(_clean(v))}{u}")
        self._steps_view.setPlainText("\n".join(lines))

    # ── Accept ───────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        task = self._task.currentText().strip()
        param = self._param.currentText().strip()
        if not task:
            return self._set_preview("pick a task", error=True)
        if not param:
            return self._set_preview(
                "this task has no numeric parameter to ramp" if self._run_mode
                else "this task has no live parameter to ramp", error=True)
        spec = self._spec_from_form()
        if spec["start"] is None or spec["stop"] is None:
            return self._set_preview("enter numeric From/To values", error=True)
        anchor = self._anchor.currentData()
        err = tlm._ramp_spec_error(spec, anchor)
        if err:
            return self._set_preview(err, error=True)
        offset = round(self._offset.value(), 1)
        offset_end = round(self._offset_end.value(), 1) if anchor == "both" else 0.0

        args: List[str] = []
        if self._run_mode:
            # Fixed values for the other params (the ramped one is injected per point
            # on the unit). No span check — each point is a standalone one-shot.
            ferr = self._form.validate()
            if ferr:
                return self._set_preview(ferr, error=True)
            args = self._form.build_args()
        else:
            spans_getter = getattr(self._editor, "task_spans", None)
            if spans_getter is not None:
                span_err = tlm.step_within_task_error(spans_getter(task), anchor, offset,
                                                      offset_end, kind="ramp")
                if span_err:
                    return self._set_preview(span_err, error=True)

        ramp = {k: v for k, v in spec.items() if v is not None}
        self.result_item = tlm.RunItem(
            task_name=task, action="ramp", ramp=ramp, anchor=anchor,
            offset=offset, offset_end=offset_end,
            args=args, replace_args=True, uid=self._src.uid)
        self.accept()

    def _disconnect(self) -> None:
        if self._editor._hub is None:
            return
        try:
            self._editor._hub.task_done.disconnect(self._on_params)
        except (TypeError, RuntimeError):
            pass


def _fmt(v) -> str:
    return "" if v is None else fmt_value(v) if isinstance(v, (int, float)) else str(v)


def _off(v: float) -> str:
    return fmt_duration(v, signed=True)


def _spin(value: float) -> DurationSpinBox:
    w = DurationSpinBox()
    w.setValue(value)
    return w


def _row(form: QFormLayout, label: str, widget: QWidget) -> QLabel:
    lbl = QLabel(label)
    form.addRow(lbl, widget)
    widget._row_label = lbl
    return lbl
