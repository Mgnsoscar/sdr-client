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
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import ramp as _ramp
from state.power_fold import (PowerFold, fold_params_from_values, refold_bounds,
                              resolve_keyed_values)
from state.power_law import parse_law

from . import timeline_model as tlm
from .duration_spin import DurationSpinBox
from .param_form import (
    BoundedNumberField, ParamForm, apply_power_bounds, find_power_index,
    fmt_duration, fmt_value, hz_per_unit, range_hint,
)
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


def _swap_only(layout, widget) -> None:
    """Make `widget` the sole child of a (single-slot) layout, disposing of the previous one."""
    while layout.count():
        old = layout.takeAt(0).widget()
        if old is not None:
            old.setParent(None)
            old.deleteLater()
    layout.addWidget(widget)


def _sequence_tasks(editor) -> list:
    """Tasks a tune/ramp step may target — those started as duration bars in the
    current sequence. Falls back to all tasks for an editor that can't report them."""
    getter = getattr(editor, "sequence_task_names", None)
    return getter() if getter is not None else editor.available_tasks()


def _ramp_range_error(spec: Optional[dict], start, stop) -> Optional[str]:
    """If From/To fall outside the ramped parameter's declared min/max, describe it
    — else None. A ramp is monotonic between its endpoints, so every intermediate
    level lies within [From, To]; checking the two endpoints covers the whole sweep.
    Mirrors ParamForm.validate()'s out-of-range wording so the two feel the same."""
    if not spec:
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is None and hi is None:
        return None
    unit = spec.get("unit") or ""
    u = f" {unit}" if unit else ""
    bad = []
    for label, val in (("From", start), ("To", stop)):
        if val is None:
            continue
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            bad.append(f"{label} {fmt_value(val)}{u}")
    if not bad:
        return None
    name = spec.get("name") or spec.get("dest") or "parameter"
    joined = " and ".join(bad)
    return f"{joined} outside allowed range {range_hint(spec)}{u} for {name}"


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
        from .dialog_style import editor_qss
        from .param_widgets import Dropdown
        self.setStyleSheet(editor_qss())
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

        self._task = Dropdown()
        self._populate_tasks()
        form.addRow("Task", self._task)

        self._param = Dropdown()
        form.addRow("Parameter", self._param)

        # From/To render as bounded numeric fields (spinbox + range rail + limit chip) —
        # the same widget the parameter form uses — rebuilt for the swept parameter so its
        # min/max, unit and (for --power on a calibrated unit) the frequency-folded range
        # with real achievable-level snapping are all in view. Seeded from the saved ramp.
        self._init_start = r.get("start")
        self._init_stop = r.get("stop")
        self._seeded_view = False   # have the From/To been seeded WITH params (so the view offset
                                    # is known)? until then re-seed from the saved base, converted
        self._start_field = None
        self._stop_field = None
        self._start_box = QWidget(); self._start_lay = QVBoxLayout(self._start_box)
        self._start_lay.setContentsMargins(0, 0, 0, 0); self._start_lay.setSpacing(0)
        self._stop_box = QWidget(); self._stop_lay = QVBoxLayout(self._stop_box)
        self._stop_lay.setContentsMargins(0, 0, 0, 0); self._stop_lay.setSpacing(0)
        form.addRow("From", self._start_box)
        form.addRow("To", self._stop_box)

        self._anchor = Dropdown()
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

        self._mode = Dropdown()
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

        # Which end levels to emit. Every emitted level is held for the dwell time
        # (the ramp's duration counts all of them), so dropping an end trims one
        # whole (level + hold) — the knob for chaining ramps without a doubled seam.
        # Single-anchor only; a window-filling ramp always spans both edges.
        self._inc_first = QCheckBox("Include first step")
        self._inc_last = QCheckBox("Include last step")
        self._inc_first.setChecked(bool(r.get("include_first", True)))
        self._inc_last.setChecked(bool(r.get("include_last", True)))
        self._inc_first.setToolTip("Emit and hold the start value. Uncheck to begin at "
                                   "the next level (e.g. to follow another ramp cleanly).")
        self._inc_last.setToolTip("Emit and hold the stop value. Uncheck to end before it "
                                  "(e.g. so the next ramp supplies that value).")
        inc_row = QHBoxLayout()
        inc_row.setContentsMargins(0, 0, 0, 0)
        inc_row.addWidget(self._inc_first)
        inc_row.addWidget(self._inc_last)
        inc_row.addStretch(1)
        self._inc_container = QWidget()
        self._inc_container.setLayout(inc_row)
        self._inc_row_lbl = _row(form, "Include", self._inc_container)

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
        ok_btn = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        ok_btn.setDefault(True)                  # accent primary
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Now everything exists — wire the change signals.
        self._run_chk.toggled.connect(self._sync_target_mode)
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        self._param.currentTextChanged.connect(lambda _t: self._on_param_changed())
        for w in (self._steps, self._step, self._hold, self._duration):
            w.textChanged.connect(self._update_preview)
        self._inc_first.toggled.connect(self._update_preview)
        self._inc_last.toggled.connect(self._update_preview)
        self._offset.valueChanged.connect(self._update_preview)
        self._offset_end.valueChanged.connect(self._update_preview)
        self._anchor.currentIndexChanged.connect(self._sync_anchor)
        self._mode.currentIndexChanged.connect(self._sync_mode)

        # Restore the mode this ramp was authored in (else _sync_anchor defaults to
        # the first mode, hiding the fields the saved ramp actually uses).
        self._init_mode = _mode_for_ramp(r, self._is_both())
        self._ready = True
        self._rebuild_value_fields()   # From/To fields (fallback until params load)
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
                return self._with_cal_bounds(s)
        return None

    def _with_cal_bounds(self, spec: dict) -> dict:
        """If the ramped parameter is the calibrated --power field, narrow its min/max
        to the target unit's resolved dBm range (the task's calibration signal), so
        the range check, preview and unit conform to calibration rather than the
        script's wider declared bounds. For a frequency-dependent chain the range is
        re-folded at the frequency the ramped task runs at (carried from the sequence),
        the same fold the step editor applies — so the range tracks the operating
        frequency, not just the calibration's representative one. Non-power params, no
        unit, or an uncalibrated unit pass through unchanged."""
        getter = getattr(self._editor, "cal_bounds_for_task", None)
        if getter is None:
            return spec
        task = self._task.currentText().strip()
        bounds = getter(task)
        if not bounds:
            return spec
        # Fold the range at the frequency AND the bridge params (a chirp's --bw, GPS C/A's enbw
        # behind --sidelobes) in effect when the ramp fires, so every level From..To is checked
        # against what the unit can actually deliver at the operating point — not the law's
        # representative value. refold_bounds is a no-op when neither applies.
        bounds = refold_bounds(bounds, self._op_freq_hz(task), self._op_params(task))
        out = apply_power_bounds([spec], bounds)[0]
        # Author a density ramp in the CONTROLLED view (a chirp's live spectral density), like the
        # Run/Tune power card: shift the base range into the view at the carried bw and relabel the
        # unit, so the operator ramps the live density and it stays honest at that sweep width.
        view = self._control_view()
        if view is not None and find_power_index([out]) is not None:
            off = self._view_offset(task, view)
            out = dict(out)
            out["unit"] = view["unit"]
            for k in ("min", "max"):
                v = out.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = round(float(v) + off, 4)
        return out

    def _ramp_order_key(self):
        """This ramp's best-effort position on its task's timeline (mirrors
        timeline_model._carry_order_key), so carried state comes only from earlier steps.
        A window-filling ('both') ramp starts at on-air, so it orders like a start anchor."""
        anchor = self._anchor.currentData() or "start"
        off = round(float(self._offset.value()), 1)
        return (1, off) if anchor == "stop" else (0, off)

    def _freq_unit_factor(self, freq_param: str) -> float:
        """Hz per unit of the ramped script's calibration frequency field, so a carried
        value in its own unit (MHz etc.) converts to the Hz that refold_bounds expects."""
        for s in self._all_params:
            if s.get("dest") == freq_param:
                return hz_per_unit(s.get("unit"))
        return 1.0

    def _op_state(self, task: str) -> dict:
        """The effective ``{dest: value}`` parameter state to fold the swept --power range at —
        the ramped task's duration-bar baseline replayed through the earlier same-task steps in
        the sequence (the same carry the step editor uses). ``{}`` when it can't be built. Both
        ``_op_freq_hz`` and ``_op_params`` read from this one snapshot, so the range's frequency
        and its bridge params come from a single consistent operating point."""
        items_getter = getattr(self._editor, "items", None)
        if items_getter is None:
            return {}
        try:
            items = list(items_getter())
            _script, base_args = self._editor.script_for_task(task)
            # Seed from the task's duration-bar args (its on-air baseline) so state is known even
            # when the ramp coincides with the bar's start (same order key, which would otherwise
            # drop the bar); earlier tune steps then carry forward.
            bar_args = next((list(it.args) for it in items
                             if getattr(it, "kind", None) == "bar"
                             and getattr(it, "task_name", None) == task
                             and getattr(it, "args", None)), None)
            return tlm.sequence_effective_values(
                items, task, bar_args or base_args, self._all_params,
                getattr(self._src, "uid", None), target_key=self._ramp_order_key())
        except Exception:      # noqa: BLE001 — a fold helper must never break the editor
            return {}

    def _op_freq_hz(self, task: str) -> Optional[float]:
        """The transmit frequency (Hz) the ramped task is running at when this ramp fires (see
        ``_op_state``) — for folding a frequency-dependent power range. None when the script
        declares no calibration freq param, or it's unset."""
        freq_param = (getattr(self._editor, "_script_cal_freq_params", None) or {}).get(
            self._current_script)
        if not freq_param:
            return None
        val = self._op_state(task).get(freq_param)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return None
        return float(val) * self._freq_unit_factor(freq_param)

    def _op_params(self, task: str) -> Optional[dict]:
        """The bridge-keyed --power params (a chirp's --bw, GPS C/A's enbw behind --sidelobes) the
        ramped task runs with when this ramp fires — so the swept range/snapping fold through them,
        not the law's representative value. None when the signal has no keyed params, an
        uncalibrated unit, or one can't be resolved. Same source as ``_op_freq_hz``."""
        getter = getattr(self._editor, "cal_bounds_for_task", None)
        artifact = ((getter(task) if getter is not None else None) or {}).get("artifact")
        if not artifact:
            return None
        return fold_params_from_values(artifact, self._all_params, self._op_state(task))

    def _control_view(self):
        """The CONTROLLED --power view the ramp authors in — the leading ``restates_measurement``
        law from the script's CAL_POWER_LAWS (a chirp's live spectral density), or None when
        --power is authored in the base/measured quantity. Mirrors ``ParamForm._power_views``'
        drop-base rule: a restatement law stands in only when the reported reading is the measured
        base (a declared reported axis is the operator's chosen quantity and is never dropped)."""
        laws = (getattr(self._editor, "_script_power_laws", None) or {}).get(
            self._current_script) or []
        task = self._task.currentText().strip()
        getter = getattr(self._editor, "cal_bounds_for_task", None)
        art = ((getter(task) if getter is not None else None) or {}).get("artifact") or {}
        rep = (art.get("readings") or {}).get("reported") or {}
        if rep.get("kind") == "law":
            return None
        for spec in laws:
            if isinstance(spec, dict) and spec.get("restates_measurement"):
                try:
                    law = parse_law(spec)
                except (ValueError, TypeError):
                    continue
                unit = str(spec.get("unit") or ("dBm" if law.out_fam == "abs" else "dBm/MHz"))
                return {"id": law.id, "unit": unit, "law": law}
        return None

    def _view_offset(self, task: str, view: Optional[dict]) -> float:
        """dB the control ``view`` adds over the base --power quantity at the ramp's carried
        operating point (a chirp's live --bw) — the shift that turns the base range into the live
        density range and the operator's typed density into the base --power sent. 0 with no view
        law; folds through the ramp's carried bridge params (``_op_state``)."""
        law = (view or {}).get("law") if view else None
        if law is None:
            return 0.0
        keyed = resolve_keyed_values(self._all_params, self._op_state(task), law.params())
        try:
            return law.delta_db(keyed) if keyed else law.rep_delta_db()
        except (ValueError, TypeError):
            return law.rep_delta_db()

    def _ramp_view_offset(self) -> float:
        """The controlled-view offset (dB) when the RAMPED param is the calibrated --power field,
        else 0. From/To are DISPLAYED in the view (base + offset); the STORED ramp start/stop are
        base (offset removed on save, added back on load)."""
        view = self._control_view()
        spec = self._ramped_spec()
        if view is None or not spec or find_power_index([spec]) is None:
            return 0.0
        return self._view_offset(self._task.currentText().strip(), view)

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
        self._rebuild_run_form()      # the ramped param leaves the fixed-value form
        self._rebuild_value_fields()  # From/To take the new parameter's range/unit
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
        # Include first/last applies to single-anchor ramps; a window-filling ramp
        # always spans both edges, so hide the checkboxes there.
        self._inc_row_lbl.setVisible(not both)
        self._inc_container.setVisible(not both)
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
        # Populate ALL per-script caches (params, calibration signal, fold freq AND power laws)
        # through the editor's single writer, so a step editor opened after this ramp editor
        # still finds the power laws and renders the multi-quantity --power card.
        cache_meta = getattr(self._editor, "cache_script_meta", None)
        if cache_meta is not None:
            cache_meta(script, result)
        else:                                    # older editor without the shared writer
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
        self._rebuild_value_fields()   # now the swept param's real range/unit is known
        self._update_preview()

    # ── Preview ──────────────────────────────────────────────────────────────

    def _spec_from_form(self) -> dict:
        fields = _FIELDS.get(self._mode.currentData(), ())
        spec = {"param": self._param.currentText().strip(),
                "start": self._val(self._start_field), "stop": self._val(self._stop_field)}
        if "steps" in fields:
            n = _num(self._steps.text())
            spec["steps"] = int(n) if n is not None else None
        if "step" in fields:
            spec["step"] = _num(self._step.text())
        if "hold" in fields:
            spec["hold_s"] = _num(self._hold.text())
        if "duration" in fields:
            spec["duration_s"] = _num(self._duration.text())
        # First/last-level toggles are meaningful only for a single-anchor ramp; a
        # window-filling ramp always spans both edges, so don't store them there.
        if not self._is_both():
            spec["include_first"] = self._inc_first.isChecked()
            spec["include_last"] = self._inc_last.isChecked()
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

    def _range_error(self) -> Optional[str]:
        """From/To against the ramped parameter's allowed range (or None)."""
        return _ramp_range_error(self._ramped_spec(),
                                 self._val(self._start_field), self._val(self._stop_field))

    # ── From/To bounded fields ───────────────────────────────────────────────

    def _val(self, field) -> Optional[float]:
        """The current value of a From/To field — a BoundedNumberField always has one; a
        plain-line-edit fallback (no numeric spec yet) may be blank."""
        if isinstance(field, BoundedNumberField):
            return field.value()
        if isinstance(field, QLineEdit):
            return _num(field.text())
        return None

    def _power_fold_ctx(self, spec: dict):
        """(PowerFold, freq_hz, fold_params, rail_note, view_offset) for the swept parameter when
        it's the calibrated --power field, so the bounded field snaps to real achievable levels at
        the operating frequency + bridge params, notes what the range moves with, and (for a chirp)
        displays the controlled density view — exactly as the parameter form does. ``view_offset``
        (dB) shifts the displayed view over the base quantity the fold snaps in. (None, None, None,
        "", 0.0) otherwise."""
        task = self._task.currentText().strip()
        getter = getattr(self._editor, "cal_bounds_for_task", None)
        bounds = getter(task) if getter is not None else None
        if not bounds or find_power_index([spec]) is None:
            return None, None, None, "", 0.0
        fold = PowerFold.from_artifact((bounds.get("artifact") or {}))
        freq = self._op_freq_hz(task)
        params = self._op_params(task)
        view = self._control_view()
        view_off = self._view_offset(task, view) if view is not None else 0.0
        note = "Calibrated for this unit"
        if view is not None:
            note = "Range at the live sweep bandwidth"
        elif fold is not None and fold.freq_dependent and isinstance(freq, (int, float)):
            note = f"Range at {freq / 1e6:.2f} MHz · moves with frequency"
        elif fold is not None and fold.param_dependent:
            note = "Range moves with the live parameters"
        return fold, freq, params, note, view_off

    def _make_value_field(self, spec: Optional[dict], value, placeholder: str):
        """A From/To widget for the swept parameter: a bounded numeric field (spinbox + rail
        + limit chip) when the parameter has a numeric min/max, else a plain line edit."""
        if spec and spec.get("type") in ("int", "float") \
                and spec.get("min") is not None and spec.get("max") is not None:
            fold, freq, params, note, view_off = self._power_fold_ctx(spec)
            field = BoundedNumberField(spec, fold=fold, fold_freq=freq, note=note,
                                       fold_params=params, view_offset=view_off)
            if isinstance(value, (int, float)):
                field.setValue(value)
            field.valueChanged.connect(self._update_preview)
            return field
        le = QLineEdit("" if value is None else _fmt(value))
        le.setPlaceholderText(placeholder)
        le.textChanged.connect(self._update_preview)
        return le

    def _rebuild_value_fields(self) -> None:
        """Rebuild the From/To fields for the currently-swept parameter, carrying the values
        over. Called when the parameter, task or its params change so the fields always show
        the right range/unit and (for --power) the calibrated, frequency-folded bound."""
        spec = self._ramped_spec()
        # The saved ramp's start/stop are BASE; show them in the controlled view (+off). The FIRST
        # build runs before params load (offset unknown), so re-seed from the saved base — converted
        # at the now-known offset — until the params are in; after that carry the operator's current
        # (already-view) value across rebuilds so their edits aren't lost.
        off = self._ramp_view_offset()
        seed_start = (self._init_start + off if isinstance(self._init_start, (int, float))
                      else self._init_start)
        seed_stop = (self._init_stop + off if isinstance(self._init_stop, (int, float))
                     else self._init_stop)
        if self._seeded_view and self._start_field is not None:
            cur_start, cur_stop = self._val(self._start_field), self._val(self._stop_field)
        else:
            cur_start, cur_stop = seed_start, seed_stop
            if self._all_params:                     # params are in → this seed is the real one
                self._seeded_view = True
        self._start_field = self._make_value_field(spec, cur_start, "start value")
        self._stop_field = self._make_value_field(spec, cur_stop, "stop value")
        _swap_only(self._start_lay, self._start_field)
        _swap_only(self._stop_lay, self._stop_field)

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

        range_err = self._range_error()   # From/To vs the parameter's allowed range
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
            if range_err:
                lines.append("⚠ " + range_err)
            self._set_preview("\n".join(lines), error=bool(range_err))
            self._refresh_steps_view(spec, both=True)
            return

        anchor = "on-air" if self._anchor.currentData() == "start" else "off-air"
        lines.append(f"Anchor: {anchor}, offset {_off(self._offset.value())}")
        try:
            res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                     step=spec.get("step"), hold_s=spec.get("hold_s"),
                                     duration_s=spec.get("duration_s"),
                                     include_first=spec.get("include_first", True),
                                     include_last=spec.get("include_last", True))
        except (ValueError, TypeError) as exc:
            lines.append("⚠ " + str(exc))
            self._set_preview("\n".join(lines), error=True)
            self._steps_view.setPlainText("")
            return
        step = abs(res.values[1] - res.values[0]) if len(res.values) > 1 else 0
        dropped = [w for w, on in (("first", self._inc_first.isChecked()),
                                   ("last", self._inc_last.isChecked())) if not on]
        excl = f" · {'/'.join(dropped)} excluded" if dropped else ""
        lines.append(f"{len(res.values)} levels · {res.n_intervals} steps · "
                     f"step size {fmt_value(_clean(step))}{u} · hold {fmt_duration(res.hold_s)} · "
                     f"duration {fmt_duration(res.duration_s)}{excl}")
        if range_err:
            lines.append("⚠ " + range_err)
        self._set_preview("\n".join(lines), error=bool(range_err))
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
                                     duration_s=spec.get("duration_s"),
                                     include_first=spec.get("include_first", True),
                                     include_last=spec.get("include_last", True))
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
        range_err = self._range_error()
        if range_err:
            return self._set_preview(range_err, error=True)
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

        # A density ramp is AUTHORED in the controlled view (From/To are live density); store the
        # ramp start/stop in the BASE quantity the unit is commanded in (subtract the view offset at
        # the carried bw — constant over a fixed-bw ramp, so a linear density sweep stays a linear
        # base sweep), and record the control view so the walk/hold treat it as that quantity.
        view = self._control_view()
        off = self._ramp_view_offset()
        if off:
            for k in ("start", "stop"):
                if isinstance(spec.get(k), (int, float)):
                    spec[k] = round(spec[k] - off, 4)
        ramp = {k: v for k, v in spec.items() if v is not None}
        power_view = view["id"] if (view is not None and find_power_index(
            [self._ramped_spec() or {}]) is not None) else None
        self.result_item = tlm.RunItem(
            task_name=task, action="ramp", ramp=ramp, anchor=anchor,
            offset=offset, offset_end=offset_end,
            args=args, replace_args=True, uid=self._src.uid, power_view=power_view)
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
