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

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from api import ramp as _ramp
from state.power_fold import (PowerFold, fold_params_from_values, refold_bounds,
                              resolve_keyed_values)
from state.power_law import parse_law

from . import timeline_model as tlm
from .duration_spin import DurationSpinBox
from .param_form import (
    BoundedNumberField, ParamForm, _family_chip, apply_gain_bounds, apply_power_bounds,
    find_power_index, fmt_duration, fmt_value, hz_per_unit, range_hint,
)
from .param_widgets import DualRangeRail
from .theme import Palette, mono_font


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
        # The --power quantity the ramp is authored in (a CAL_POWER_LAWS view id, or None for the
        # signal's base/measured quantity). Seeded from the saved ramp; the "Set power in" picker
        # lets the operator switch it, exactly like the Run/Tune power card's "Control in this →".
        self._power_view = getattr(self._src, "power_view", None)
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
        # The sibling fields are contained ".ofield" rows (a bordered surface-alt box with an
        # accent-ink label + the control), matching the mockup. The controls are FLATTENED inside
        # the box (no own border/inset) so the box provides the boundary — the DurationSpinBox and
        # Dropdown keep their painted chevron (padding reserves its room).
        self.setStyleSheet(editor_qss() + f"""
QFrame#ofield {{ background: {Palette.SURFACE_ALT}; border: 1px solid {Palette.BORDER};
    border-radius: 8px; }}
QFrame#ofield QComboBox, QFrame#ofield QLineEdit, QFrame#ofield QAbstractSpinBox {{
    background: transparent; border: none; border-radius: 0; min-height: 24px; padding: 2px 0; }}
QFrame#ofield DurationSpinBox {{ padding: 2px 30px 2px 0; }}
QFrame#ofield QComboBox:focus, QFrame#ofield QLineEdit:focus,
QFrame#ofield QAbstractSpinBox:focus {{ background: transparent; border: none; }}
QFrame#ofield QCheckBox {{ background: transparent; }}
""")
        # One scroll environment for the whole form: the fields, the run-mode params, the preview
        # AND the per-step listing all live in a single scrollable body, so a long step list scrolls
        # with everything else instead of being trapped in its own tiny box. The button row is pinned
        # beneath the scroll (assembled after the body is populated).
        dlg_lay = QVBoxLayout(self)
        dlg_lay.setContentsMargins(0, 0, 0, 0)
        dlg_lay.setSpacing(0)
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)
        # The sibling fields stack as full-width ".ofield" rows (not a QFormLayout), so they flow
        # edge-to-edge with the power card exactly like the mockup.
        form = QVBoxLayout()
        form.setContentsMargins(0, 0, 0, 0)
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
        form.addWidget(self._run_chk)

        self._task = Dropdown()
        self._populate_tasks()
        form.addWidget(_ofield("Task", self._task))

        self._param = Dropdown()
        form.addWidget(_ofield("Parameter", self._param))

        # Which quantity to author a calibrated --power ramp in (spectral density / total power /
        # dBm-per-Hz …) — the ramp analogue of the Run/Tune power card's "Control in this →". It is
        # now driven by the per-quantity "Ramp in this →" buttons on the power card's companion
        # tiles, so the combo itself is a hidden state/logic holder (kept for its view-conversion
        # wiring — _on_power_view_changed), never shown; the visible switch is the card.
        self._power_unit = Dropdown()
        self._power_unit.setParent(self)
        self._power_unit.hide()

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
        # The From/To fields live in ONE full-width area that renders EITHER the styled
        # multi-quantity power card (calibrated --power with ≥2 views — the ramp analogue of the
        # Run/Tune power card: a RAMPING IN primary + From/To, an ALSO READS AS companion grid, a
        # DEPENDS ON row) OR plain From/To rows (any other parameter). Rebuilt by
        # _render_power_area when the swept parameter, task or its params change; the persistent
        # From/To boxes are re-parented across renders, never deleted with the old card.
        self._card_active = False
        self._span_lbl = None
        self._companion_labels: List[tuple] = []
        self._pwr_rail = None          # the shared dual-handle From/To rail (card mode)
        self._pwr_min = None
        self._pwr_max = None
        self._ft_from_lbl = None       # FROM/TO sub-labels (fire times) — refreshed live
        self._ft_to_lbl = None
        self._power_area = QWidget()
        self._power_area_lay = QVBoxLayout(self._power_area)
        self._power_area_lay.setContentsMargins(0, 0, 0, 0)
        self._power_area_lay.setSpacing(0)
        form.addWidget(self._power_area)

        self._anchor = Dropdown()
        self._anchor.addItem("On-air (T0)", "start")
        self._anchor.addItem("Off-air", "stop")
        self._anchor.addItem("Fill on-air window", "both")
        # A window-B ramp (the down-ramp) runs forward from the Hold's resume instant —
        # offered once a Hold exists on the timeline, or when editing a ramp already
        # anchored to it. The runtime + canvas already resolve anchor="hold" (Phase 1/2).
        src_anchor = getattr(self._src, "anchor", "start")
        if getattr(self._editor, "has_hold", lambda: False)() or src_anchor == "hold":
            self._anchor.addItem("Hold (after Hold)", "hold")
        # Step-to-step anchoring (agent ≥ 1.24.0): run this ramp forward from ANOTHER step's
        # start/end edge — e.g. a down-ramp right after an up-ramp's end. Offered when there's
        # an eligible target (no cycle) or the ramp already uses it; saving to an agent that
        # can't resolve it is blocked at save-time. Mutually exclusive with a Hold (Phase 1).
        items_getter = getattr(self._editor, "items", None)
        self._step_targets = tlm.eligible_step_targets(list(items_getter()), self._src.uid) \
            if (items_getter is not None and not getattr(self._editor, "has_hold", lambda: False)()) \
            else []
        if self._step_targets or src_anchor == "step":
            self._anchor.addItem("After step…", "step")
        _ai = self._anchor.findData(src_anchor)
        self._anchor.setCurrentIndex(_ai if _ai >= 0 else 0)

        self._offset = _spin(float(getattr(self._src, "offset", 0.0)))
        self._offset_end = _spin(float(getattr(self._src, "offset_end", 0.0)))
        form.addWidget(_ofield("Anchor", self._anchor))

        # Step-anchor target + edge pickers (shown only when anchor == "step").
        self._anchor_target = Dropdown()
        for tgt in self._step_targets:
            self._anchor_target.addItem(tlm._target_label(tgt), getattr(tgt, "uid", None))
        self._anchor_edge = Dropdown()
        self._anchor_edge.addItem("its end", "end")
        self._anchor_edge.addItem("its start", "start")
        self._target_row = _ofield("Anchor to", self._anchor_target)
        self._edge_row = _ofield("Relative to", self._anchor_edge)
        form.addWidget(self._target_row)
        form.addWidget(self._edge_row)
        if src_anchor == "step":
            want = getattr(self._src, "anchor_step_id", "") or ""
            for tgt in self._step_targets:
                if (getattr(tgt, "step_id", "") or "") == want:
                    ti = self._anchor_target.findData(getattr(tgt, "uid", None))
                    if ti >= 0:
                        self._anchor_target.setCurrentIndex(ti)
                    break
            ei = self._anchor_edge.findData(getattr(self._src, "anchor_edge", "end") or "end")
            self._anchor_edge.setCurrentIndex(ei if ei >= 0 else 0)

        self._off_row = _ofield("Offset from anchor", self._offset)
        self._off_lbl = self._off_row._klabel
        self._offend_row = _ofield("End offset from off-air", self._offset_end)
        form.addWidget(self._off_row)
        form.addWidget(self._offend_row)

        self._mode = Dropdown()
        form.addWidget(_ofield("Define by", self._mode))

        self._steps = QLineEdit();    self._steps.setPlaceholderText("count (equal increments)")
        self._step = QLineEdit();     self._step.setPlaceholderText("increment per step")
        self._hold = QLineEdit();     self._hold.setPlaceholderText("seconds per step")
        self._duration = QLineEdit(); self._duration.setPlaceholderText("seconds")
        self._row_steps = _ofield("Number of steps", self._steps, "levels")
        self._row_step = _ofield("Step size", self._step)
        self._row_hold = _ofield("Hold time", self._hold, "s / step")
        self._row_duration = _ofield("Duration", self._duration, "s")
        for _rw in (self._row_steps, self._row_step, self._row_hold, self._row_duration):
            form.addWidget(_rw)
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
        self._inc_row = _ofield("Include", self._inc_container)
        form.addWidget(self._inc_row)

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

        # Offline-calibration notice: when the ramped --power range is folded from the last-known
        # (cached) calibration because the target unit is offline, say so — it refreshes on reconnect.
        self._cal_note = QLabel("")
        self._cal_note.setWordWrap(True)
        self._cal_note.setStyleSheet(
            f"font-size: 11px; color: {Palette.ARMED}; background: {Palette.ARMED_SOFT}; "
            f"border: 1px solid #ecd3a3; border-radius: 8px; padding: 6px 9px;")
        self._cal_note.setVisible(False)
        outer.addWidget(self._cal_note)

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
        # A QLabel (not a fixed-height text box with its OWN scrollbar) so the listing expands to its
        # full height inside the shared scroll — the operator sees every step, not one line at a time.
        self._steps_view = QLabel("")
        self._steps_view.setFont(QFont("monospace"))
        self._steps_view.setTextFormat(Qt.TextFormat.PlainText)      # \n are line breaks, content literal
        self._steps_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._steps_view.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._steps_view.setStyleSheet(
            f"QLabel {{ background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 8px; padding: 8px; font-size: 11px; }}")
        self._steps_view.setVisible(False)
        outer.addWidget(self._steps_view)

        # The scrollable body holds everything above; assemble it, then pin the button row beneath.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        dlg_lay.addWidget(scroll, 1)

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
        btn_row = QWidget()
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(16, 8, 16, 12)
        btn_lay.addWidget(buttons)
        dlg_lay.addWidget(btn_row)

        # Now everything exists — wire the change signals.
        self._run_chk.toggled.connect(self._sync_target_mode)
        self._task.currentTextChanged.connect(lambda t: self._select_task(t))
        self._param.currentTextChanged.connect(lambda _t: self._on_param_changed())
        self._power_unit.currentIndexChanged.connect(self._on_power_view_changed)
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
        self._render_power_area()      # From/To fields + power card (fallback until params load)
        self._apply_mode_visibility()
        self._sync_anchor()   # populate modes + show/hide rows + preview
        # Open at a size that fits the power card horizontally (its two From/To columns + the
        # ALSO READS AS companions need real width) and shows a large part of the form vertically,
        # instead of the tiny min-size-hint the scroll area would otherwise collapse to. The body
        # is scrollable, and the height is capped to the screen so the buttons stay on-screen.
        self.setMinimumWidth(560)
        want_w, want_h = 700, 820
        scr = QApplication.primaryScreen()
        if scr is not None and scr.availableGeometry().height() > 240:
            avail = scr.availableGeometry()
            self.setMaximumHeight(int(avail.height() * 0.92))
            want_w = min(want_w, int(avail.width() * 0.95))
            want_h = min(want_h, int(avail.height() * 0.9))
        self.resize(max(self.minimumWidth(), want_w), want_h)

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
        """If the ramped parameter is the calibrated --power OR --gain field, narrow its min/max
        to the target unit's resolved calibration range (the task's calibration signal), so
        the range check, preview and unit conform to calibration rather than the
        script's wider declared bounds. --power gets the resolved dBm range (achievable-level
        snapping); relative --gain gets the usable gain range [min_gain_db, max_gain_db] on the
        SDR's real gain step, so the slider lands on commandable gains and can't overshoot the
        calibrated ceiling. For a frequency-dependent chain the range is re-folded at the frequency
        the ramped task runs at (carried from the sequence), the same fold the step editor applies.
        Other params, no unit, or an uncalibrated unit pass through unchanged."""
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
        # apply_power_bounds targets --power, apply_gain_bounds targets --gain; each is a no-op for
        # the other's field, so chaining both narrows whichever one this ramp sweeps.
        out = apply_power_bounds([spec], bounds)[0]
        out = apply_gain_bounds([out], bounds)[0]
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
        timeline_model.carry_order_key), so carried state comes only from earlier steps.
        A window-filling ('both') ramp starts at on-air, so it orders like a start anchor; a
        window-B (anchor='hold') ramp orders after window A (hold-boundary aware)."""
        anchor = self._anchor.currentData() or "start"
        off = round(float(self._offset.value()), 1)
        items_getter = getattr(self._editor, "items", None)
        items = list(items_getter()) if items_getter is not None else []
        h_off = tlm.hold_offset(items) if items else None
        if anchor == "step":
            # Order at the RESOLVED base (target edge + offset) so only genuinely-earlier steps
            # carry state into this ramp's operating point — mirror _carry_order_key's step branch.
            bases = tlm.resolve_step_offsets(items, h_off)
            base = bases.get(getattr(self._src, "uid", None))
            if base is not None:
                return (0, base)
        return tlm.carry_order_key(anchor, off, h_off)

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

    def _power_views(self) -> List[dict]:
        """Selectable --power unit views for a calibrated ramp — the base (embedded reported)
        quantity plus each declared CAL_POWER_LAW that reads DIFFERENTLY, with the SAME drop-base
        rule as ``ParamForm._power_views``: a ``restates_measurement`` law re-expresses the raw
        measured reading, so that measured base is dropped and the restatement stands in (a declared
        reported axis is the operator's chosen quantity and is never dropped). Empty unless the
        signal declares ≥1 differing law. Each view: ``{id, name, unit, law}`` (``law`` None for
        the base). Powers the "Set power in" picker AND the fold — ``_selected_view`` chooses one."""
        laws = (getattr(self._editor, "_script_power_laws", None) or {}).get(
            self._current_script) or []
        if not laws:
            return []
        task = self._task.currentText().strip()
        getter = getattr(self._editor, "cal_bounds_for_task", None)
        art = ((getter(task) if getter is not None else None) or {}).get("artifact") or {}
        rep = (art.get("readings") or {}).get("reported") or {}
        base_law_id = (rep.get("law") or {}).get("id") if rep.get("kind") == "law" else None
        base_unit = (art.get("operating_unit") or "").strip() or "dBm"
        base_name = art.get("quantity") or (rep.get("law") or {}).get("name") or "power"
        base_view = {"id": None, "name": base_name, "unit": base_unit, "law": None}
        law_views: List[dict] = []
        drop_base = False
        for spec in laws:
            if not isinstance(spec, dict):
                continue
            try:
                law = parse_law(spec)
            except (ValueError, TypeError):
                continue
            if law.id == base_law_id:
                continue
            unit = str(spec.get("unit") or ("dBm" if law.out_fam == "abs" else "dBm/MHz"))
            law_views.append({"id": law.id, "name": spec.get("name", law.id),
                              "unit": unit, "law": law})
            if spec.get("restates_measurement"):
                drop_base = True
        if not law_views:
            return []
        if drop_base and base_law_id is None:   # only the RAW measured quantity is a restatement target
            return law_views
        return [base_view] + law_views

    def _selected_view(self) -> Optional[dict]:
        """The view the ramp is currently authored in — the one whose id matches ``_power_view``,
        else the first (default) view, or None when the signal offers no views."""
        views = self._power_views()
        if not views:
            return None
        return next((v for v in views if v["id"] == self._power_view), views[0])

    def _control_view(self):
        """The CONTROLLED --power view the ramp authors in (the operator's "Set power in" choice —
        a chirp's live spectral density by default), or None when --power is authored in the
        base/measured quantity / the signal offers no view."""
        return self._selected_view()

    def _populate_power_views(self) -> None:
        """(Re)fill the hidden --power quantity picker for the currently-swept parameter, keeping
        the current selection valid (falls back to the default view). The card's companion
        "Ramp in this →" buttons drive this combo; the card itself is shown/hidden by
        _render_power_area. Signals are blocked so repopulating never spuriously re-folds; the
        caller rebuilds the From/To fields afterwards."""
        spec = self._ramped_spec()
        views = self._power_views() if (spec and find_power_index([spec]) is not None) else []
        self._power_unit.blockSignals(True)
        self._power_unit.clear()
        for v in views:
            unit = (v.get("unit") or "dBm").strip()
            name = (v.get("name") or "").strip()
            label = f"{name} [{unit}]" if name and name.lower() != "power" else unit
            self._power_unit.addItem(label, v["id"])
        if views:
            idx = next((i for i, v in enumerate(views) if v["id"] == self._power_view), 0)
            self._power_unit.setCurrentIndex(idx)
            self._power_view = views[idx]["id"]   # snap a stale/absent selection to the default
        self._power_unit.blockSignals(False)

    def _on_power_view_changed(self, *_) -> None:
        """The operator picked a different --power quantity: convert the current From/To through the
        base quantity into the new view (hold the same physical power, re-expressed — like the card's
        "Control in this →"), then rebuild the range/unit and re-preview."""
        if not self._ready:
            return
        old_off = self._ramp_view_offset()
        cur_start, cur_stop = self._val(self._start_field), self._val(self._stop_field)
        self._power_view = self._power_unit.currentData()
        delta = self._ramp_view_offset() - old_off   # base is unchanged; the display shifts by Δoffset
        self._render_power_area()                     # new view's card (primary/companions) + fields
        if isinstance(cur_start, (int, float)) and isinstance(self._start_field, BoundedNumberField):
            self._start_field.setValue(cur_start + delta)
        if isinstance(cur_stop, (int, float)) and isinstance(self._stop_field, BoundedNumberField):
            self._stop_field.setValue(cur_stop + delta)
        self._update_preview()

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
        self._render_power_area()     # From/To + the power card (or plain rows) for the new param
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
        anchor = self._anchor.currentData() or "start"
        # A window-filling ramp is inset from BOTH edges; a single-anchor ramp has
        # one offset from its anchor (from the Hold's resume instant for a window-B ramp).
        if both:
            self._off_lbl.setText("Start offset from on-air")
        elif anchor == "hold":
            self._off_lbl.setText("Offset from Hold (resume)")
        elif anchor == "step":
            self._off_lbl.setText("Offset after the step")
        else:
            self._off_lbl.setText("Offset from anchor")
        self._offend_row.setVisible(both)
        # Target/edge pickers only when anchoring to another step.
        is_step = anchor == "step"
        if getattr(self, "_target_row", None) is not None:
            self._target_row.setVisible(is_step)
            self._edge_row.setVisible(is_step)
        # Include first/last applies to single-anchor ramps; a window-filling ramp
        # always spans both edges, so hide the whole row there.
        self._inc_row.setVisible(not both)
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
        for name, row in (("steps", self._row_steps), ("step", self._row_step),
                          ("hold", self._row_hold), ("duration", self._row_duration)):
            row.setVisible(name in fields)
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
        self._render_power_area()      # the swept param's From/To + power card (or plain rows)
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
        view_law = (view or {}).get("law")
        note = "Calibrated for this unit"
        if view_law is not None and view_law.params():
            # A bandwidth-keyed view (a chirp's live spectral density): the displayed range is the
            # live density at the carried sweep width. A view with no keyed param (total power) or
            # the base quantity keeps the ordinary freq/param note below.
            note = "Range at the live sweep bandwidth"
        elif fold is not None and fold.freq_dependent and isinstance(freq, (int, float)):
            note = f"Range at {freq / 1e6:.2f} MHz · moves with frequency"
        elif fold is not None and fold.param_dependent:
            note = "Range moves with the live parameters"
        return fold, freq, params, note, view_off

    def _make_value_field(self, spec: Optional[dict], value, placeholder: str,
                          card: bool = False):
        """A From/To widget for the swept parameter: a bounded numeric field (spinbox + rail
        + limit chip) when the parameter has a numeric min/max, else a plain line edit.
        ``card=True`` builds the mockup's .p-input (no own rail/chip — the power card supplies one
        shared dual rail)."""
        if spec and spec.get("type") in ("int", "float") \
                and spec.get("min") is not None and spec.get("max") is not None:
            fold, freq, params, note, view_off = self._power_fold_ctx(spec)
            field = BoundedNumberField(spec, fold=fold, fold_freq=freq, note=note,
                                       fold_params=params, view_offset=view_off,
                                       show_rail=not card, pinput=card)
            if isinstance(value, (int, float)):
                field.setValue(value)
            field.valueChanged.connect(self._update_preview)
            return field
        le = QLineEdit("" if value is None else _fmt(value))
        le.setPlaceholderText(placeholder)
        le.textChanged.connect(self._update_preview)
        return le

    def _rebuild_value_fields(self, card: bool = False) -> None:
        """Rebuild the From/To fields for the currently-swept parameter, carrying the values
        over. Called when the parameter, task or its params change so the fields always show
        the right range/unit and (for --power) the calibrated, frequency-folded bound.
        ``card=True`` builds each field as the mockup's .p-input (no own rail — the card adds a
        shared dual-handle one)."""
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
        self._start_field = self._make_value_field(spec, cur_start, "start value", card)
        self._stop_field = self._make_value_field(spec, cur_stop, "stop value", card)
        _swap_only(self._start_lay, self._start_field)
        _swap_only(self._stop_lay, self._stop_field)

    # ── Power card (the multi-quantity presentation of From/To) ─────────────────
    def _render_power_area(self) -> None:
        """Rebuild the From/To area for the currently-swept parameter: the styled multi-quantity
        power card (the ramp analogue of the Run/Tune power card) when the parameter is the
        calibrated --power field and the signal offers ≥2 quantities, else plain From/To rows.
        Rebuilds the From/To bounded fields and the hidden quantity picker first, then lays them
        into the chosen presentation. The persistent From/To boxes are re-parented, never deleted."""
        if not self._ready:
            return
        self._populate_power_views()     # hidden picker reflects the swept param's views
        spec = self._ramped_spec()
        is_power = spec is not None and find_power_index([spec]) is not None
        views = self._power_views() if is_power else []
        card = is_power and len(views) >= 2
        # In card mode the two fields share ONE dual-handle rail and render as the mockup's .p-input
        # (no own rail/chip); plain mode keeps each field's own rail. (Build after the card decision.)
        self._rebuild_value_fields(card=card)
        self._clear_power_area()
        if card:
            root = self._build_power_card(spec, views)
            self._card_active = True
        else:
            root = self._build_plain_fromto()
        self._power_area_lay.addWidget(root)
        self._update_power_readouts()

    def _clear_power_area(self) -> None:
        """Empty the power area, detaching the persistent From/To boxes FIRST so deleting the
        previous card (a child of the area) doesn't take them with it, and dropping the per-render
        read-out references so a stale card's labels are never touched."""
        for box in (self._start_box, self._stop_box):
            if box is not None:
                box.setParent(None)
        while self._power_area_lay.count():
            item = self._power_area_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._span_lbl = None
        self._companion_labels = []
        self._pwr_rail = None
        self._pwr_min = None
        self._pwr_max = None
        self._ft_from_lbl = None
        self._ft_to_lbl = None
        self._card_active = False

    def _build_plain_fromto(self) -> QWidget:
        """Plain 'From' / 'To' rows — the presentation for any parameter that is not a calibrated
        --power field with ≥2 quantities (an ordinary numeric knob, or --power with a single view)."""
        w = QWidget()
        fl = QVBoxLayout(w)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        fl.addWidget(_ofield("From", self._start_box))
        fl.addWidget(_ofield("To", self._stop_box))
        return w

    def _build_power_card(self, spec: dict, views: List[dict]) -> QWidget:
        """The multi-quantity --power card: a RAMPING IN primary (the swept quantity's name +
        family chip, the From/To bounded fields, a span read-out and a DEPENDS ON row) and an
        ALSO READS AS grid of read-only companion tiles (each other quantity's live From → To with
        a 'Ramp in this →' switch). Matches docs/ramp-power-mockup.html and the Run/Tune power card
        (ui/param_form._add_power_unit_ui) — the values-carrying widgets are the same BoundedNumber
        fields the plain rows use, so all calibrated folding/snapping/clamping is preserved."""
        selected = self._selected_view() or {}
        p_unit = (selected.get("unit") or spec.get("unit") or "dBm").strip()

        card = QFrame(); card.setObjectName("rampPwrCard")
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        card.setStyleSheet(
            f"#rampPwrCard {{ background: {Palette.SURFACE}; border: 1px solid {Palette.ACCENT_SOFT}; "
            f"border-radius: 10px; }}")
        v = QVBoxLayout(card); v.setContentsMargins(14, 12, 14, 14); v.setSpacing(10)

        # header: eyebrow + one-line lead + LIVE badge
        head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 0); head.setSpacing(9)
        head.addWidget(_uc_label("RAMP POWER", 11, Palette.ACCENT, 1.3))
        lead = QLabel("sweep From → To in any one quantity — the rest track it live")
        lead.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px;")
        head.addWidget(lead); head.addStretch(1)
        head.addWidget(self._live_badge())
        v.addLayout(head)

        # primary block — the quantity you sweep. Vertical accent-soft → surface gradient (the
        # mockup's linear-gradient(180deg, accent-soft, surface 62%)).
        prim = QFrame(); prim.setObjectName("rampPwrPrimary")
        prim.setStyleSheet(
            f"#rampPwrPrimary {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {Palette.ACCENT_SOFT}, stop:0.62 {Palette.SURFACE}); "
            f"border: 1px solid {Palette.BORDER_STRONG}; border-radius: 10px; }}")
        pv = QVBoxLayout(prim); pv.setContentsMargins(14, 13, 14, 13); pv.setSpacing(9)

        topline = QHBoxLayout(); topline.setContentsMargins(0, 0, 0, 0); topline.setSpacing(9)
        tag = _uc_label("RAMPING IN", 9, Palette.ACCENT, 0.8)
        tag.setStyleSheet(tag.styleSheet() + f" background: {Palette.SURFACE}; "
                          f"border: 1px solid {Palette.ACCENT_SOFT}; border-radius: 5px; padding: 2px 7px;")
        topline.addWidget(tag)
        pname = QLabel(selected.get("name") or "power")
        nf = QFont("IBM Plex Sans"); nf.setPixelSize(15); nf.setWeight(QFont.Weight.DemiBold)
        pname.setFont(nf); pname.setStyleSheet(f"color: {Palette.TEXT};")
        topline.addWidget(pname)
        topline.addWidget(_family_chip(p_unit, p_unit))
        topline.addStretch(1)
        pv.addLayout(topline)

        # From / To — the persistent bounded fields, now rendered as the mockup's .p-input (built
        # by BoundedNumberField in pinput mode). Their sub-labels reflect the ACTUAL fire times
        # for the current anchor + offset (not a hardcoded on-air/off-air).
        from_sub, to_sub = self._ft_sublabels()
        self._ft_from_lbl = self._ft_label("FROM", from_sub)
        self._ft_to_lbl = self._ft_label("TO", to_sub)
        grid = QGridLayout(); grid.setContentsMargins(0, 6, 0, 2)
        grid.setHorizontalSpacing(14); grid.setVerticalSpacing(4)
        grid.addWidget(self._ft_from_lbl, 0, 0)
        grid.addWidget(self._ft_to_lbl, 0, 1)
        grid.addWidget(self._start_box, 1, 0)
        grid.addWidget(self._stop_box, 1, 1)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        pv.addLayout(grid)

        # ONE shared dual-handle rail over the achievable MIN..MAX, the swept span filled between
        # the two handles — the mockup's single power slider with two handles.
        lo, hi = (self._start_field.bounds()
                  if isinstance(self._start_field, BoundedNumberField) else (None, None))
        mm = QHBoxLayout(); mm.setContentsMargins(0, 8, 0, 0)
        self._pwr_min = QLabel(); self._pwr_max = QLabel()
        for _lbl in (self._pwr_min, self._pwr_max):
            _lbl.setFont(mono_font(11)); _lbl.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        mm.addWidget(self._pwr_min); mm.addStretch(1); mm.addWidget(self._pwr_max)
        pv.addLayout(mm)
        rail = DualRangeRail()
        if lo is not None and hi is not None:
            rail.set_bounds(lo, hi)
        rail.set_from(self._val(self._start_field)); rail.set_to(self._val(self._stop_field))
        rail.fromMoved.connect(lambda vv: self._on_rail_drag("from", vv))
        rail.toMoved.connect(lambda vv: self._on_rail_drag("to", vv))
        self._pwr_rail = rail
        pv.addWidget(rail)

        # span read-out (rising / falling / flat), in the primary quantity
        self._span_lbl = QLabel("—")
        self._span_lbl.setFont(mono_font(11, 500))
        self._span_lbl.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        pv.addWidget(self._span_lbl)

        # DEPENDS ON — the fold inputs (frequency + carried bridge knobs) the range moves with
        fold, freq, _params, _note, _off = self._power_fold_ctx(spec)
        dep_row = self._build_deps_row(spec, fold, freq)
        if dep_row is not None:
            pv.addWidget(dep_row)
        v.addWidget(prim)

        # ALSO READS AS — one read-only companion per OTHER quantity, each promotable to primary.
        # Two equal-stretch columns so the tiles fill the card width and flow with it.
        others = [x for x in views if x.get("id") != selected.get("id")]
        if others:
            v.addWidget(self._reads_divider())
            cg = QGridLayout(); cg.setContentsMargins(0, 0, 0, 0)
            cg.setHorizontalSpacing(10); cg.setVerticalSpacing(10)
            for i, view in enumerate(others):
                cg.addWidget(self._companion_card(view), i // 2, i % 2)
            cg.setColumnStretch(0, 1); cg.setColumnStretch(1, 1)
            v.addLayout(cg)
        return card

    def _live_badge(self) -> QLabel:
        b = QLabel("● LIVE")
        f = QFont("IBM Plex Sans"); f.setPixelSize(10); f.setWeight(QFont.Weight.DemiBold)
        b.setFont(f)
        b.setStyleSheet(
            f"color: {Palette.ONLINE}; background: {Palette.ONLINE_SOFT}; border: 1px solid #BFE6D7; "
            f"border-radius: 10px; padding: 2px 9px;")
        return b

    def _ft_label(self, main: str, sub: str) -> QLabel:
        lbl = QLabel(f"{main}  ({sub})")
        f = QFont("IBM Plex Sans"); f.setPixelSize(9); f.setWeight(QFont.Weight.Bold)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {Palette.TEXT_FAINT}; letter-spacing: 0.8px;")
        return lbl

    def _reads_divider(self) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 4, 0, 0); h.setSpacing(10)
        h.addWidget(_uc_label("ALSO READS AS", 10, Palette.TEXT_FAINT, 0.9))
        rule = QFrame(); rule.setFrameShape(QFrame.Shape.HLine); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {Palette.BORDER}; border: none;")
        h.addWidget(rule, 1)
        return row

    def _companion_card(self, view: dict) -> QWidget:
        """A read-only companion tile: the quantity name + family chip, its live From → To value,
        a live marker, and a 'Ramp in this →' button that promotes it to the primary (drives the
        hidden picker, so all the view-conversion wiring runs). Its value labels are registered for
        live refresh in _update_power_readouts."""
        card = QFrame(); card.setObjectName("rampCompCard")
        card.setStyleSheet(
            f"#rampCompCard {{ background: {Palette.SURFACE_ALT}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 7px; }}")
        cv = QVBoxLayout(card); cv.setContentsMargins(13, 11, 13, 11); cv.setSpacing(8)
        unit = (view.get("unit") or "dBm").strip()

        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(8)
        name = QLabel(view.get("name") or "power"); name.setWordWrap(True)
        nf = QFont("IBM Plex Sans"); nf.setPixelSize(12); nf.setWeight(QFont.Weight.DemiBold)
        name.setFont(nf); name.setStyleSheet(f"color: {Palette.TEXT};")
        top.addWidget(name, 1)
        top.addWidget(_family_chip(unit, unit))
        cv.addLayout(top)

        valrow = QHBoxLayout(); valrow.setContentsMargins(0, 0, 0, 0); valrow.setSpacing(7)
        from_lbl = QLabel("—"); from_lbl.setFont(mono_font(16, 500))
        from_lbl.setStyleSheet(f"color: {Palette.TEXT};")
        arrow = QLabel("→"); arrow.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        to_lbl = QLabel("—"); to_lbl.setFont(mono_font(16, 600))
        to_lbl.setStyleSheet(f"color: {Palette.TEXT};")
        u = QLabel(unit); u.setFont(mono_font(11)); u.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        valrow.addWidget(from_lbl); valrow.addWidget(arrow); valrow.addWidget(to_lbl)
        valrow.addWidget(u); valrow.addStretch(1)
        cv.addLayout(valrow)

        foot = QHBoxLayout(); foot.setContentsMargins(0, 0, 0, 0); foot.setSpacing(8)
        live = QLabel("● live"); lf = QFont("IBM Plex Sans"); lf.setPixelSize(10); live.setFont(lf)
        live.setStyleSheet(f"color: {Palette.ONLINE};")
        foot.addWidget(live); foot.addStretch(1)
        btn = QPushButton("Ramp in this →"); btn.setObjectName("rampInThis")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton#rampInThis {{ color: {Palette.ACCENT}; background: transparent; "
            f"border: none; padding: 3px 4px; font-size: 11px; font-weight: 500; }}"
            f"QPushButton#rampInThis:hover {{ background: {Palette.ACCENT_SOFT}; "
            f"border-radius: 5px; text-decoration: underline; }}")
        btn.clicked.connect(lambda _=False, vid=view.get("id"): self._set_ramp_power_view(vid))
        foot.addWidget(btn)
        cv.addLayout(foot)

        self._companion_labels.append((view, from_lbl, to_lbl))
        return card

    def _set_ramp_power_view(self, vid) -> None:
        """Promote a companion quantity to the primary — the card's 'Ramp in this →'. Drives the
        hidden picker (setCurrentIndex fires _on_power_view_changed), so the From/To values are
        re-expressed in the new quantity and the base ramp the unit is commanded in is unchanged."""
        if vid == self._power_view:
            return
        idx = self._power_unit.findData(vid)
        if idx >= 0:
            self._power_unit.setCurrentIndex(idx)

    def _ft_sublabels(self) -> tuple:
        """The FROM/TO sub-labels describing WHEN each endpoint fires, from the current anchor +
        offset (see api.ramp): anchor 'start' pins the FROM point to on-air T0 + offset and runs
        forward to the ramp end; anchor 'stop' holds the TO level up to off-air + offset (offset ≤
        0); anchor 'both' fills on-air + start-inset .. off-air − end-inset. Replaces the old
        hardcoded 'on-air, T0' / 'off-air'."""
        anchor = self._anchor.currentData() or "start"
        off = float(self._offset.value())
        if anchor == "both":
            end = float(self._offset_end.value())
            return (self._time_sub("on-air", off), self._time_sub("off-air", -abs(end)))
        if anchor == "stop":
            return ("ramp start", self._time_sub("off-air", off))
        if anchor == "hold":
            # Window B: the ramp runs forward from the resume instant to its end.
            return (self._time_sub("on-resume", off), "ramp end")
        return (self._time_sub("on-air", off), "ramp end")

    @staticmethod
    def _time_sub(edge: str, secs: float) -> str:
        """A fire-time sub-label relative to an edge: 'at on-air (T0)' at 0, else e.g.
        'on-air +10 s' / 'off-air −5 s'."""
        if abs(secs) < 1e-9:
            return f"at {edge}" + (" (T0)" if edge == "on-air" else "")
        sign = "+" if secs > 0 else "−"
        return f"{edge} {sign}{fmt_duration(abs(secs))}"

    def _on_rail_drag(self, which: str, v: float) -> None:
        """A drag on the shared dual rail: snap the value to a real achievable level and write it
        into the matching From/To field (its valueChanged then re-syncs the handle position)."""
        field = self._start_field if which == "from" else self._stop_field
        if isinstance(field, BoundedNumberField):
            field.setValue(field.snap(v))

    def _build_deps_row(self, spec: dict, fold, freq):
        """The 'DEPENDS ON' chip row — the fold frequency (when the range is freq-dependent) and
        each carried bridge knob the range folds through (a chirp's --bw, GPS C/A's --sidelobes),
        resolved to the real knob behind an internal derived quantity. None when nothing re-folds."""
        chips = self._ramp_dep_chips(spec, fold, freq)
        if not chips:
            return None
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 3, 0, 0); h.setSpacing(8)
        h.addWidget(_uc_label("DEPENDS ON", 9, Palette.TEXT_FAINT, 0.8))
        for c in chips:
            chip = QFrame(); chip.setObjectName("rampDepChip")
            # A fully-rounded pill (radius ≈ half height), matching the mockup's .dep.
            chip.setStyleSheet(
                f"#rampDepChip {{ background: {Palette.INSET}; border: 1px solid {Palette.BORDER}; "
                f"border-radius: 13px; }}")
            ch = QHBoxLayout(chip); ch.setContentsMargins(11, 4, 11, 4); ch.setSpacing(6)
            k = QLabel(c["name"])                          # bold, accent-coloured parameter name
            kf = QFont("IBM Plex Sans"); kf.setPixelSize(11); kf.setWeight(QFont.Weight.DemiBold)
            k.setFont(kf); k.setStyleSheet(f"color: {Palette.ACCENT_INK};")
            dv = QLabel(fmt_value(c["value"]) if c["value"] is not None else "—")
            dv.setFont(mono_font(12))                      # sleeker (regular-weight) value text
            dv.setStyleSheet(f"color: {Palette.TEXT};")
            ch.addWidget(k); ch.addWidget(dv)
            if c.get("unit"):
                du = QLabel(c["unit"]); du.setFont(mono_font(10))
                du.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
                ch.addWidget(du)
            h.addWidget(chip)
        h.addStretch(1)
        return row

    def _ramp_dep_chips(self, spec: dict, fold, freq) -> List[dict]:
        """``[{name, value, unit}]`` for the DEPENDS ON row: the fold frequency in MHz (when the
        range moves with it) plus each carried bridge knob the range folds through, read at the
        ramp's operating point (``_op_state``). An internal derived key a law uses (e.g. an
        equivalent-noise bandwidth from --sidelobes) is resolved to its source knob."""
        chips: List[dict] = []
        task = self._task.currentText().strip()
        if fold is not None and getattr(fold, "freq_dependent", False) \
                and isinstance(freq, (int, float)):
            fname = "Frequency"
            fparam = (getattr(self._editor, "_script_cal_freq_params", None) or {}).get(
                self._current_script)
            s = next((x for x in self._all_params if x.get("dest") == fparam), None)
            if s is not None:
                fname = _pretty(s.get("name") or "Frequency")
            chips.append({"name": fname, "value": round(freq / 1e6, 4), "unit": "MHz"})
        keyed = set(fold.keyed_params()) if fold is not None else set()
        view = self._control_view()
        if view is not None and view.get("law") is not None:
            try:
                keyed.update(view["law"].params())
            except (ValueError, TypeError):
                pass
        state = self._op_state(task)
        seen: set = set()
        for pdest in sorted(keyed):
            for src in self._resolve_dep_source(pdest):
                if src in seen:
                    continue
                seen.add(src)
                s = next((x for x in self._all_params if x.get("dest") == src), None)
                if s is None:
                    continue
                chips.append({"name": _pretty(s.get("name") or src),
                              "value": state.get(src), "unit": (s.get("unit") or "").strip()})
        return chips

    def _resolve_dep_source(self, pdest: str) -> List[str]:
        """The real input knob(s) behind a law-keyed dest: the dest itself when it is a visible
        input param, else the source fields of the derived quantity under it (e.g. --sidelobes
        behind an ``enbw_mhz`` table lookup). Falls back to the dest so the row is never empty."""
        own = next((x for x in self._all_params if x.get("dest") == pdest), None)
        if own is not None and own.get("kind") != "derived" and not own.get("hidden"):
            return [pdest]
        srcs: List[str] = []
        if own is not None and own.get("kind") == "derived":
            for key, args in (own.get("formula") or {}).items():
                if key == "labels":
                    continue
                if isinstance(args, (list, tuple)):
                    srcs.extend(str(a) for a in args
                                if not (isinstance(a, (int, float)) and not isinstance(a, bool)))
        real = [s for s in srcs
                if any(x.get("dest") == s and x.get("kind") != "derived" for x in self._all_params)]
        return real or [pdest]

    def _power_disp_decimals(self) -> int:
        """Display decimals for the companion read-outs — the primary field's own device-step
        decimals (a QDoubleSpinBox setDecimals), so companions round like the field. 2 by default."""
        f = self._start_field
        spin = getattr(f, "_spin", None)
        if isinstance(f, BoundedNumberField) and spin is not None and hasattr(spin, "decimals"):
            try:
                return int(spin.decimals())
            except (TypeError, ValueError):
                return 2
        return 2

    def _fmt_pw(self, v) -> str:
        try:
            return f"{float(v):.{self._power_disp_decimals()}f}".replace("-", "−")
        except (TypeError, ValueError):
            return "—"

    def _update_power_readouts(self) -> None:
        """Refresh the span read-out and each companion's From → To from the live From/To values.
        A companion value is the primary value plus the gap between the two views' offsets at the
        carried operating point (base is shared, so displayed = primary + (off_companion − off_sel));
        no-op when the plain rows are showing (no card labels)."""
        if self._span_lbl is None:
            return
        a = self._selected_view() or {}
        unit = (a.get("unit") or self._param_unit() or "").strip()
        sfrom, sto = self._val(self._start_field), self._val(self._stop_field)
        if sfrom is None or sto is None:
            self._span_lbl.setText("enter From / To")
        else:
            d = sto - sfrom
            direction = "rising" if d > 1e-9 else "falling" if d < -1e-9 else "flat"
            sign = "+" if d >= 0 else "−"
            u = f" {unit}" if unit else ""
            self._span_lbl.setText(f"sweeps  {sign}{abs(d):.{self._power_disp_decimals()}f}{u}  "
                                   f"({direction})")
        # FROM/TO sub-labels reflect the live anchor + offset (fire times)
        if self._ft_from_lbl is not None:
            fs, ts = self._ft_sublabels()
            self._ft_from_lbl.setText(f"FROM  ({fs})")
            self._ft_to_lbl.setText(f"TO  ({ts})")
        # the shared dual rail + MIN/MAX labels track the live From/To and the field bounds
        if isinstance(self._start_field, BoundedNumberField):
            lo, hi = self._start_field.bounds()
            if self._pwr_min is not None and lo is not None and hi is not None:
                self._pwr_min.setText(f"MIN  {self._fmt_pw(lo)}{(' ' + unit) if unit else ''}")
                self._pwr_max.setText(f"MAX  {self._fmt_pw(hi)}{(' ' + unit) if unit else ''}")
            if self._pwr_rail is not None:
                if lo is not None and hi is not None:
                    self._pwr_rail.set_bounds(lo, hi)
                self._pwr_rail.set_from(sfrom); self._pwr_rail.set_to(sto)
        sel_off = self._ramp_view_offset()
        task = self._task.currentText().strip()
        for view, from_lbl, to_lbl in self._companion_labels:
            gap = self._view_offset(task, view) - sel_off
            if sfrom is None or sto is None:
                from_lbl.setText("—"); to_lbl.setText("—")
            else:
                from_lbl.setText(self._fmt_pw(sfrom + gap))
                to_lbl.setText(self._fmt_pw(sto + gap))

    def _update_cal_note(self) -> None:
        """Show the offline-calibration notice when the ramped --power range is folded from the
        last-known (cached) calibration because the target unit is offline. Hidden otherwise."""
        if getattr(self, "_cal_note", None) is None:
            return
        stale_getter = getattr(self._editor, "cal_is_stale", None)
        stale = bool(stale_getter()) if callable(stale_getter) else False
        spec = self._ramped_spec()
        is_power = spec is not None and find_power_index([spec]) is not None
        bounds = self._editor.cal_bounds_for_task(self._task.currentText().strip()) \
            if getattr(self._editor, "cal_bounds_for_task", None) else None
        if stale and is_power and bounds:
            host = getattr(self._editor, "_cal_hostname", "") or "the target unit"
            self._cal_note.setText(
                f"⚠ Using the last-known calibration for {host} (offline) — the power range "
                f"refreshes when the unit reconnects.")
            self._cal_note.setVisible(True)
        else:
            self._cal_note.setVisible(False)

    def _update_preview(self, *_) -> None:
        if not self._ready:
            return
        self._update_power_readouts()   # span + companion From→To track the live From/To values
        self._update_cal_note()         # offline "last-known calibration" notice
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

        anchor = {"start": "on-air", "stop": "off-air",
                  "hold": "Hold (resume)"}.get(self._anchor.currentData(), "on-air")
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
            self._steps_view.setText("")
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
            self._steps_view.setText("")
            return
        unit = self._param_unit()
        u = f" {unit}" if unit else ""
        try:
            if both:
                # steps or step fix the value sequence regardless of the window; a
                # hold-only ramp's point count depends on the (scheduled) window.
                if spec.get("steps") is None and spec.get("step") is None:
                    self._steps_view.setText(
                        "Hold-time ramp: the number of points depends on the on-air\n"
                        "window, resolved when the sequence is scheduled.")
                    return
                res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                         step=spec.get("step"), window_s=1.0)
                lines = ["   #   value      (times set by the schedule window)"]
                for i, v in enumerate(res.values):
                    lines.append(f"  {i:>3}   {fmt_value(_clean(v))}{u}")
                self._steps_view.setText("\n".join(lines))
                return
            res = _ramp.resolve_ramp(spec["start"], spec["stop"], steps=spec.get("steps"),
                                     step=spec.get("step"), hold_s=spec.get("hold_s"),
                                     duration_s=spec.get("duration_s"),
                                     include_first=spec.get("include_first", True),
                                     include_last=spec.get("include_last", True))
        except (ValueError, TypeError) as exc:
            self._steps_view.setText(str(exc))
            return
        anchor = self._anchor.currentData()
        fires = _ramp.place_ramp(anchor, float(self._offset.value()), res)
        lines = [f"{'#':>3}  {'time':>9}   value"]
        for i, (fa, foff, v) in enumerate(fires):
            tag = "T0" if fa == "start" else "off"
            lines.append(f"{i:>3}  {tag + _off(foff):>9}   {fmt_value(_clean(v))}{u}")
        self._steps_view.setText("\n".join(lines))

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

        # A step-anchored ramp runs forward from its target's edge; resolve the target (and
        # assign it a stable id) + validate the non-negative offset here.
        step_fields: dict = {}
        if anchor == "step":
            if offset < 0:
                return self._set_preview("offset must be ≥ 0 — a ramp can't start before the "
                                         "step it anchors to", error=True)
            tgt_uid = self._anchor_target.currentData()
            items_getter = getattr(self._editor, "items", None)
            target = None
            if items_getter is not None:
                target = next((it for it in items_getter()
                               if getattr(it, "uid", None) == tgt_uid), None)
            if target is None:
                return self._set_preview("pick a step to anchor to", error=True)
            step_fields = {"anchor_step_id": tlm.ensure_step_id(target),
                           "anchor_edge": self._anchor_edge.currentData() or "end"}

        args: List[str] = []
        if self._run_mode:
            # Fixed values for the other params (the ramped one is injected per point
            # on the unit). No span check — each point is a standalone one-shot.
            ferr = self._form.validate()
            if ferr:
                return self._set_preview(ferr, error=True)
            args = self._form.build_args()
        elif anchor not in ("hold", "step"):
            # A window-B (Hold-anchored) ramp is timed from the resume instant, not against
            # the on-air window, so the on-air-fit check doesn't apply (its target task still
            # needs a duration step — enforced by the sequence-level validate()). Mirrors the
            # step editor skipping the window-fit check for a hold-anchored tune.
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
            args=args, replace_args=True, uid=self._src.uid, power_view=power_view,
            step_id=getattr(self._src, "step_id", "") or "", **step_fields)
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


