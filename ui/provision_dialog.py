"""
ProvisionDialog — bootstrap a fresh Raspberry Pi into the fleet over SSH (Phase 2).

The operator gives the Pi's current address + SSH login and a unit number N; the
client derives the hostname and static IPs from the addressing scheme (editable and
persisted), shows them for confirmation, then runs the Provisioner: upload the agent
bundle, install it in the versioned layout, set the hostname + static IPs, and reboot.
Progress streams into a log. On success the unit is registered here (at its new
addresses) and the client waits for it to come back on the network.

Threading: the Provisioner runs on a worker thread via hub.run_async; its per-step
callback emits a Qt signal (thread-safe, queued to the GUI thread) so the log stays
live without touching widgets off-thread.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from config import ProvisionScheme
from state.agent_bundle import bundle_version, find_bundle
from state.provisioner import ProvisionParams, Provisioner
from .qt_adapter import DataHub
from .theme import Palette

# Callback the Units tab passes in to register a provisioned unit (label, addresses,
# api_key) and returns the permanent uid it was given.
RegisterFn = Callable[[str, List[str], str], str]

VERIFY_DEADLINE_S = 210.0    # allow for reboot + agent start on the new IP
VERIFY_INTERVAL_MS = 4000


class ProvisionDialog(QDialog):
    _step = pyqtSignal(str, str)     # (message, level) — from the worker thread

    def __init__(self, hub: DataHub, scheme: ProvisionScheme,
                 register: RegisterFn, default_api_key: str = "",
                 taken_numbers: Optional[set] = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.scheme = scheme
        self._register = register
        self._default_api_key = default_api_key
        self._taken_numbers = taken_numbers or set()
        self._bundle = find_bundle()
        self._bundle_ver = bundle_version(self._bundle) if self._bundle else None
        self._busy = False
        self._done = False
        self._verify_addresses: List[str] = []
        self._verify_deadline = 0.0

        self.setWindowTitle("Provision a new unit")
        self.setMinimumWidth(720)
        self._build()

        self._step.connect(self._on_step)
        self.hub.task_done.connect(self._on_done)
        self.finished.connect(lambda _=0: self._disconnect())
        self._verify_timer = QTimer(self)
        self._verify_timer.timeout.connect(self._poll_verify)

        self._recompute()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        title = QLabel("Provision a new Raspberry Pi")
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {Palette.TEXT};")
        outer.addWidget(title)

        bundled = self._bundle_ver or "— no agent bundle bundled with this client"
        sub = QLabel(f"Installs agent {bundled} over SSH and sets the hostname. "
                     f"DHCP by default (no reboot); tick “static IP” only for a dedicated subnet.")
        sub.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(sub)

        cols = QHBoxLayout()
        cols.setSpacing(18)
        cols.addWidget(self._left_column(), 1)
        cols.addWidget(self._right_column(), 1)
        outer.addLayout(cols)

        # Computed preview
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            f"font-size: 12px; color: {Palette.TEXT}; background: {Palette.SURFACE}; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 6px; padding: 8px 10px;")
        outer.addWidget(self._preview)

        # Live log
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(150)
        self._log.setFont(QFont("monospace", 10))
        self._log.setStyleSheet(
            f"background: {Palette.BG}; color: {Palette.TEXT_MUTED}; "
            f"border: 1px solid {Palette.BORDER}; border-radius: 6px;")
        outer.addWidget(self._log)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")
        outer.addWidget(self._status)

        row = QHBoxLayout()
        self._provision_btn = QPushButton("Provision")
        self._provision_btn.setObjectName("primary")
        self._provision_btn.clicked.connect(self._start)
        row.addWidget(self._provision_btn)
        row.addStretch(1)
        self._close_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._close_btn.rejected.connect(self.reject)
        row.addWidget(self._close_btn)
        outer.addLayout(row)

    def _left_column(self) -> QWidget:
        box = QGroupBox("Target unit")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)

        self._n = QSpinBox()
        self._n.setRange(1, 254)
        self._n.setValue(self._first_free_number())
        self._n.valueChanged.connect(self._recompute)
        form.addRow("Unit number *", self._n)

        self._host = QLineEdit()
        self._host.setPlaceholderText("current IP or hostname, e.g. 192.168.1.50")
        self._host.textChanged.connect(self._sync_buttons)
        form.addRow("Reach it at *", self._host)

        self._user = QLineEdit(self.scheme.ssh_user)
        form.addRow("SSH user *", self._user)

        self._pw = QLineEdit()
        self._pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw.setPlaceholderText("SSH password")
        self._pw.textChanged.connect(self._sync_buttons)
        form.addRow("SSH password *", self._pw)

        self._sudo_pw = QLineEdit()
        self._sudo_pw.setEchoMode(QLineEdit.EchoMode.Password)
        self._sudo_pw.setPlaceholderText("only if different from the SSH password")
        form.addRow("sudo password", self._sudo_pw)

        self._api = QLineEdit(self._default_api_key)
        self._api.setPlaceholderText("baked into the unit's service (fleet API key)")
        form.addRow("API key", self._api)
        return box

    def _right_column(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # DHCP is the default (works over WiFi, a transparent bridge, or a direct
        # cable, and stays reachable by broadcaster-N.local). A static IP is opt-in
        # for a dedicated fleet subnet the PC also joins.
        self._static_chk = QCheckBox("Assign a static IP (advanced — dedicated fleet subnet)")
        self._static_chk.toggled.connect(self._on_static_toggled)
        lay.addWidget(self._static_chk)

        self._scheme_box = QGroupBox("Addressing scheme")
        scheme_box = self._scheme_box
        sform = QFormLayout(scheme_box)
        sform.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sform.setSpacing(6)

        self._prefix = QLineEdit(self.scheme.hostname_prefix)
        self._prefix.textChanged.connect(self._recompute)
        sform.addRow("Hostname prefix", self._prefix)

        self._eth_sub = QLineEdit(self.scheme.eth_subnet)
        self._eth_sub.textChanged.connect(self._recompute)
        sform.addRow("Ethernet subnet", self._eth_sub)

        self._wlan_sub = QLineEdit(self.scheme.wlan_subnet)
        self._wlan_sub.textChanged.connect(self._recompute)
        sform.addRow("WiFi subnet", self._wlan_sub)

        self._gw = QLineEdit(self.scheme.eth_gateway)
        self._gw.textChanged.connect(self._recompute)
        sform.addRow("Gateway", self._gw)

        self._dns = QLineEdit(self.scheme.dns)
        sform.addRow("DNS", self._dns)
        lay.addWidget(scheme_box)

        self._wifi_box = QGroupBox("WiFi")
        wifi_box = self._wifi_box
        wform = QFormLayout(wifi_box)
        wform.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        wform.setSpacing(6)
        self._wifi_chk = QCheckBox("Also configure WiFi (static)")
        self._wifi_chk.toggled.connect(self._recompute)
        wform.addRow(self._wifi_chk)
        self._ssid = QLineEdit(self.scheme.wifi_ssid)
        wform.addRow("SSID", self._ssid)
        self._psk = QLineEdit()
        self._psk.setEchoMode(QLineEdit.EchoMode.Password)
        self._psk.setPlaceholderText("WiFi passphrase (not stored)")
        wform.addRow("Passphrase", self._psk)
        lay.addWidget(wifi_box)
        lay.addStretch(1)
        # DHCP default: grey the static-only boxes. Set enabled state directly here —
        # _recompute (which _on_static_toggled calls) needs widgets built later in
        # _build; the trailing _recompute() in __init__ paints the preview.
        self._scheme_box.setEnabled(False)
        self._wifi_box.setEnabled(False)
        return wrap

    def _on_static_toggled(self, on: bool) -> None:
        # The addressing scheme and WiFi-static boxes only apply when assigning a
        # static IP; grey them out under DHCP so it's clear they're not in play.
        self._scheme_box.setEnabled(on)
        self._wifi_box.setEnabled(on)
        self._recompute()

    # ── Scheme / preview ──────────────────────────────────────────────────────

    def _first_free_number(self) -> int:
        n = 1
        while n in self._taken_numbers and n < 254:
            n += 1
        return n

    def _live_scheme(self) -> ProvisionScheme:
        """The scheme with the dialog's current edits applied (so the preview and
        the run use exactly what's on screen)."""
        s = ProvisionScheme.from_dict(self.scheme.to_dict())
        s.hostname_prefix = self._prefix.text().strip() or "broadcaster"
        s.eth_subnet = self._eth_sub.text().strip() or "10.0.0"
        s.wlan_subnet = self._wlan_sub.text().strip() or "10.0.1"
        s.eth_gateway = self._gw.text().strip()
        s.dns = self._dns.text().strip()
        s.ssh_user = self._user.text().strip() or "pi"
        s.wifi_ssid = self._ssid.text().strip()
        return s

    def _recompute(self, *_) -> None:
        s = self._live_scheme()
        n = self._n.value()
        if not self._static_chk.isChecked():
            self._preview.setText(
                f"Will configure:  <b>{s.hostname_for(n)}</b>   ·   DHCP "
                f"(keeps its current address, no reboot; reachable at "
                f"<b>{s.hostname_for(n)}.local</b>)")
            self._sync_buttons()
            return
        parts = [f"<b>{s.hostname_for(n)}</b>",
                 f"eth <b>{s.eth_ip_for(n)}/{s.prefix_len}</b>"]
        if self._wifi_chk.isChecked():
            parts.append(f"wlan <b>{s.wlan_ip_for(n)}/{s.prefix_len}</b>")
        parts.append(f"gw {s.eth_gateway or '—'}")
        self._preview.setText("Will configure:  " + "   ·   ".join(parts))
        self._sync_buttons()

    def _collision(self) -> str:
        """A blocking misconfiguration in the computed addressing, or '' if fine.
        The important one: a unit whose IP equals its gateway — a host can't be its
        own gateway, NetworkManager rejects it, and the interface comes up with no IP
        (the unit provisions but is then unreachable)."""
        if not self._static_chk.isChecked():
            return ""   # DHCP mode assigns no IP, so no collision is possible
        s = self._live_scheme()
        n = self._n.value()
        if s.eth_gateway and s.eth_ip_for(n) == s.eth_gateway:
            return (f"Ethernet IP {s.eth_ip_for(n)} is the same as the gateway — "
                    f"set the gateway to something else (e.g. {s.eth_subnet}.254), "
                    f"leave it blank for a router-less network, or pick another unit number.")
        if self._wifi_chk.isChecked():
            wgw = self.scheme.wlan_gateway or s.eth_gateway
            if wgw and s.wlan_ip_for(n) == wgw:
                return (f"WiFi IP {s.wlan_ip_for(n)} is the same as the WiFi gateway — "
                        f"change the WiFi gateway or pick another unit number.")
        return ""

    def _sync_buttons(self, *_) -> None:
        collision = self._collision()
        ready = (not self._busy and not self._done
                 and bool(self._host.text().strip())
                 and bool(self._pw.text())
                 and self._bundle is not None
                 and not collision)
        self._provision_btn.setEnabled(ready)
        # Surface a collision inline while idle (progress messages own the label once
        # a run starts). Only clear our own warning, never a run's status text.
        if not self._busy and not self._done:
            if collision:
                self._status.setText("⚠ " + collision)
                self._status.setStyleSheet(f"font-size: 11px; color: {Palette.CRASH};")
            elif self._status.text().startswith("⚠"):
                self._status.setText("")
                self._status.setStyleSheet(f"font-size: 11px; color: {Palette.TEXT_FAINT};")

    # ── Run ───────────────────────────────────────────────────────────────────

    def _params(self) -> ProvisionParams:
        s = self._live_scheme()
        n = self._n.value()
        return ProvisionParams(
            host=self._host.text().strip(),
            ssh_user=s.ssh_user,
            ssh_password=self._pw.text(),
            sudo_password=self._sudo_pw.text(),
            unit_n=n,
            unit_id=s.hostname_for(n),
            api_key=self._api.text().strip(),
            hostname=s.hostname_for(n),
            assign_static=self._static_chk.isChecked(),
            eth_ip=s.eth_ip_for(n),
            prefix_len=s.prefix_len,
            eth_gateway=s.eth_gateway,
            dns=s.dns,
            configure_wlan=self._wifi_chk.isChecked(),
            wlan_ip=s.wlan_ip_for(n),
            wlan_gateway=self.scheme.wlan_gateway or s.eth_gateway,
            wifi_ssid=s.wifi_ssid,
            wifi_psk=self._psk.text(),
        )

    def _start(self) -> None:
        if self._busy or self._bundle is None:
            return
        self._persist_scheme()
        self._busy = True
        self._sync_buttons()
        self._log.clear()
        params = self._params()
        self._provision_host = params.host
        bundle = self._bundle

        def work():
            prov = Provisioner(params, bundle,
                               on_step=lambda m, lvl: self._step.emit(m, lvl))
            return prov.run()   # returns the address to reconnect at

        self.hub.run_async("agentprov:run", work)

    def _persist_scheme(self) -> None:
        """Fold the dialog's scheme edits back into the shared scheme object so the
        Units tab persists them to units.yaml on close."""
        s = self._live_scheme()
        for f in s.to_dict():
            setattr(self.scheme, f, getattr(s, f))

    # ── Streamed steps + results ──────────────────────────────────────────────

    def _on_step(self, message: str, level: str) -> None:
        color = {"ok": Palette.ONLINE, "warn": Palette.ARMED,
                 "error": Palette.CRASH, "info": Palette.TEXT}.get(level, Palette.TEXT_MUTED)
        prefix = {"ok": "✓ ", "warn": "! ", "error": "✗ ", "info": "» "}.get(level, "  ")
        self._log.appendHtml(f'<span style="color:{color};">{prefix}{_esc(message)}</span>')
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    def _on_done(self, label: str, result) -> None:
        if label == "agentprov:run":
            self._on_run_done(result)
        elif label == "agentprov:verify":
            self._on_verify_result(result)

    def _on_run_done(self, result) -> None:
        self._busy = False
        if isinstance(result, Exception):
            self._status.setText(f"Provisioning failed: {result}")
            self._on_step(str(result), "error")
            self._sync_buttons()
            return
        # Register the unit at its new addresses right away — it exists now.
        s = self._live_scheme()
        n = self._n.value()
        label = f"{s.hostname_prefix.title()} {n}"
        addresses = [f"{s.hostname_for(n)}.local"]
        if self._static_chk.isChecked():
            addresses.append(s.eth_ip_for(n))
            if self._wifi_chk.isChecked():
                addresses.append(s.wlan_ip_for(n))
        else:
            # DHCP: the address we provisioned over is still valid and reachable now.
            host = getattr(self, "_provision_host", "")
            if host and host not in addresses:
                addresses.append(host)
        try:
            self._register(label, addresses, self._api.text().strip())
            self._on_step(f"registered '{label}' at {', '.join(addresses)}", "ok")
        except Exception as exc:  # noqa: BLE001
            self._on_step(f"could not register the unit here: {exc}", "warn")
        self._done = True
        self._provision_btn.setText("Provisioned")
        self._sync_buttons()
        # Wait for it to come back — probe EVERY registered address (mDNS name first),
        # not just the static IP. The operator's PC often isn't on the unit's static
        # subnet (10.0.0.x), so the static IP is unroutable from here even though the
        # unit is perfectly reachable at broadcaster-N.local on the same LAN.
        self._verify_addresses = list(addresses)
        self._verify_deadline = time.monotonic() + VERIFY_DEADLINE_S
        self._status.setText(f"Waiting for the unit to come back after reboot "
                             f"(trying {', '.join(addresses)})…")
        self._verify_timer.start(VERIFY_INTERVAL_MS)

    def _poll_verify(self) -> None:
        addresses = list(self._verify_addresses)
        api = self._api.text().strip()

        def probe():
            from api.client import AgentClient
            last = None
            for a in addresses:
                c = AgentClient(a, addresses=[a], api_key=api)
                try:
                    return (a, c.info())
                except Exception as exc:  # noqa: BLE001 — try the next address
                    last = exc
                finally:
                    c.close()
            raise last or RuntimeError("no addresses to probe")

        self.hub.run_async("agentprov:verify", probe)

    def _on_verify_result(self, result) -> None:
        if not isinstance(result, Exception):
            addr, info = result
            self._verify_timer.stop()
            ver = getattr(info, "agent_version", "?")
            self._status.setText(f"✓ Unit is up at {addr} (agent {ver}).")
            self._on_step(f"unit reachable at {addr} — agent {ver}", "ok")
            return
        if time.monotonic() >= self._verify_deadline:
            self._verify_timer.stop()
            self._status.setText(
                "Provisioned and registered, but not reachable from this PC yet at "
                f"{', '.join(self._verify_addresses)}. If your PC isn't on the unit's "
                "static subnet, reach it by its broadcaster-N.local name instead — it "
                "should appear in the Units grid shortly.")

    def _disconnect(self) -> None:
        self._verify_timer.stop()
        try:
            self.hub.task_done.disconnect(self._on_done)
        except (TypeError, RuntimeError):
            pass


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
