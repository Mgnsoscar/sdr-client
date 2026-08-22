"""
AgentUpdateDialog — push the client's bundled agent build to one unit (OTA).

Shows the unit's current agent version and the version the client ships, uploads
the bundle to POST /admin/update, then polls /info until the unit comes back on the
new version (it briefly goes unreachable while it restarts) or a timeout — in which
case the agent has auto-rolled back and /info still shows the old version. A
Roll back button reverts to the previous release.
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from api.client import AgentHTTPError
from .qt_adapter import DataHub
from .theme import Palette
from state.agent_bundle import bundle_version, find_bundle, is_newer

POLL_INTERVAL_MS = 3000
UPDATE_DEADLINE_S = 240.0      # give the Pi time to install deps + restart
CONFIRM_DEADLINE_S = 130.0     # then wait out the unit's health-confirm grace (~90s)
CAP_OTA_STATUS = "ota-status"  # agent capability: /admin/update-status


class AgentUpdateDialog(QDialog):
    def __init__(self, hub: DataHub, hostname: str, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.hostname = hostname
        self._bundle = find_bundle()
        self._bundle_ver = bundle_version(self._bundle) if self._bundle else None
        self._current: Optional[str] = None
        self._previous: Optional[str] = None
        self._target: Optional[str] = None      # version we're waiting to see
        self._from_version: Optional[str] = None # version we're updating FROM (rollback tell)
        self._phase = ""                         # "restart" → "confirm"
        self._deadline = 0.0
        self._busy = False

        self.setWindowTitle("Update agent")
        self.setMinimumWidth(420)
        self._build()

        self.hub.task_done.connect(self._on_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_info)

        self._refresh_info()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(8)

        self._unit_lbl = QLabel(self._unit_name())
        self._unit_lbl.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(self._unit_lbl)

        self._cur_lbl = QLabel("current: …")
        self._cur_lbl.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._cur_lbl)

        bundled = self._bundle_ver or "— (this build ships no agent bundle)"
        self._bundle_lbl = QLabel(f"bundled with this client: {bundled}")
        self._bundle_lbl.setStyleSheet(f"font-size: 12px; color: {Palette.TEXT_MUTED};")
        outer.addWidget(self._bundle_lbl)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        row = QHBoxLayout()
        self._update_btn = QPushButton("Update")
        self._update_btn.setObjectName("primary")
        self._update_btn.setEnabled(False)
        self._update_btn.clicked.connect(self._start_update)
        row.addWidget(self._update_btn)
        self._rollback_btn = QPushButton("Roll back")
        self._rollback_btn.setEnabled(False)
        self._rollback_btn.clicked.connect(self._start_rollback)
        row.addWidget(self._rollback_btn)
        row.addStretch(1)
        outer.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _unit_name(self) -> str:
        try:
            return self.hub.fleet.get(self.hostname).label or self.hostname
        except Exception:  # noqa: BLE001
            return self.hostname

    # ── State ─────────────────────────────────────────────────────────────────

    def _refresh_info(self) -> None:
        self.hub.run_async(f"agentupd_info:{self.hostname}",
                           lambda: self.hub.fleet.get(self.hostname).info())

    def _sync_buttons(self) -> None:
        # Offer the update only when the bundle is strictly NEWER (numeric compare, so
        # 1.0.10 > 1.0.9). To move to an older build, use Roll back.
        newer = is_newer(self._bundle_ver, self._current)
        can_update = (not self._busy and self._bundle is not None
                      and self._current is not None and newer)
        self._update_btn.setEnabled(bool(can_update))
        if self._bundle_ver and self._current and not newer:
            self._update_btn.setText(f"Up to date ({self._current})")
        else:
            self._update_btn.setText(
                f"Update to {self._bundle_ver}" if self._bundle_ver else "Update")
        self._rollback_btn.setEnabled(not self._busy and bool(self._previous))
        if self._previous:
            self._rollback_btn.setText(f"Roll back to {self._previous}")

    def _start_update(self) -> None:
        if not self._bundle:
            return
        self._busy = True
        self._from_version = self._current       # to recognise an auto-rollback later
        self._sync_buttons()
        self._status.setText("uploading & staging on the unit (installs dependencies)…")
        path = str(self._bundle)
        self.hub.run_async(f"agentupd_apply:{self.hostname}",
                           lambda: self.hub.fleet.get(self.hostname).update_agent(path))

    def _start_rollback(self) -> None:
        self._busy = True
        self._sync_buttons()
        self._status.setText("rolling back…")
        self.hub.run_async(f"agentupd_rollback:{self.hostname}",
                           lambda: self.hub.fleet.get(self.hostname).rollback_agent())

    def _begin_polling(self, target: str) -> None:
        self._target = target
        self._phase = "restart"
        self._deadline = time.monotonic() + UPDATE_DEADLINE_S
        self._status.setText(f"unit restarting — waiting for version {target}…")
        self._poll.start(POLL_INTERVAL_MS)

    def _poll_info(self) -> None:
        # In the restart phase watch the version; in the confirm phase watch the OTA
        # status (did the new release confirm healthy, or did it get rolled back?).
        if self._phase == "confirm":
            self.hub.run_async(f"agentupd_confirm:{self.hostname}",
                               lambda: self.hub.fleet.get(self.hostname).update_status())
        else:
            self.hub.run_async(f"agentupd_poll:{self.hostname}",
                               lambda: self.hub.fleet.get(self.hostname).info())

    # ── Results (marshalled from worker threads via task_done) ─────────────────

    def _on_done(self, label: str, result) -> None:
        host = self.hostname
        if label == f"agentupd_info:{host}":
            if not isinstance(result, Exception):
                self._current = result.agent_version
                self._previous = getattr(result, "previous_version", None)
                self._cur_lbl.setText(f"current: {self._current}"
                                      + (f"  (rollback → {self._previous})" if self._previous else ""))
            self._sync_buttons()
        elif label == f"agentupd_apply:{host}":
            self._on_apply(result)
        elif label == f"agentupd_rollback:{host}":
            self._on_apply(result, rolling_back=True)
        elif label == f"agentupd_poll:{host}":
            self._on_poll(result)
        elif label == f"agentupd_confirm:{host}":
            self._on_confirm(result)

    def _on_apply(self, result, rolling_back: bool = False) -> None:
        if isinstance(result, Exception):
            self._busy = False
            self._status.setText(f"failed: {result}")
            self._sync_buttons()
            return
        if not getattr(result, "ok", False):
            self._busy = False
            self._status.setText(f"failed: {getattr(result, 'message', 'update rejected')}")
            self._sync_buttons()
            return
        self._begin_polling(result.to_version)

    def _on_poll(self, result) -> None:
        # A connection error is expected while the unit restarts — keep waiting.
        if not isinstance(result, Exception) and result.agent_version == self._target:
            self._current = result.agent_version
            self._previous = getattr(result, "previous_version", None)
            self._cur_lbl.setText(f"current: {self._current}"
                                  + (f"  (rollback → {self._previous})" if self._previous else ""))
            # Version flipped. If the unit can report its confirm state, wait for it to
            # mark the release healthy (or roll it back) so we give a definitive
            # outcome instead of declaring success just before a silent revert.
            if getattr(self.hub.fleet.get(self.hostname), "supports",
                       lambda _c: False)(CAP_OTA_STATUS):
                self._begin_confirm()
            else:
                self._finish_ok()
            return
        if time.monotonic() >= self._deadline:
            self._poll.stop()
            self._busy = False
            self._status.setText(
                "timed out waiting for the new version — the unit may have rolled "
                "back to the previous release. Re-check its version.")
            self._refresh_info()
            self._sync_buttons()

    def _begin_confirm(self) -> None:
        self._phase = "confirm"
        self._deadline = time.monotonic() + CONFIRM_DEADLINE_S
        self._status.setText(
            f"now on {self._target} — waiting for the unit to confirm it healthy…")

    def _on_confirm(self, result) -> None:
        # An old agent without the route, or a transient error: don't block on
        # confirmation we can't observe — the version already flipped, call it done.
        if isinstance(result, AgentHTTPError) and result.status_code == 404:
            self._finish_ok()
            return
        if isinstance(result, dict):
            cur = result.get("current_version")
            pending = result.get("pending_version")
            confirmed = result.get("pending_confirmed")
            # Healthy: the pending release confirmed (or the marker's already cleared
            # with us on the target version).
            if confirmed or (pending is None and cur == self._target):
                self._finish_ok(confirmed=True)
                return
            # Rolled back: the unit reverted to the version we came from.
            if pending is None and cur and cur == self._from_version and cur != self._target:
                self._poll.stop()
                self._busy = False
                self._current = cur
                self._cur_lbl.setText(f"current: {cur}")
                self._status.setText(
                    f"⚠ the new release {self._target} did not confirm healthy and the "
                    f"unit rolled back to {cur}. Check `journalctl -u sdr-agent` on the "
                    f"unit for why it failed to start.")
                self._sync_buttons()
                return
        if time.monotonic() >= self._deadline:
            # Version is on target but we never saw a confirm — report it as running,
            # with a hint, rather than hanging.
            self._finish_ok(note=" (could not verify health-confirm — re-check shortly)")

    def _finish_ok(self, confirmed: bool = False, note: str = "") -> None:
        self._poll.stop()
        self._busy = False
        self._current = self._target
        self._cur_lbl.setText(f"current: {self._current}"
                              + (f"  (rollback → {self._previous})" if self._previous else ""))
        tick = "✓ confirmed healthy — " if confirmed else "✓ "
        self._status.setText(f"{tick}now running {self._current}{note}")
        self._sync_buttons()

    def _disconnect(self) -> None:
        self._poll.stop()
        try:
            self.hub.task_done.disconnect(self._on_done)
        except (TypeError, RuntimeError):
            pass
