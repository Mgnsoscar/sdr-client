"""
provisioner — SSH bootstrap of a fresh Raspberry Pi into the fleet (Phase 2).

Takes a Pi that is on the network with SSH enabled but no agent, and:
  1. connects over SSH (username + password),
  2. sanity-checks that it's a Pi with working sudo + python3,
  3. uploads the agent bundle and unpacks it,
  4. runs deploy/provision_install.sh (versioned-layout install + service env),
  5. verifies the agent came up,
  6. runs deploy/provision_network.sh (hostname + static IPs) LAST, then reboots.

The re-IP gotcha (docs/provisioning-and-ota.md §4.4): changing an interface's IP
drops the SSH session, so network config is applied last and the box reboots; the
client reconnects at the computed static IP afterwards. This module returns the
address to reconnect at; the caller (ProvisionDialog) polls it and registers the unit.

Nothing here is Qt-aware. Progress is reported through an ``on_step(message, level)``
callback so the UI can stream it; ``level`` is one of "info"/"out"/"warn"/"error"/"ok".
Secrets (passwords, WiFi PSK, API key) are held only for the run and are written to
the Pi in root-only files that are deleted before the run returns — never on a
command line where they'd show in ``ps``.
"""
from __future__ import annotations

import io
import shlex
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import paramiko
except ImportError:  # pragma: no cover - surfaced to the user in the dialog
    paramiko = None

StepFn = Callable[[str, str], None]

REMOTE_BUNDLE = "/tmp/sdr-bundle.tar.gz"
REMOTE_UNPACK = "/tmp/sdr-bundle"


class ProvisionError(Exception):
    """A provisioning step failed; the message is safe to show the operator."""


@dataclass
class ProvisionParams:
    # Reaching the fresh Pi right now:
    host: str                       # current IP / hostname with SSH open
    ssh_user: str
    ssh_password: str
    sudo_password: str = ""         # defaults to ssh_password when blank

    # Identity + fleet:
    unit_n: int = 1
    unit_id: str = ""               # SDR_UNIT_ID baked in (e.g. broadcaster-2)
    api_key: str = ""

    # Target hostname + static IPs (computed from the scheme, confirmed by operator):
    hostname: str = ""
    eth_ip: str = ""
    prefix_len: int = 24
    eth_gateway: str = ""
    dns: str = ""

    # WiFi (optional):
    configure_wlan: bool = False
    wlan_ip: str = ""
    wlan_gateway: str = ""
    wifi_ssid: str = ""
    wifi_psk: str = ""

    ssh_port: int = 22

    def sudo_pw(self) -> str:
        return self.sudo_password or self.ssh_password

    def register_address(self) -> str:
        """The address the unit will answer at after the reboot — its Ethernet
        static IP (the primary), with the mDNS name as a fallback the caller adds."""
        return self.eth_ip or self.host


def _noop(_msg: str, _level: str) -> None:
    pass