def _ofield(label: str, widget: QWidget, unit: str = "") -> QFrame:
    """A contained form row matching the mockup's .ofield: an accent-ink label in a fixed left
    column + the control filling the rest (+ an optional faint unit on the right), inside a bordered
    surface-alt box. The control keeps its own identity (only re-parented) and is flattened by the
    dialog's #ofield QSS. The label is exposed as ``._klabel`` (some rows relabel it live)."""
    frame = QFrame(); frame.setObjectName("ofield")
    h = QHBoxLayout(frame); h.setContentsMargins(12, 5, 11, 5); h.setSpacing(10)
    lbl = QLabel(label); lbl.setObjectName("ofieldKey")
    lf = QFont("IBM Plex Sans"); lf.setPixelSize(11); lf.setWeight(QFont.Weight.DemiBold)
    lbl.setFont(lf); lbl.setFixedWidth(140)
    lbl.setStyleSheet(f"color: {Palette.ACCENT_INK};")
    h.addWidget(lbl)
    h.addWidget(widget, 1)
    if unit:
        u = QLabel(unit); u.setFont(mono_font(11))
        u.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        h.addWidget(u)
    frame._klabel = lbl
    return frame


def _uc_label(text: str, px: int, color: str, spacing: float) -> QLabel:
    """A small uppercase eyebrow/label — bold IBM Plex Sans at ``px`` with letter-spacing,
    matching the power card's section headers (RAMP POWER / RAMPING IN / ALSO READS AS / DEPENDS ON)."""
    lbl = QLabel(text)
    f = QFont("IBM Plex Sans"); f.setPixelSize(px); f.setWeight(QFont.Weight.Bold)
    lbl.setFont(f)
    lbl.setStyleSheet(f"color: {color}; letter-spacing: {spacing}px;")
    return lbl


def _pretty(name: str) -> str:
    """A parameter's display name for a chip: strip a leading '--', underscores → spaces, first
    letter capitalised (e.g. ``sidelobes`` → ``Sidelobes``, ``--bw`` → ``Bw``)."""
    s = str(name or "").lstrip("-").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else s
