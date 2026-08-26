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
from .library_tab import LibraryTab
from .qt_adapter import DataHub
from .theme import Palette
from .timeline_tab import TimelineTab
from .units_tab import UnitsTab

logger = logging.getLogger(__name__)

# Plans now live as a sub-tab of Library (authoring), not a top-level tab.
TABS = ["Timeline", "Units", "Library"]

# A unit's clock vs THIS PC's: past this many seconds we flag it, because it's the
# skew that makes scheduled plans (which fire on the unit's own clock) miss. Set
# generously so normal poll timing isn't mistaken for skew.
PC_SKEW_WARN_S = 3.0


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

        # Clock-sync indicator. Clickable when a unit's clock differs from this PC
        # (so schedules would miss): a click sets the reachable units' clocks here.
        self._clock_lbl = QLabel("clocks: —")
        self._clock_lbl.setObjectName("clockUnknown")
        self._clock_lbl.setToolTip("Clock offset between units and this PC")
        self._clock_actionable = False
        self._skew_reachable: list = []
        self._clock_lbl.mousePressEvent = self._on_clock_clicked
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
        self.timeline_tab = TimelineTab(self.hub)
        self.library_tab = LibraryTab(self.hub)
        # The Plans tab is owned by the Library tab (a sub-tab); expose it here too
        # so existing references (e.g. cross-tab navigation) keep working.
        self.plans_tab = self.library_tab._plans_panel
        self._tabs = {
            "Timeline": self.timeline_tab,
            "Units": self.units_tab,
            "Library": self.library_tab,
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
        # Leaving the Units tab while a unit's Calibration sub-tab holds unsaved edits?
        # Let it warn first (Save / Don't save / Cancel), same as navigating inside the unit.
        cur = self._stack.currentWidget()
        if (cur is self.units_tab and self._stack.widget(idx) is not self.units_tab
                and not self.units_tab.confirm_leave()):
            for i, btn in enumerate(self._tab_buttons):        # stay on Units
                btn.setChecked(self._stack.currentIndex() == i)
            return
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
        if label == "sync_clocks":
            self._report_clock_sync(result)
            return
        if label == "state_sync":
            self._report_state_sync(result)
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
        """Flag when any unit's clock differs from THIS PC's — the skew that
        actually breaks scheduled plans (they fire on the unit's own clock).
        Measured against the poll's capture time, so poll/render lag isn't counted
        as skew. When flagged, the indicator is a one-click "sync to this PC"."""
        from datetime import datetime, timezone

        anchor = snap.captured_at or None
        worst = 0.0          # signed: + means the unit is ahead of this PC
        reachable = []
        for host, r in snap.system.items():
            if not isinstance(r, m.SystemHealth) or not r.utc_now:
                continue
            try:
                dt = datetime.fromisoformat(r.utc_now)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            reachable.append(host)
            if anchor is not None:
                off = dt.timestamp() - anchor
                if abs(off) > abs(worst):
                    worst = off
        self._skew_reachable = reachable

        if not reachable or anchor is None:
            self._clock_lbl.setText("clocks: —")
            self._clock_lbl.setObjectName("clockUnknown")
            self._set_clock_actionable(False)
        elif abs(worst) > PC_SKEW_WARN_S:
            self._clock_lbl.setText(f"clocks: ⚠ {self._fmt_skew(worst)} — sync")
            self._clock_lbl.setObjectName("clockWarn")
            self._set_clock_actionable(True)
        else:
            self._clock_lbl.setText("clocks: synced ✓")
            self._clock_lbl.setObjectName("clockOk")
            self._set_clock_actionable(False)
        # Re-polish so the objectName-based stylesheet repaints
        self._clock_lbl.style().unpolish(self._clock_lbl)
        self._clock_lbl.style().polish(self._clock_lbl)

    @staticmethod
    def _fmt_skew(off: float) -> str:
        """'2m behind' / '40s ahead' — magnitude + direction relative to this PC."""
        mag = abs(off)
        unit = f"{mag / 60:.0f}m" if mag >= 90 else f"{mag:.0f}s"
        return f"{unit} {'ahead' if off > 0 else 'behind'}"

    def _set_clock_actionable(self, on: bool) -> None:
        self._clock_actionable = on
        self._clock_lbl.setCursor(
            Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.ArrowCursor)
        self._clock_lbl.setToolTip(
            "Set each reachable unit's clock to this PC's time (for testing "
            "schedules on a unit with no internet)" if on
            else "Clock offset between units and this PC")

    def _unit_name(self, host: str) -> str:
        try:
            return self.hub.fleet.get(host).label or host
        except KeyError:
            return host

    def _on_clock_clicked(self, event) -> None:
        if not self._clock_actionable or not self._skew_reachable:
            return
        hosts = list(self._skew_reachable)
        names = ", ".join(self._unit_name(h) for h in hosts)
        if QMessageBox.question(
            self, "Sync unit clocks",
            f"Set the system clock on {len(hosts)} reachable unit(s) to this PC's "
            f"current time?\n\n{names}\n\nUse this to test schedules on a unit with "
            f"no internet (no NTP). It changes the unit's clock; once the unit is "
            f"back online it re-syncs to real time on its own.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.hub.run_async("sync_clocks", lambda: self._sync_clocks(hosts))

    def _sync_clocks(self, hosts: list) -> list:
        """Worker thread: push this PC's time to each unit CONCURRENTLY. Returns
        per-unit ok. Running the units in parallel means an offline unit costs one
        connect-timeout for the whole batch, not one per unit in series."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _one(h):
            try:
                res = self.hub.fleet.get(h).set_time()
                return (h, bool(res.get("ok")), res.get("detail", ""))
            except Exception as exc:  # noqa: BLE001 — reported per unit
                return (h, False, str(exc))

        if not hosts:
            return []
        out = []
        with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
            for fut in as_completed([pool.submit(_one, h) for h in hosts]):
                out.append(fut.result())
        return out

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

    def _report_clock_sync(self, result) -> None:
        if isinstance(result, Exception):
            QMessageBox.warning(self, "Clock sync failed", str(result))
            return
        ok = [h for h, good, _ in result if good]
        bad = [(h, detail) for h, good, detail in result if not good]
        if not bad:
            self.statusBar().showMessage(
                f"Synced clock on {len(ok)} unit(s) to this PC.", 5000)
            self.hub.refresh_now()      # reflect the corrected skew immediately
            return
        lines = "\n".join(f"  • {self._unit_name(h)}: {d}" for h, d in bad)
        QMessageBox.warning(
            self, "Clock sync — partial",
            f"Set the clock on {len(ok)} unit(s).\n\nFailed on:\n{lines}\n\n"
            f"If a unit reports a permission error, its agent isn't running as "
            f"root (the installed service is, by default).")
        self.hub.refresh_now()

    def _report_state_sync(self, result) -> None:
        """After a plans/schedule edit was mirrored out to the fleet. Stay quiet
        on full success (the whole point is that units track the PC seamlessly);
        only surface the units we couldn't reach, so drift is never silent."""
        if isinstance(result, Exception):        # whole fan-out failed to start
            self.statusBar().showMessage(f"Couldn't sync units: {result}", 6000)
            return
        if not isinstance(result, dict) or not result:
            return
        bad = [h for h, r in result.items() if isinstance(r, Exception)]
        if not bad:
            return
        names = ", ".join(self._unit_name(h) for h in bad)
        self.statusBar().showMessage(
            f"Change saved. {len(bad)} unit(s) unreachable ({names}) — they'll "
            f"catch up on the next deploy.", 6000)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):  # noqa: N802
        self.hub.stop()
        super().closeEvent(event)