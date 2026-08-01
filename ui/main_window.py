"""
Main window — the application shell.

Layout:
    ┌─────────────────────────────────────────────────────────┐
    │ [Timeline][Units][Sequences][Plans]   ●clocks   🔴 PANIC│  top bar
    ├─────────────────────────────────────────────────────────┤
    │                  active tab (QStackedWidget)            │
    ├─────────────────────────────────────────────────────────┤
    │  ACTIVITY / ALERT FEED  (persistent, collapsible)       │
    └─────────────────────────────────────────────────────────┘

The window connects to the DataHub's Qt signals (event_received, fast_update,
slow_update, task_done). In this first pass the tabs are placeholders; they get
filled in subsequent steps. The chrome, panic flow, clock indicator, and live
alert feed are all functional.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget, QMainWindow,
)

from api import models as m
from .alert_feed import AlertFeed
from .plans_tab import PlansTab
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_tab import TimelineTab
from .units_tab import UnitsTab

logger = logging.getLogger(__name__)

TABS = ["Timeline", "Units", "Sequences", "Plans"]

# Clock-skew threshold (seconds) beyond which we warn before coordinated arming.
CLOCK_WARN_SKEW_S = 1.0


class _Placeholder(QWidget):
    """Temporary empty tab body until the real tab is built."""
    def __init__(self, name: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel(f"{name}")
        lbl.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 16px;")
        sub = QLabel("coming soon")
        sub.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 12px;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        lay.addWidget(sub)


class MainWindow(QMainWindow):
    def __init__(self, hub: DataHub):
        super().__init__()
        self.hub = hub
        self.setWindowTitle("SDR Broadcaster Control")
        self.resize(1180, 760)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._build_topbar(outer)
        self._build_body(outer)
        self._build_alertfeed(outer)

        self._connect_signals()
        self._select_tab(0)

    # ── Top bar ────────────────────────────────────────────────────────────────

    def _build_topbar(self, outer: QVBoxLayout) -> None:
        bar = QWidget()
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)

        # Tab buttons (checkable, mutually exclusive)
        self._tab_buttons: list[QPushButton] = []
        for i, name in enumerate(TABS):
            btn = QPushButton(name)
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, idx=i: self._select_tab(idx))
            lay.addWidget(btn)
            self._tab_buttons.append(btn)

        lay.addStretch(1)

        # Clock-sync indicator
        self._clock_lbl = QLabel("clocks: —")
        self._clock_lbl.setObjectName("clockUnknown")
        self._clock_lbl.setToolTip("Clock synchronization across units")
        lay.addWidget(self._clock_lbl)

        # Spacer before panic
        gap = QLabel("  ")
        lay.addWidget(gap)

        # PANIC
        self._panic_btn = QPushButton("PANIC")
        self._panic_btn.setObjectName("panic")
        self._panic_btn.setToolTip("Emergency stop — halts all RF on every unit")
        self._panic_btn.clicked.connect(self._on_panic)
        lay.addWidget(self._panic_btn)

        outer.addWidget(bar)

    # ── Body (stacked tabs) ─────────────────────────────────────────────────────

    def _build_body(self, outer: QVBoxLayout) -> None:
        self._stack = QStackedWidget()
        # Real tabs where built; placeholders elsewhere for now.
        self.units_tab = UnitsTab(self.hub.fleet, self.hub)
        self.plans_tab = PlansTab(self.hub.fleet, self.hub)
        self.timeline_tab = TimelineTab(self.hub)
        self._tabs = {
            "Timeline": self.timeline_tab,
            "Units": self.units_tab,
            "Sequences": _Placeholder("Sequences"),
            "Plans": self.plans_tab,
        }
        for name in TABS:
            self._stack.addWidget(self._tabs[name])
        outer.addWidget(self._stack, stretch=1)

    # ── Alert feed ───────────────────────────────────────────────────────────

    def _build_alertfeed(self, outer: QVBoxLayout) -> None:
        self.alert_feed = AlertFeed()
        self.alert_feed.alert_raised.connect(self._on_alert)
        outer.addWidget(self.alert_feed)

    # ── Signal wiring ───────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        # These come from background threads but are delivered on the GUI thread
        # because DataHub is a QObject and emits across the thread boundary.
        self.hub.event_received.connect(self._on_event)
        self.hub.fast_update.connect(self._on_fast_update)
        self.hub.slow_update.connect(self._on_slow_update)
        self.hub.stream_status.connect(self._on_stream_status)
        self.hub.task_done.connect(self._on_task_done)

    # ── Tab switching ───────────────────────────────────────────────────────────

    def _select_tab(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._tab_buttons):
            btn.setChecked(i == idx)
        # Let a tab refresh itself when it becomes visible (e.g. Plans reloads its
        # list and live run state rather than being polled).
        w = self._stack.currentWidget()
        if hasattr(w, "on_shown"):
            w.on_shown()

    # ── Event / data handlers (GUI thread) ──────────────────────────────────────

    def _on_event(self, ev) -> None:
        self.alert_feed.add_event(ev)

    def _on_alert(self, line: str) -> None:
        # An attention-worthy event arrived. Make sure the feed is visible.
        self.alert_feed.expand()
        # (Sound / window flash can be added here later.)
        logger.warning("ALERT: %s", line)

    def _on_fast_update(self, snap) -> None:
        # Update the clock indicator from the latest system snapshot.
        self._update_clock_indicator(snap)
        self.units_tab.on_fast_update(snap)

    def _on_slow_update(self, snap) -> None:
        self.units_tab.on_slow_update(snap)

    def _on_stream_status(self, unit_id: str, connected: bool) -> None:
        # Per-unit SSE connection status → reflect on the unit card.
        self.units_tab.on_stream_status(unit_id, connected)
        logger.info("stream %s: %s", unit_id, "connected" if connected else "disconnected")

    def _on_task_done(self, label: str, result) -> None:
        if label == "panic_all":
            self._report_panic_result(result)
            return
        if isinstance(result, Exception):
            logger.error("Action '%s' failed: %s", label, result)
            self.statusBar().showMessage(self._format_action_error(label, result), 6000)

    @staticmethod
    def _format_action_error(label: str, err: Exception) -> str:
        parts = label.split(":")               # "task_start:<host>:<task>"
        if len(parts) == 3 and parts[0].startswith("task_"):
            verb = parts[0].split("_", 1)[1]
            return f"Failed to {verb} '{parts[2]}': {err}"
        return f"Action '{label}' failed: {err}"

    # ── Clock indicator ─────────────────────────────────────────────────────────

    def _update_clock_indicator(self, snap) -> None:
        """Derive a fleet clock-sync status from the fast snapshot's system data."""
        from datetime import datetime, timezone

        times = []
        any_unsynced = False
        for r in snap.system.values():
            if isinstance(r, m.SystemHealth):
                if r.clock_synced is False:
                    any_unsynced = True
                if r.utc_now:
                    try:
                        dt = datetime.fromisoformat(r.utc_now)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        times.append(dt)
                    except ValueError:
                        pass

        if not times:
            self._clock_lbl.setText("clocks: —")
            self._clock_lbl.setObjectName("clockUnknown")
        else:
            skew = (max(times) - min(times)).total_seconds() if len(times) >= 2 else 0.0
            if any_unsynced or skew > CLOCK_WARN_SKEW_S:
                self._clock_lbl.setText(f"clocks: ⚠ {skew:.1f}s skew")
                self._clock_lbl.setObjectName("clockWarn")
            else:
                self._clock_lbl.setText("clocks: synced ✓")
                self._clock_lbl.setObjectName("clockOk")
        # Re-polish so the objectName-based stylesheet repaints
        self._clock_lbl.style().unpolish(self._clock_lbl)
        self._clock_lbl.style().polish(self._clock_lbl)

    # ── Panic ────────────────────────────────────────────────────────────────────

    def _on_panic(self) -> None:
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Critical)
        confirm.setWindowTitle("Emergency stop")
        confirm.setText("Halt all RF on every unit now?")
        confirm.setInformativeText(
            "This aborts every running sequence and event and stops all tasks "
            "on all units."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() == QMessageBox.StandardButton.Yes:
            self._panic_btn.setEnabled(False)
            self._panic_btn.setText("STOPPING…")
            self.hub.panic_all()

    def _report_panic_result(self, result) -> None:
        self._panic_btn.setEnabled(True)
        self._panic_btn.setText("PANIC")

        if isinstance(result, Exception):
            QMessageBox.critical(self, "Panic failed", str(result))
            return

        # result is {unit_id: PanicResult | Exception}
        failed = [u for u, r in result.items() if isinstance(r, Exception)]
        ok = [u for u, r in result.items() if not isinstance(r, Exception)]
        if failed:
            QMessageBox.warning(
                self, "Panic — partial",
                f"Stopped {len(ok)} unit(s).\n\n"
                f"FAILED to confirm stop on: {', '.join(failed)}\n"
                f"Check these units directly."
            )
        else:
            logger.info("Panic confirmed on all %d unit(s)", len(ok))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):  # noqa: N802
        self.hub.stop()
        super().closeEvent(event)