class Provisioner:
    def __init__(self, params: ProvisionParams, bundle_path: Path,
                 on_step: Optional[StepFn] = None):
        self.p = params
        self.bundle = Path(bundle_path)
        self.on = on_step or _noop
        self._client = None  # paramiko.SSHClient

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self) -> str:
        """Provision the Pi end to end. Returns the address to reconnect at.
        Raises ProvisionError on any failure (message safe to display)."""
        if paramiko is None:
            raise ProvisionError(
                "paramiko is not installed — run `pip install paramiko` to enable "
                "provisioning (it's in the client requirements).")
        if not self.bundle.is_file():
            raise ProvisionError(
                "no agent bundle to deploy — build one with the agent repo's "
                "deploy/build_bundle.sh and place it in the client's bundles/ dir.")
        try:
            self._connect()
            self._sanity_check()
            self._upload_bundle()
            self._install_agent()
            self._verify_agent()
            self._configure_network()   # last — drops the session at reboot
        finally:
            self._close()
        addr = self.p.register_address()
        self.on(f"reboot triggered — the unit will come up at {addr}", "info")
        return addr

    # ── SSH plumbing ──────────────────────────────────────────────────────────

    def _connect(self) -> None:
        self.on(f"connecting to {self.p.ssh_user}@{self.p.host}:{self.p.ssh_port}…", "info")
        c = paramiko.SSHClient()
        # A fresh Pi's host key is unknown and trust is established out of band
        # (the operator is provisioning it), so accept it. paramiko persists nothing.
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(self.p.host, port=self.p.ssh_port, username=self.p.ssh_user,
                      password=self.p.ssh_password, timeout=15,
                      allow_agent=False, look_for_keys=False)
        except paramiko.AuthenticationException:
            raise ProvisionError("SSH authentication failed — check the username and password.")
        except (socket.timeout, OSError) as exc:
            raise ProvisionError(f"could not reach {self.p.host}: {exc}")
        self._client = c
        self.on("connected", "ok")

    def _close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _run(self, command: str, *, sudo: bool = False, timeout: float = 300.0,
             check: bool = True, quiet: bool = False) -> tuple:
        """Run a command; stream stdout to on_step. Returns (rc, stdout, stderr).
        With sudo=True the command is run under `sudo -S` and the sudo password is
        fed on stdin (never on the command line)."""
        if sudo:
            command = f"sudo -S -p '' {command}"
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        if sudo:
            stdin.write(self.p.sudo_pw() + "\n")
            stdin.flush()
        out_lines = []
        for line in iter(stdout.readline, ""):
            line = line.rstrip("\n")
            out_lines.append(line)
            if line.strip() and not quiet:
                self.on(line, "out")
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode(errors="replace")
        out = "\n".join(out_lines)
        if check and rc != 0:
            detail = (err.strip() or out.strip() or f"exit code {rc}").splitlines()
            raise ProvisionError("; ".join(detail[-3:]) or f"command failed (rc={rc})")
        return rc, out, err

    # ── Steps ─────────────────────────────────────────────────────────────────

    def _sanity_check(self) -> None:
        self.on("checking the target…", "info")
        rc, arch, _ = self._run("uname -m", check=False, quiet=True)
        rc_pi, _, _ = self._run("test -f /etc/rpi-issue", check=False, quiet=True)
        is_pi = rc_pi == 0 or arch.strip().startswith(("arm", "aarch"))
        if not is_pi:
            self.on(f"target doesn't look like a Raspberry Pi (uname -m = {arch.strip()!r}) "
                    "— continuing anyway", "warn")
        rc_py, _, _ = self._run("command -v python3", check=False, quiet=True)
        if rc_py != 0:
            raise ProvisionError("python3 is not installed on the target.")
        # Confirm sudo works with the given password before we start changing things.
        rc_sudo, _, err = self._run("true", sudo=True, check=False, quiet=True, timeout=30)
        if rc_sudo != 0:
            raise ProvisionError("sudo failed — check the sudo password (or that the "
                                 "user has sudo rights).")
        self.on(f"target OK ({arch.strip()})", "ok")

    def _upload_bundle(self) -> None:
        size = self.bundle.stat().st_size
        self.on(f"uploading agent bundle ({size // 1024} KiB)…", "info")
        sftp = self._client.open_sftp()
        try:
            sftp.put(str(self.bundle), REMOTE_BUNDLE)
        finally:
            sftp.close()
        self.on("unpacking bundle…", "info")
        self._run(f"rm -rf {REMOTE_UNPACK} && mkdir -p {REMOTE_UNPACK} && "
                  f"tar xzf {REMOTE_BUNDLE} -C {REMOTE_UNPACK}")
        self.on("bundle unpacked", "ok")

    def _write_remote_env(self, path: str, values: dict) -> None:
        """Write a root-readable KEY=value env file on the Pi via sudo, keeping
        secrets off the command line and out of `ps`."""
        body = "".join(f"{k}={v}\n" for k, v in values.items() if v != "")
        # tee under sudo so we can write to a 0600 root file; feed body on stdin
        # after the sudo password.
        cmd = f"sudo -S -p '' bash -c {shlex.quote(f'umask 077; cat > {path}')}"
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=30)
        stdin.write(self.p.sudo_pw() + "\n")
        stdin.write(body)
        stdin.channel.shutdown_write()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise ProvisionError(f"could not write {path} on the target (rc={rc}).")

    def _install_agent(self) -> None:
        self.on("installing the agent (versioned layout + dependencies)…", "info")
        env_file = f"{REMOTE_UNPACK}/.prov-install.env"
        self._write_remote_env(env_file, {
            "SDR_UNIT_ID": self.p.unit_id,
            "SDR_API_KEY": self.p.api_key,
        })
        script = f"{REMOTE_UNPACK}/deploy/provision_install.sh"
        inner = f"set -a; . {shlex.quote(env_file)}; exec bash {shlex.quote(script)}"
        try:
            self._run(f"bash -c {shlex.quote(inner)}", sudo=True, timeout=600)
        finally:
            self._run(f"sudo -S -p '' rm -f {shlex.quote(env_file)}",
                      check=False, quiet=True, timeout=30)
        self.on("agent installed", "ok")

    def _verify_agent(self) -> None:
        self.on("verifying the agent is running…", "info")
        for attempt in range(10):
            rc, out, _ = self._run("systemctl is-active sdr-agent",
                                   check=False, quiet=True, timeout=20)
            if out.strip() == "active":
                self.on("agent service is active", "ok")
                return
            time.sleep(2)
        # Not fatal to the network step, but the operator should know.
        raise ProvisionError("the agent service did not become active — check "
                             "`journalctl -u sdr-agent` on the unit.")

    def _configure_network(self) -> None:
        self.on(f"setting hostname {self.p.hostname} and static IP {self.p.eth_ip}"
                f"/{self.p.prefix_len} (network change — the SSH session will drop)…", "info")
        env_file = f"{REMOTE_UNPACK}/.prov-net.env"
        values = {
            "PROV_HOSTNAME": self.p.hostname,
            "PROV_ETH_IP": self.p.eth_ip,
            "PROV_PREFIX": str(self.p.prefix_len),
            "PROV_ETH_GW": self.p.eth_gateway,
            "PROV_DNS": self.p.dns,
        }
        if self.p.configure_wlan and self.p.wlan_ip:
            values.update({
                "PROV_WLAN_IP": self.p.wlan_ip,
                "PROV_WLAN_GW": self.p.wlan_gateway or self.p.eth_gateway,
                "PROV_WLAN_SSID": self.p.wifi_ssid,
                "PROV_WLAN_PSK": self.p.wifi_psk,
            })
        self._write_remote_env(env_file, values)
        script = f"{REMOTE_UNPACK}/deploy/provision_network.sh"
        inner = f"set -a; . {shlex.quote(env_file)}; rm -f {shlex.quote(env_file)}; " \
                f"exec bash {shlex.quote(script)}"
        # The script backgrounds the reboot (sleep 2) and exits 0 first, but the
        # connection can still be torn down mid-read — treat a dropped session here
        # as success, since the reboot is exactly what we asked for.
        try:
            self._run(f"bash -c {shlex.quote(inner)}", sudo=True, timeout=60, check=False)
        except (EOFError, OSError, paramiko.SSHException):
            pass
        self.on("network configured; unit rebooting", "ok")
