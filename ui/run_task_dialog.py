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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from api import models as m

from .param_form import (
    ParamForm, power_mode_of_args, find_power_index, find_gain_index,
    _POWER_FLAGS, _GAIN_FLAGS,
)
from .dialog_style import scrollbar_qss
from .qt_adapter import DataHub
from .theme import Palette, mono_font


# The task env key that carries the uncalibrated stop-gap gain (a script uses it only
# while the unit is uncalibrated for the signal; see the Pi scripts' power precedence).
FALLBACK_GAIN_ENV = "SDR_CAL_FALLBACK_GAIN"


# Card chrome for the dialog — header/body/footer as one elevated surface, matching the
# Run-task panel design. The parameter form styles its own inputs (ui.param_form).
_DIALOG_QSS = f"""
#runCard {{
    background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER}; border-radius: 14px;
}}
#cardHead {{
    background: {Palette.SURFACE_ALT}; border-bottom: 1px solid {Palette.BORDER};
    border-top-left-radius: 14px; border-top-right-radius: 14px;
}}
#cardFoot {{
    background: {Palette.SURFACE_ALT}; border-top: 1px solid {Palette.BORDER};
    border-bottom-left-radius: 14px; border-bottom-right-radius: 14px;
}}
#glyph {{
    background: {Palette.ACCENT_SOFT}; color: {Palette.ACCENT_INK};
    border-radius: 10px; font-size: 16px;
}}
#cardFoot QPushButton {{
    background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 10px; padding: 9px 16px; font-weight: 600; color: {Palette.TEXT};
}}
#cardFoot QPushButton:hover {{ background: {Palette.SURFACE_ALT}; }}
#cardFoot QPushButton#startBtn {{
    background: {Palette.ACCENT}; border: 1px solid {Palette.ACCENT}; color: #FFFFFF;
}}
#cardFoot QPushButton#startBtn:hover {{
    background: {Palette.ACCENT_INK}; border-color: {Palette.ACCENT_INK};
}}
{scrollbar_qss()}
"""


def _fmt_gain(v: float) -> str:
    return f"{v:g}"


def _has_flag(args: List[str], flags) -> bool:
    return any(a in flags for a in args)


def _without_flag(args: List[str], flags) -> List[str]:
    """Drop each occurrence of a flag AND the value token after it."""
    out: List[str] = []
    i = 0
    while i < len(args):
        if args[i] in flags:
            i += 2                                   # skip the flag and its value
            continue
        out.append(args[i])
        i += 1
    return out


class RunTaskDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, task_name: str,
                 running: bool = False, parent=None, quick: bool = False):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self.task_name = task_name
        self._running = running
        # Quick mode: the dialog is driven headlessly by the unit's play button. It loads
        # the task exactly like the visible dialog, but instead of showing the form it
        # decides on its own — an uncalibrated absolute-power task fires the relative-gain
        # prompt (and persists it), everything else starts from the stored command.
        self._quick = quick

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
        self._script_cal_signal: Optional[str] = None   # the SCRIPT's declared signal
        self._script_cal_freq_param: Optional[str] = None  # CAL_FREQ_PARAM: the freq field
        self._cal_bounds = None
        self._task_entry: Optional[dict] = None          # full tasks.yaml entry, for persistence
        self._fallback_gain: Optional[str] = None         # persisted uncalibrated stop-gap gain
        self._params_ready = False
        self._cal_ready = False

        self.setWindowTitle(f"Run '{task_name}' with parameters")
        self.setMinimumWidth(560)
        self._build()
        # Open with generous vertical room — the parameter list is usually several
        # fields tall, and opening compact meant resizing it by hand every time.
        self.resize(620, 760)
        self.hub.task_done.connect(self._on_task_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._load()

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setStyleSheet(_DIALOG_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(0)

        card = QFrame(); card.setObjectName("runCard")
        cardlay = QVBoxLayout(card)
        cardlay.setContentsMargins(0, 0, 0, 0)
        cardlay.setSpacing(0)

        cardlay.addWidget(self._build_header())

        # The parameter form (scrolls when a script has many arguments).
        self._form = ParamForm()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._form)
        scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            + scrollbar_qss())
        scroll.setMinimumHeight(300)
        body = QWidget(); bodylay = QVBoxLayout(body)
        bodylay.setContentsMargins(18, 18, 18, 6)
        bodylay.addWidget(scroll, stretch=1)
        cardlay.addWidget(body, stretch=1)

        # Collapsible "Additional args" section (hidden until the footer button, or a
        # prefill the form didn't recognise, reveals it) — Additional args + command.
        self._extra_section = self._build_extra_section()
        self._extra_section.setVisible(False)
        cardlay.addWidget(self._extra_section)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setContentsMargins(18, 0, 18, 0)
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        cardlay.addWidget(self._status)

        cardlay.addWidget(self._build_footer())
        root.addWidget(card)

        self._form.changed.connect(self._update_preview)
        if self._running:
            self._set_status(
                "This task is already running — stop it first, or starting will "
                "fail.", error=True)

    def _build_header(self) -> QFrame:
        head = QFrame(); head.setObjectName("cardHead")
        lay = QHBoxLayout(head)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        glyph = QLabel("🎛️"); glyph.setObjectName("glyph")
        glyph.setFixedSize(34, 34)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(glyph)

        titles = QVBoxLayout(); titles.setContentsMargins(0, 0, 0, 0); titles.setSpacing(1)
        self._head_title = QLabel(f"Run · {self.task_name}")
        self._head_title.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {Palette.TEXT};")
        self._head_sub = QLabel(self.hostname)
        self._head_sub.setFont(mono_font(11))
        self._head_sub.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        titles.addWidget(self._head_title)
        titles.addWidget(self._head_sub)
        lay.addLayout(titles, stretch=1)

        self._cal_pill = QLabel("")
        self._cal_pill.setVisible(False)
        lay.addWidget(self._cal_pill)
        return head

    def _build_extra_section(self) -> QWidget:
        sec = QWidget()
        lay = QVBoxLayout(sec)
        lay.setContentsMargins(18, 4, 18, 10)
        lay.setSpacing(6)
        cap = QLabel("ADDITIONAL ARGS")
        cap.setStyleSheet(
            f"font-size: 10px; font-weight: 600; letter-spacing: 0.7px; color: {Palette.TEXT_MUTED};")
        lay.addWidget(cap)
        self._extra = QLineEdit()
        self._extra.setPlaceholderText("--flag value  (anything not in the form above)")
        self._extra.setFont(mono_font(13))
        self._extra.setStyleSheet(
            f"background: {Palette.INSET}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 10px; min-height: 38px; padding: 0 12px;")
        self._extra.textChanged.connect(self._update_preview)
        lay.addWidget(self._extra)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setFixedHeight(52)
        self._preview.setFont(mono_font(12))
        self._preview.setStyleSheet(
            f"background: {Palette.INSET}; border: 1px solid {Palette.BORDER}; "
            f"border-radius: 10px; color: {Palette.TEXT_MUTED}; padding: 6px 10px;")
        lay.addWidget(self._preview)
        return sec

    def _build_footer(self) -> QFrame:
        foot = QFrame(); foot.setObjectName("cardFoot")
        lay = QHBoxLayout(foot)
        lay.setContentsMargins(15, 14, 15, 14)
        lay.setSpacing(10)
        self._extra_btn = QPushButton("Additional args…")
        self._extra_btn.setAutoDefault(False)    # don't let Enter in a field open this
        self._extra_btn.clicked.connect(self._toggle_extra)
        lay.addWidget(self._extra_btn)
        lay.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        lay.addWidget(cancel)
        self._run_btn = QPushButton("Start task")
        self._run_btn.setObjectName("startBtn")
        self._run_btn.setAutoDefault(True)       # Enter in any field runs the task
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self._on_run)
        lay.addWidget(self._run_btn)
        # Kept as the handle _on_run/_on_task_done enable/disable during a start.
        self._buttons = foot
        return foot

    def _toggle_extra(self) -> None:
        self._extra_section.setVisible(not self._extra_section.isVisible())

    def _reveal_extra(self) -> None:
        self._extra_section.setVisible(True)

    def _set_pill(self, text: str, kind: str) -> None:
        """Show the calibration state pill in the header (kind: 'ok'/'warn'/'idle')."""
        fg, bg = {
            "ok": (Palette.ONLINE, Palette.ONLINE_SOFT),
            "warn": (Palette.ARMED, Palette.ARMED_SOFT),
            "idle": (Palette.IDLE, Palette.IDLE_SOFT),
        }.get(kind, (Palette.IDLE, Palette.IDLE_SOFT))
        self._cal_pill.setText("● " + text)
        self._cal_pill.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: 999px; "
            f"padding: 3px 10px; font-size: 11px; font-weight: 600;")
        self._cal_pill.setVisible(True)

    def _refresh_header(self) -> None:
        """Fill the header subtitle (host · script) and the calibration pill once the
        task's script and calibration state are known."""
        if self._script_name:
            self._head_sub.setText(f"{self.hostname} · {self._script_name}")
        if self._cal_bounds is not None:
            self._set_pill("Calibrated", "ok")
        elif self._cal_signal_id:
            self._set_pill("Uncalibrated", "warn")
        else:
            self._cal_pill.setVisible(False)

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
            if self._quick:
                self._quick_plain_start()                # couldn't load state — start as-is
            return

        if op == "runtask_yaml":
            self._parse_command(result if isinstance(result, str) else "")
        elif op == "runtask_params":
            self._params_inflight = False
            self._params_ready = True
            self._param_specs = (result or {}).get("params", [])
            self._script_cal_signal = (result or {}).get("calibration_signal")
            self._script_cal_freq_param = (result or {}).get("calibration_freq_param")
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
            if self._quick:
                self._quick_plain_start()                # can't inspect it — start as-is
            return
        self._task_entry = dict(entry)                   # kept for persisting a gain default

        # The task opts into calibration by setting this env to the script's signal id.
        self._cal_signal_id = (entry.get("env") or {}).get("SDR_CAL_SIGNAL_ID")
        self._fallback_gain = (entry.get("env") or {}).get(FALLBACK_GAIN_ENV)

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
            self._reveal_extra()
            if self._quick:
                self._quick_plain_start()                # no calibration form — start as-is
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
        # Open in the mode the deployed command used (absolute if it set --power, relative
        # if --gain) so it's preserved rather than snapping to the form's default.
        mode = power_mode_of_args(self._current_args)
        from .param_form import calibration_caution
        caution = calibration_caution(
            bool(self._cal_signal_id), targeted=True,
            calibrated=self._cal_bounds is not None,
            script_calibratable=bool(self._script_cal_signal or self._cal_signal_id))
        self._form.set_params(self._param_specs, cal_bounds=self._cal_bounds,
                              absolute_allowed=True, default_power_mode=mode,
                              caution=caution, cal_freq_param=self._script_cal_freq_param)
        # Prefill from the deployed args; anything the form doesn't recognise
        # (positional args, flags not in the schema) drops into "Additional args".
        extra = self._form.set_values(self._current_args)
        if self._uncalibrated_absolute():
            # An authored absolute --power is meaningless on an uncalibrated unit and the
            # form has no --power field here, so set_values returns it as an "extra". Drop
            # it rather than surfacing a stale, un-runnable value in Additional args.
            extra = _without_flag(extra, _POWER_FLAGS)
            # Pre-fill the relative gain from a previously persisted fallback, so a task
            # that already has one runs (and quick-plays) without re-prompting.
            if self._fallback_gain and not _has_flag(self._current_args, _GAIN_FLAGS):
                self._form.set_values([_GAIN_FLAGS[0], self._fallback_gain])
        if extra:
            self._extra.setText(" ".join(shlex.quote(e) for e in extra))
            if not self._quick:
                self._reveal_extra()
        self._refresh_header()
        if self._quick:
            self._quick_dispatch()

    def _quick_dispatch(self) -> None:
        """Headless decision for the unit's play button. An uncalibrated absolute-power task
        needs a relative gain, so run the same first-Start flow as the visible dialog (prompt +
        persist); a cancelled prompt closes without starting. A calibrated task whose stored
        --power is out of the unit's range is clamped to the limit here (the client handles it,
        so the script never receives a level it would just clip). Anything else starts from the
        task's stored command/env untouched."""
        if self._uncalibrated_absolute():
            self._on_run()
            if not self._starting:
                self.reject()                            # gain prompt cancelled — don't start
            return
        clamp = self._clamp_absolute_power()
        if clamp is not None:
            args, value = clamp
            self._persist_clamped_power(value)           # heal the stored config to the limit
            self._quick_start_args(args)                 # run at the limit, not the script's clip
        else:
            self._quick_plain_start()

    def _clamp_absolute_power(self):
        """If the unit is calibrated for this signal and the stored --power is outside the
        resolved range, return (clamped_trailing_args, clamped_dbm); else None. Clamping here
        keeps an out-of-range level (e.g. after a recalibration) from reaching the script,
        which would otherwise transmit clipped to its limit with no UI feedback."""
        if self._cal_bounds is None or find_power_index(self._param_specs) is None:
            return None
        lo = self._cal_bounds.get("min_power_dbm")
        hi = self._cal_bounds.get("max_power_dbm")
        args = list(self._current_args)
        clamped = None
        for i, a in enumerate(args):
            if a in _POWER_FLAGS and i + 1 < len(args):
                try:
                    v = float(args[i + 1])
                except (TypeError, ValueError):
                    continue
                nv = v
                if hi is not None and v > float(hi):
                    nv = float(hi)
                elif lo is not None and v < float(lo):
                    nv = float(lo)
                if nv != v:
                    args[i + 1] = f"{nv:g}"
                    clamped = nv
        return (args, clamped) if clamped is not None else None

    def _persist_clamped_power(self, value: float) -> None:
        """Rewrite the stored task command's --power to ``value`` (the unit's limit) and
        persist it, so the deployed task no longer shows/uses a level the unit can't produce."""
        if self._task_entry is None:
            return
        entry = dict(self._task_entry)
        cmd = list(entry.get("command") or [])
        for i, a in enumerate(cmd):
            if a in _POWER_FLAGS and i + 1 < len(cmd):
                cmd[i + 1] = f"{value:g}"
        entry["command"] = cmd
        self.hub.run_async(
            f"runtask_persist:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).update_task(self.task_name, entry),
        )

    def _quick_start_args(self, args: List[str]) -> None:
        """Start with the given trailing args (replace_args), used by quick-start to run a
        clamped --power instead of the stored over-range one."""
        if self._starting:
            return
        self._starting = True
        self._set_status("starting…")
        req = m.StartRequest(args=args, replace_args=True)
        self.hub.run_async(
            f"runtask_start:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).start_task(self.task_name, req),
        )

    def _quick_plain_start(self) -> None:
        """Start the task from its stored command/env (no arg replacement), exactly like the
        old play button. Routed through the dialog only so quick mode shares one code path."""
        if self._starting:
            return
        self._starting = True
        self._set_status("starting…")
        self.hub.run_async(
            f"runtask_start:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).start_task(self.task_name),
        )

    def _uncalibrated_absolute(self) -> bool:
        """True when this task opts into calibration, the unit has NO resolved bounds for
        the signal (so absolute --power is meaningless here), and the script exposes BOTH a
        --power and a relative --gain. In that state --power is never sent and a relative
        --gain is required. A script with only --power (no relative fallback) keeps showing
        the power field, so it's excluded here."""
        return (bool(self._cal_signal_id) and self._cal_bounds is None
                and find_power_index(self._param_specs) is not None
                and find_gain_index(self._param_specs) is not None)
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
        # On an uncalibrated unit, absolute --power can't be honoured. If the operator
        # hasn't set a relative --gain, explain and capture one before running (and before
        # the form's required-gain check fires) — rather than starting on a meaningless dBm.
        prompted_gain = None
        if self._uncalibrated_absolute() and not _has_flag(self._override_args(), _GAIN_FLAGS):
            gain = self._prompt_relative_gain()
            if gain is None:
                return                                   # cancelled — don't start
            self._form.set_values([_GAIN_FLAGS[0], _fmt_gain(gain)])
            self._update_preview()
            prompted_gain = gain
        err = self._form.validate()
        if err:
            self._set_status(err, error=True)
            return
        args = self._override_args()
        # Persist the stop-gap gain as the task's uncalibrated FALLBACK (env), NOT by
        # overwriting --power — the command keeps its authored absolute power, so quick-play
        # and sequences use the fallback gain while uncalibrated and auto-revert to the dBm
        # value the moment the unit is calibrated for the signal.
        if prompted_gain is not None and self._task_entry is not None:
            self._persist_fallback_gain(prompted_gain)
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

    def _prompt_relative_gain(self) -> Optional[float]:
        """Modal: explain the unit is uncalibrated for this signal and capture a relative
        TX gain (dB). Returns the value, or None if cancelled. Bounds come from the --gain
        param's schema."""
        gi = find_gain_index(self._param_specs)
        spec = self._param_specs[gi] if gi is not None else {}
        lo = float(spec.get("min", 0.0) or 0.0)
        hi = float(spec.get("max", 89.75) or 89.75)
        default = spec.get("default")
        start = float(default) if default is not None else lo
        val, ok = QInputDialog.getDouble(
            self, "Set a relative gain to run",
            (f"This unit isn't calibrated for '{self._cal_signal_id}', so the absolute "
             f"power this task was authored with can't be converted to a real level.\n\n"
             f"Set a relative TX gain (raw dB) to run it here. It becomes this unit's "
             f"fallback gain for the task — used every time you Start it or run it in a "
             f"sequence, while uncalibrated.\n\n"
             f"The task keeps its Library absolute power: once you calibrate this unit for "
             f"'{self._cal_signal_id}', it automatically reverts to that dBm value."),
            start, lo, hi, 2)
        return float(val) if ok else None

    def _persist_fallback_gain(self, gain: float) -> None:
        """Persist the stop-gap gain as the task's SDR_CAL_FALLBACK_GAIN env, keeping the
        command (and its authored --power) intact. The script uses this fallback only while
        uncalibrated; calibrating the unit auto-reverts to the absolute --power."""
        entry = dict(self._task_entry or {})
        env = dict(entry.get("env") or {})
        env[FALLBACK_GAIN_ENV] = _fmt_gain(gain)
        entry["env"] = env
        self.hub.run_async(
            f"runtask_persist:{self.hostname}",
            lambda: self.hub.fleet.get(self.hostname).update_task(self.task_name, entry),
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
