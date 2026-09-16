"""
SyncedClockWidget — the always-visible, internet-synchronized clock in the top bar.

Shows the local time of day (HH:MM:SS, mono) with the zone, a UTC read-out underneath (the
units and every run timestamp are UTC), and a source chip: green "NTP ✓ ±N ms" when the time
comes from an internet time server, amber "PC clock" when none is reachable (the widget then
shows this PC's own clock and keeps retrying), muted "syncing…" before the first answer.

The NTP exchange runs on a daemon thread (`_NtpSyncer`) and hands its result back over Qt
signals (emitted from the worker thread → queued to the GUI thread), so the UI never blocks
on the network. Re-sync every `RESYNC_S` after a success, every `RETRY_S` after a failure.
The time SOURCE is `state.ntp_clock.SyncedClock` (pure, tested); this widget only renders it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

_ZERO = timedelta(0)

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from state import ntp_clock
from state.ntp_clock import NtpSample, SyncedClock
from .theme import Fonts, Palette

RESYNC_S = 600          # after a good sample
RETRY_S = 60            # after a failure
TICK_MS = 200           # display refresh (sub-second so the seconds flip on time)


class _NtpSyncer(QObject):
    """Runs `ntp_clock.sync()` off-thread; one exchange at a time."""
    synced = pyqtSignal(object)      # NtpSample
    failed = pyqtSignal(str)

    def __init__(self, parent=None, sync_fn=ntp_clock.sync):
        super().__init__(parent)
        self._sync_fn = sync_fn
        self._busy = False

    def request(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        threading.Thread(target=self._run, name="ntp-sync", daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            sample = self._sync_fn()
        except Exception as exc:  # noqa: BLE001 — every failure is a fallback, never a crash
            self._busy = False
            self.failed.emit(str(exc))
            return
        self._busy = False
        self.synced.emit(sample)


class SyncedClockWidget(QWidget):
    """The top-bar clock. `autostart=False` keeps the network + tick timers off (tests)."""

    def __init__(self, parent=None, *, autostart: bool = True, clock: SyncedClock | None = None,
                 sync_fn=ntp_clock.sync):
        super().__init__(parent)
        self.clock = clock or SyncedClock()
        self._syncer = _NtpSyncer(self, sync_fn=sync_fn)
        self._syncer.synced.connect(self._apply_sample)
        self._syncer.failed.connect(self._apply_failure)
        self._build()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self.refresh)
        self._resync = QTimer(self)
        self._resync.setSingleShot(True)
        self._resync.timeout.connect(self.sync_now)
        self.refresh()
        if autostart:
            self.start()

    # ── build ──
    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setContentsMargins(0, 0, 0, 0)
        self._time_lbl = QLabel("--:--:--")
        self._time_lbl.setStyleSheet(
            f"font-family: {Fonts.MONO}; font-size: 19px; font-weight: 600; color: {Palette.TEXT};"
            " letter-spacing: 1px;")
        self._time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sub_lbl = QLabel("UTC --:--:--")
        self._sub_lbl.setStyleSheet(
            f"font-family: {Fonts.MONO}; font-size: 10.5px; color: {Palette.TEXT_FAINT};")
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        col.addWidget(self._time_lbl)
        col.addWidget(self._sub_lbl)
        lay.addLayout(col)
        self._chip = QLabel("syncing…")
        self._chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chip.mousePressEvent = lambda _e: self.sync_now()     # a click re-syncs now
        lay.addWidget(self._chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self._style_chip("pending")

    def _style_chip(self, kind: str) -> None:
        ink, soft = {
            "ntp": (Palette.ONLINE, Palette.ONLINE_SOFT),
            "local": (Palette.ARMED, Palette.ARMED_SOFT),
        }.get(kind, (Palette.TEXT_MUTED, Palette.SURFACE_ALT))
        self._chip.setStyleSheet(
            f"QLabel {{ color: {ink}; background: {soft}; border: 1px solid {ink}; border-radius: 9px;"
            f" padding: 2px 8px; font-size: 10.5px; font-weight: 700; }}")

    # ── lifecycle ──
    def start(self) -> None:
        self._tick.start(TICK_MS)
        self.sync_now()

    def stop(self) -> None:
        self._tick.stop()
        self._resync.stop()

    def sync_now(self) -> None:
        """Kick an NTP exchange (no-op while one is in flight)."""
        self._syncer.request()

    # ── results (GUI thread) ──
    def _apply_sample(self, sample: NtpSample) -> None:
        self.clock.apply(sample)
        self._resync.start(RESYNC_S * 1000)
        self.refresh()

    def _apply_failure(self, error: str) -> None:
        self.clock.fail(error)
        self._resync.start(RETRY_S * 1000)
        self.refresh()

    # ── render ──
    def refresh(self) -> None:
        now = self.clock.now()
        local = datetime.fromtimestamp(now).astimezone()
        utc = datetime.fromtimestamp(now, tz=timezone.utc)
        self._time_lbl.setText(local.strftime("%H:%M:%S"))
        zone = local.tzname() or "local"
        date = local.strftime("%a %d %b")
        # Show the UTC read-out only when the PC's own zone isn't already UTC (units + run
        # timestamps are UTC, so it's worth having side by side — but not doubled up).
        if (local.utcoffset() or _ZERO) == _ZERO:
            self._sub_lbl.setText(f"UTC · {date}")
        else:
            self._sub_lbl.setText(f"{zone} · UTC {utc.strftime('%H:%M:%S')} · {date}")
        self._chip.setText(self.clock.status_text())
        self._style_chip(self.clock.source if self.clock.attempts or self.clock.sample else "pending")
        self.setToolTip(self.clock.describe() + "\nClick the chip to re-sync now.")

    # ── test hooks ──
    def time_text(self) -> str:
        return self._time_lbl.text()

    def chip_text(self) -> str:
        return self._chip.text()
