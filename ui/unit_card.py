"""
UnitCard — a compact card in the Units dashboard grid.

Shows at a glance: unit name, a connection status dot, CPU temperature, and a few
key stats (clock sync, SDR, running/crashed task counts). Clicking it requests a
drill-in to that unit's detail view (the Units tab handles the navigation).

Cards are updated from poller snapshots (fast = health/system/tasks, slow = sdr)
and from live SSE stream-status. Each card holds its own latest state and repaints
when any source updates.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout

from api import models as m
from .theme import Palette


class _Dot(QLabel):
    """A small colored status dot."""
    def __init__(self, color: str = Palette.IDLE):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background: {color}; border-radius: 6px;"
        )


class UnitCard(QFrame):
    """Compact, clickable card for one unit."""

    clicked = pyqtSignal(str)   # emits hostname (stable key) when clicked

    def __init__(self, hostname: str, display_name: str | None = None, parent=None):
        super().__init__(parent)
        self.hostname = hostname
        self.display_name = display_name or hostname
        self.setObjectName("card")
        self.setFixedSize(240, 132)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._online: Optional[bool] = None
        self._build()

    def is_online(self) -> bool:
        """True only if the latest connection/stream update said online."""
        return self._online is True

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        # ── Header: dot + name ────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)
        self._dot = _Dot(Palette.IDLE)
        header.addWidget(self._dot)

        self._name = QLabel(self.display_name)
        self._name.setStyleSheet(
            f"font-size: 15px; font-weight: 600; color: {Palette.TEXT};"
        )
        header.addWidget(self._name)
        header.addStretch(1)

        self._conn = QLabel("—")
        self._conn.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        header.addWidget(self._conn)
        outer.addLayout(header)

        # ── Key stats grid ────────────────────────────────────────────────────
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)

        self._temp = self._stat(grid, 0, "temp")
        self._clock = self._stat(grid, 1, "clock")
        self._sdr = self._stat(grid, 2, "sdr")
        self._tasks = self._stat(grid, 3, "tasks")

        outer.addLayout(grid)
        outer.addStretch(1)

    def _stat(self, grid: QGridLayout, row: int, label: str) -> QLabel:
        lbl = QLabel(label)
        lbl.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        val = QLabel("—")
        val.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(lbl, row, 0)
        grid.addWidget(val, row, 1)
        return val

    # ── Click → drill in ────────────────────────────────────────────────────

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.hostname)
        super().mousePressEvent(event)

    # ── Updates from data sources ────────────────────────────────────────────

    def set_connection(self, connected: Optional[bool]) -> None:
        """Connection/stream status: True online, False offline, None unknown."""
        self._online = connected
        if connected is True:
            self._dot.set_color(Palette.ONLINE)
            self._conn.setText("online")
        elif connected is False:
            self._dot.set_color(Palette.CRASH)
            self._conn.setText("offline")
        else:
            self._dot.set_color(Palette.IDLE)
            self._conn.setText("—")

    def update_system(self, sys: m.SystemHealth) -> None:
        # Temperature, with color hint if hot
        if sys.cpu_temp_c is not None:
            temp = f"{sys.cpu_temp_c:.0f}°C"
            color = Palette.CRASH if sys.cpu_temp_c >= 75 else (
                Palette.ARMED if sys.cpu_temp_c >= 65 else Palette.TEXT_MUTED
            )
            self._temp.setText(temp)
            self._temp.setStyleSheet(f"font-size: 12px; color: {color};")
        else:
            self._temp.setText("—")

        # Clock sync — distinguish real NTP/internet time from a hand-set PC-clock
        # sync (the agent reports clock_source="manual" for the latter). On a direct
        # no-internet cable, "PC clock" is the expected healthy state, so it's not an
        # error; only a clock that's neither disciplined nor hand-set is "unsynced".
        source = (sys.clock_source or "").lower()
        if sys.clock_synced is True:
            self._clock.setText("internet time")
            self._clock.setStyleSheet(f"font-size: 12px; color: {Palette.ONLINE};")
            self._clock.setToolTip(
                f"NTP-synchronized{f' via {sys.clock_source}' if sys.clock_source else ''}")
        elif source == "manual":
            self._clock.setText("PC clock")
            self._clock.setStyleSheet(f"font-size: 12px; color: {Palette.ONLINE};")
            self._clock.setToolTip(
                "Set to the PC clock (not NTP-disciplined). Will switch to internet "
                "time automatically once this unit is back online.")
        elif sys.clock_synced is False:
            self._clock.setText("unsynced")
            self._clock.setStyleSheet(f"font-size: 12px; color: {Palette.CRASH};")
            self._clock.setToolTip("Clock is not NTP-synced and hasn't been set to "
                                   "the PC clock — sync it from the status bar.")
        else:
            self._clock.setText("—")
            self._clock.setToolTip("")

        # Connection implied online if we got a snapshot
        self.set_connection(True)

    def update_sdr(self, sdr: m.SdrStatus) -> None:
        if sdr.detected and sdr.device_count > 0:
            # Show product if available, else just "detected"
            name = sdr.devices[0].product or sdr.devices[0].name or "detected"
            self._sdr.setText(name)
            self._sdr.setStyleSheet(f"font-size: 12px; color: {Palette.ONLINE};")
        else:
            self._sdr.setText("none")
            self._sdr.setStyleSheet(f"font-size: 12px; color: {Palette.CRASH};")

    def update_tasks(self, tasks: list[m.ProcessStatus]) -> None:
        running = sum(1 for t in tasks if t.state == m.ProcessState.RUNNING)
        crashed = sum(1 for t in tasks if t.state == m.ProcessState.CRASHED)
        if crashed:
            self._tasks.setText(f"{running} run · {crashed} crash")
            self._tasks.setStyleSheet(f"font-size: 12px; color: {Palette.CRASH};")
        else:
            self._tasks.setText(f"{running} running")
            self._tasks.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")

    def set_offline(self) -> None:
        """Mark the card offline and clear its live stats — an unreachable unit's
        last temp / clock / SDR / task counts are stale and no longer valid, so we
        drop them to '—' rather than leave misleading values on the card."""
        self.set_connection(False)
        for stat in (self._temp, self._clock, self._sdr, self._tasks):
            stat.setText("—")
            stat.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")