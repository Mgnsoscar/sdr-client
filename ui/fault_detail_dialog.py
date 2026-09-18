"""
FaultDetailDialog — the self-diagnosis view for an RF fault (docs/rf-fault-recovery.md §6.3).

Opened by double-clicking an RF-fault row in the activity feed. Renders a TaskHealthEvent: the
fault reason, the log tail captured at detection, and — the payoff for the agent's fault snapshot —
the machine + per-task resource state (the pinned GR buffer backend, /dev/shm pressure, the VMA
map count vs its ceiling, the file-descriptor limit), with the suspects for a `vmcircbuf` failure
called out. Read-only, no network — everything comes off the event the SSE stream already carried.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from api import models as m
from .theme import Palette, mono_font
from .widgets import fit_dialog_to_screen

# The backend that self-cleans its shared memory; a SysV fallback is the leaky suspect (§6.3).
_SAFE_BACKEND = "mmap_shm_open"


def _fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "—"
    step = 1024.0
    val = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < step or unit == "TiB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{n} B"


def _pct(used: Optional[int], total: Optional[int]) -> Optional[float]:
    if not used or not total or total <= 0:
        return None
    return 100.0 * used / total


class _Row:
    """One diagnosis line: (label, value, suspect?, hint)."""
    __slots__ = ("label", "value", "suspect", "hint")

    def __init__(self, label, value, suspect=False, hint=""):
        self.label, self.value, self.suspect, self.hint = label, value, suspect, hint


def _diagnosis_rows(snap: m.FaultSnapshot) -> list:
    """Turn a FaultSnapshot into display rows, flagging the vmcircbuf suspects. Pure + tested."""
    rows: list = []

    env = snap.vmcircbuf_backend_env or ""
    compiled = snap.vmcircbuf_backend_compiled or ""
    backend = env or compiled or "—"
    # The EFFECTIVE backend is the env pin when set, else the COMPILED default — GR falls back to the
    # compiled default when no GR_CONF_* env is pinned (e.g. when the P0 pin knob is disabled). A
    # non-mmap effective backend is the leaky-fallback suspect whichever supplies it, so a sysv_shm
    # COMPILED default with no env pin is flagged too (env-only would miss it, in exactly the config
    # where sysv is active). A mismatch env≠compiled is named below even when both are mmap.
    effective = env or compiled
    backend_suspect = bool(effective) and effective != _SAFE_BACKEND
    backend_val = backend
    if env and compiled and env != compiled:
        backend_val = f"{env}  (compiled default: {compiled})"
    rows.append(_Row("GR buffer backend", backend_val, backend_suspect,
                     "a SysV-shm fallback leaks segments on SIGKILL — pin mmap_shm_open"
                     if backend_suspect else ""))

    shm_pct = _pct(snap.shm_used_bytes, snap.shm_total_bytes)
    if snap.shm_total_bytes is not None:
        shm_txt = f"{_fmt_bytes(snap.shm_used_bytes)} / {_fmt_bytes(snap.shm_total_bytes)}"
        if shm_pct is not None:
            shm_txt += f"  ({shm_pct:.0f}%)"
        rows.append(_Row("/dev/shm", shm_txt, shm_pct is not None and shm_pct >= 90.0,
                         "shared memory nearly full — a vmcircbuf allocation can fail here"))

    if snap.map_count is not None or snap.map_max is not None:
        mc, mx = snap.map_count, snap.map_max
        map_txt = f"{mc if mc is not None else '—'} / {mx if mx is not None else '—'}"
        map_pct = _pct(mc, mx)
        if map_pct is not None:
            map_txt += f"  ({map_pct:.0f}%)"
        rows.append(_Row("VMA maps (vm.max_map_count)", map_txt,
                         map_pct is not None and map_pct >= 80.0,
                         "near the mmap ceiling — raise vm.max_map_count"))

    if snap.rss_bytes is not None:
        rows.append(_Row("RSS", _fmt_bytes(snap.rss_bytes)))
    if snap.nofile_soft is not None or snap.nofile_hard is not None:
        rows.append(_Row("Open-file limit (soft/hard)",
                         f"{snap.nofile_soft if snap.nofile_soft is not None else '—'}"
                         f" / {snap.nofile_hard if snap.nofile_hard is not None else '—'}"))
    if snap.task_home:
        rows.append(_Row("Task HOME", snap.task_home))
    if snap.ipcs_summary:
        rows.append(_Row("SysV IPC", snap.ipcs_summary, True,
                         "leftover SysV segments implicate a SysV-shm backend"))
    for note in (snap.notes or []):
        rows.append(_Row("Note", note))
    return rows


class FaultDetailDialog(QDialog):
    """Read-only diagnosis for one RF-fault TaskHealthEvent."""

    def __init__(self, ev: m.TaskHealthEvent, parent=None):
        super().__init__(parent)
        self.ev = ev
        self.setWindowTitle(f"RF fault — {getattr(ev, 'task_name', '?')}")
        self._build()
        fit_dialog_to_screen(self, 640, 620)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        unit = getattr(self.ev, "unit_id", "?")
        task = getattr(self.ev, "task_name", "?")
        detail = getattr(self.ev, "detail", "") or "the flowgraph halted — the radio went silent"
        at = getattr(self.ev, "at", "")

        # ── Red banner ─────────────────────────────────────────────────────────
        banner = QFrame()
        banner.setObjectName("card")
        banner.setStyleSheet(
            f"#card {{ background: {Palette.CRASH_SOFT}; border: 1px solid {Palette.CRASH}; }}")
        bl = QVBoxLayout(banner)
        bl.setContentsMargins(12, 10, 12, 10)
        bl.setSpacing(2)
        head = QLabel(f"⚠  RF FAULT — radio silent")
        head.setStyleSheet(f"color: {Palette.CRASH}; font-size: 15px; font-weight: 700;")
        bl.addWidget(head)
        who = QLabel(f"{unit} · {task}")
        who.setStyleSheet(f"color: {Palette.TEXT}; font-size: 13px; font-weight: 600;")
        bl.addWidget(who)
        why = QLabel(detail)
        why.setWordWrap(True)
        why.setStyleSheet(f"color: {Palette.TEXT_MUTED}; font-size: 12px;")
        bl.addWidget(why)
        sub = QLabel(("detected " + at if at else "detected") +
                     " — RF was auto-dropped; the task's log is archived on the unit.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px;")
        bl.addWidget(sub)
        outer.addWidget(banner)

        # ── Diagnosis grid (from the snapshot) ───────────────────────────────────
        snap = getattr(self.ev, "snapshot", None)
        if isinstance(snap, m.FaultSnapshot):
            outer.addWidget(self._section_label("DIAGNOSIS"))
            outer.addWidget(self._diagnosis_widget(_diagnosis_rows(snap)))
        else:
            note = QLabel("No resource snapshot was captured for this fault.")
            note.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 12px;")
            outer.addWidget(note)

        # ── Log tail at detection ─────────────────────────────────────────────────
        lines = list(getattr(self.ev, "last_log_lines", []) or [])
        if lines:
            outer.addWidget(self._section_label("LOG AT FAULT"))
            log = QPlainTextEdit()
            log.setReadOnly(True)
            log.setFont(mono_font(12))
            log.setPlainText("\n".join(lines))
            log.setStyleSheet(
                f"background: {Palette.SURFACE}; color: {Palette.TEXT}; "
                f"border: 1px solid {Palette.BORDER}; border-radius: 6px;")
            outer.addWidget(log, 1)

        # ── Footer ────────────────────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        outer.addLayout(footer)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {Palette.TEXT_MUTED}; font-size: 11px; font-weight: 700; "
            "letter-spacing: 0.5px;")
        return lbl

    def _diagnosis_widget(self, rows: list) -> QWidget:
        w = QFrame()
        w.setObjectName("card")
        grid = QGridLayout(w)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        for r, row in enumerate(rows):
            name = QLabel(row.label)
            name.setStyleSheet(f"color: {Palette.TEXT_MUTED}; font-size: 12px;")
            grid.addWidget(name, r, 0, alignment=Qt.AlignmentFlag.AlignTop)

            vbox = QVBoxLayout()
            vbox.setSpacing(1)
            val = QLabel(str(row.value))
            val.setWordWrap(True)
            colour = Palette.CRASH if row.suspect else Palette.TEXT
            weight = "700" if row.suspect else "500"
            val.setFont(mono_font(12))
            val.setStyleSheet(f"color: {colour}; font-weight: {weight};")
            vbox.addWidget(val)
            if row.suspect and row.hint:
                hint = QLabel("↳ " + row.hint)
                hint.setWordWrap(True)
                hint.setStyleSheet(f"color: {Palette.CRASH}; font-size: 11px;")
                vbox.addWidget(hint)
            holder = QWidget()
            holder.setLayout(vbox)
            grid.addWidget(holder, r, 1)
        return w
