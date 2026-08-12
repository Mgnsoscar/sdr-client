"""
RampEditorDialog — author a parameter ramp step.

A ramp sweeps one live parameter of a running duration task from a start value to
a stop value over time, defined by any two of {step, hold, duration} (or, when it
fills the on-air window, just one of {step, hold} — the duration comes from the
plan/schedule). A window-filling ramp can still be inset from each edge (start it
after on-air, end it before off-air). It's stored parametrically and expanded into
tune fires on the unit at arm time (see api.ramp / agent.ramp).

The dialog reuses the timeline editor's task list and script-parameter cache,
filtered to the task's LIVE, numeric parameters — the only ones a ramp can sweep.
It returns a RunItem with action="ramp" via .result_item, like StepEditorDialog.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from api import ramp as _ramp

from . import timeline_model as tlm
from .param_form import fmt_value
from .theme import Palette

_MODES_SINGLE = [
    ("step_hold",     "Step size + hold time  → duration"),
    ("step_duration", "Step size + duration  → hold time"),
    ("duration_hold", "Duration + hold time  → step size"),
]
_MODES_BOTH = [
    ("step_window", "Step size  (duration from schedule)"),
    ("hold_window", "Hold time  (duration from schedule)"),
]
_FIELDS = {
    "step_hold":     ("step", "hold"),
    "step_duration": ("step", "duration"),
    "duration_hold": ("duration", "hold"),
    "step_window":   ("step",),
    "hold_window":   ("hold",),
}


def _num(text: str) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


class RampEditorDialog(QDialog):
    REMOVE = 2

    def __init__(self, item, editor, new: bool, parent=None):
        super().__init__(parent)
        self._src = item
        self._editor = editor
        self._new = new
        self.result_item: Optional[object] = None
        self._current_script = ""
        self._live_params: List[dict] = []
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
        self._task = QComboBox()
        tasks = self._editor.available_tasks()
        if tasks:
            self._task.addItems(tasks)
        if self._src.task_name and self._task.findText(self._src.task_name) < 0:
            self._task.addItem(self._src.task_name)
        if self._src.task_name:
            self._task.setCurrentText(self._src.task_name)
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

        self._step = QLineEdit();     self._step.setPlaceholderText("value increment")
        self._hold = QLineEdit();     self._hold.setPlaceholderText("seconds per step")
        self._duration = QLineEdit(); self._duration.setPlaceholderText("seconds")
        self._row_step = _row(form, "Step size", self._step)
        self._row_hold = _row(form, "Hold time", self._hold)
        self._row_duration = _row(form, "Duration", self._duration)
        if r.get("step") is not None:
            self._step.setText(_fmt(r.get("step")))
        if r.get("hold_s") is not None:
            self._hold.setText(_fmt(r.get("hold_s")))
        if r.get("duration_s") is not None:
            self._duration.setText(_fmt(r.get("duration_s")))

        outer.addLayout(form)
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._preview)

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
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        self._param.currentTextChanged.connect(lambda _t: self._update_preview())
        for w in (self._start, self._stop, self._step, self._hold, self._duration):
            w.textChanged.connect(self._update_preview)
        self._offset.valueChanged.connect(self._update_preview)
        self._offset_end.valueChanged.connect(self._update_preview)
        self._anchor.currentIndexChanged.connect(self._sync_anchor)
        self._mode.currentIndexChanged.connect(self._sync_mode)

        self._ready = True
        self._sync_anchor()   # populate modes + show/hide rows + preview

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
        prev = self._mode.currentData()
        self._mode.blockSignals(True)
        self._mode.clear()
        for key, label in (_MODES_BOTH if both else _MODES_SINGLE):
            self._mode.addItem(label, key)
        idx = self._mode.findData(prev)
        self._mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._mode.blockSignals(False)
        self._sync_mode()

    def _sync_mode(self) -> None:
        fields = _FIELDS.get(self._mode.currentData(), ())
        for name, widget, label in (("step", self._step, self._row_step),
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
        self._live_params = [s for s in specs if s.get("live")
                             and s.get("kind") in ("number", "integer")]
        want = (getattr(self._src, "ramp", None) or {}).get("param")
        self._param.blockSignals(True)
        self._param.clear()
        self._param.addItems([s.get("name") or s.get("dest") for s in self._live_params])
        if want and self._param.findText(want) >= 0:
            self._param.setCurrentText(want)
        self._param.blockSignals(False)
        self._update_preview()

    # ── Preview ──────────────────────────────────────────────────────────────

    def _spec_from_form(self) -> dict:
        fields = _FIELDS.get(self._mode.currentData(), ())
        spec = {"param": self._param.currentText().strip(),
                "start": _num(self._start.text()), "stop": _num(self._stop.text())}
        if "step" in fields:
            spec["step"] = _num(self._step.text())
        if "hold" in fields:
            spec["hold_s"] = _num(self._hold.text())
        if "duration" in fields:
            spec["duration_s"] = _num(self._duration.text())
        return spec

    def _update_preview(self, *_) -> None:
        if not self._ready:
            return
        spec = self._spec_from_form()
        if not self._live_params:
            self._set_preview("This task's script has no live numeric parameters.", error=True)
            return
        if spec["start"] is None or spec["stop"] is None:
            self._set_preview("Enter numeric From/To values.")
            return
        if self._is_both():
            self._set_preview("Sweeps across the on-air window between the insets "
                              "(duration set when scheduled in a plan).")
            return
        try:
            res = _ramp.resolve_ramp(spec["start"], spec["stop"], step=spec.get("step"),
                                     hold_s=spec.get("hold_s"), duration_s=spec.get("duration_s"))
        except (ValueError, TypeError) as exc:
            self._set_preview(str(exc), error=True)
            return
        self._set_preview(
            f"→ {len(res.values)} points, {fmt_value(res.hold_s)}s hold, "
            f"{fmt_value(res.duration_s)}s duration")

    def _set_preview(self, text: str, error: bool = False) -> None:
        self._preview.setStyleSheet(
            f"font-size: 11px; color: {Palette.CRASH if error else Palette.TEXT_FAINT};")
        self._preview.setText(text)

    # ── Accept ───────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        task = self._task.currentText().strip()
        param = self._param.currentText().strip()
        if not task:
            return self._set_preview("pick a task", error=True)
        if not param:
            return self._set_preview("this task has no live parameter to ramp", error=True)
        spec = self._spec_from_form()
        if spec["start"] is None or spec["stop"] is None:
            return self._set_preview("enter numeric From/To values", error=True)
        anchor = self._anchor.currentData()
        err = tlm._ramp_spec_error(spec, anchor)
        if err:
            return self._set_preview(err, error=True)
        ramp = {k: v for k, v in spec.items() if v is not None}
        self.result_item = tlm.RunItem(
            task_name=task, action="ramp", ramp=ramp, anchor=anchor,
            offset=round(self._offset.value(), 1),
            offset_end=round(self._offset_end.value(), 1) if anchor == "both" else 0.0,
            uid=self._src.uid)
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


def _spin(value: float) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(-100000.0, 100000.0)
    w.setDecimals(1); w.setSingleStep(1.0); w.setSuffix(" s")
    w.setValue(value)
    return w


def _row(form: QFormLayout, label: str, widget: QWidget) -> QLabel:
    lbl = QLabel(label)
    form.addRow(lbl, widget)
    widget._row_label = lbl
    return lbl
