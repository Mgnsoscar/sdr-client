"""
Provisioner tests — drive the SSH bootstrap flow against a fake SSH transport.

A real Pi can't be reached from CI, so these fake paramiko's SSHClient/SFTP and
assert the *orchestration*: the ordered steps, that install + network run under
sudo from the unpacked bundle via env files, that secrets travel over stdin (never
on a command line where `ps` would show them), and that failures raise ProvisionError.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import provisioner as P
from state.provisioner import ProvisionParams, Provisioner, ProvisionError


# ── Fake paramiko ───────────────────────────────────────────────────────────

class _Chan:
    def __init__(self, rc=0):
        self._rc = rc
    def recv_exit_status(self):
        return self._rc
    def shutdown_write(self):
        pass


class _Stdin:
    def __init__(self):
        self.buf = ""
        self.channel = _Chan()
    def write(self, s):
        self.buf += s
    def flush(self):
        pass


class _Stdout:
    def __init__(self, text="", rc=0):
        self._lines = (text.splitlines(keepends=True) if text else [])
        self.channel = _Chan(rc)
    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _Stderr:
    def __init__(self, text=""):
        self._text = text
    def read(self):
        return self._text.encode()


class FakeSFTP:
    def __init__(self, log):
        self.log = log
    def put(self, local, remote):
        self.log.append(("put", local, remote))
    def close(self):
        pass


class FakeClient:
    """Scripted SSH client. `responder(command)` returns (stdout_text, rc, stderr)."""
    def __init__(self, responder, connect_error=None):
        self.commands = []       # every exec_command string
        self.stdins = []         # every stdin buffer (post-run)
        self.sftp_log = []
        self._responder = responder
        self._connect_error = connect_error
        self.connected = False
        self.closed = False
    def set_missing_host_key_policy(self, policy):
        pass
    def connect(self, host, **kw):
        if self._connect_error:
            raise self._connect_error
        self.connected = True
        self.connect_kwargs = dict(kw, host=host)
    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        text, rc, err = self._responder(command)
        stdin = _Stdin()
        self.stdins.append(stdin)
        return stdin, _Stdout(text, rc), _Stderr(err)
    def open_sftp(self):
        return FakeSFTP(self.sftp_log)
    def close(self):
        self.closed = True


def _happy_responder(command):
    if "uname -m" in command:
        return ("aarch64\n", 0, "")
    if "command -v python3" in command:
        return ("/usr/bin/python3\n", 0, "")
    if "systemctl is-active sdr-agent" in command:
        return ("active\n", 0, "")
    if "provision_install.sh" in command:
        return ("==> Provisioning agent 1.0.0\n==> Done.\n", 0, "")
    if "provision_network.sh" in command:
        return ("==> Rebooting.\n", 0, "")
    return ("", 0, "")


def install_fake(monkeypatch, client):
    fake = types.SimpleNamespace(
        SSHClient=lambda: client,
        AutoAddPolicy=lambda: object(),
        AuthenticationException=type("AuthErr", (Exception,), {}),
        SSHException=type("SSHErr", (Exception,), {}),
    )
    monkeypatch.setattr(P, "paramiko", fake)
    return fake


@pytest.fixture
def bundle(tmp_path):
    b = tmp_path / "sdr-agent-1.0.0.tar.gz"
    b.write_bytes(b"not-a-real-tarball-but-a-file")
    return b


def _params():
    return ProvisionParams(
        host="192.168.1.50", ssh_user="pi", ssh_password="raspberry",
        sudo_password="", unit_n=2, unit_id="broadcaster-2", api_key="APIKEY123",
        hostname="broadcaster-2", eth_ip="10.0.0.2", prefix_len=24,
        eth_gateway="10.0.0.1", dns="10.0.0.1 1.1.1.1",
        configure_wlan=True, wlan_ip="10.0.1.2", wlan_gateway="10.0.1.1",
        wifi_ssid="LabNet", wifi_psk="WIFIPASS",
    )


# ── Tests ───────────────────────────────────────────────────────────────────

def test_happy_path_returns_eth_ip(monkeypatch, bundle):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    addr = Provisioner(_params(), bundle).run()
    assert addr == "10.0.0.2"
    assert client.closed


def test_steps_run_in_order(monkeypatch, bundle):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    steps = []
    Provisioner(_params(), bundle, on_step=lambda m, l: steps.append((l, m))).run()
    joined = " || ".join(m for _, m in steps)
    # sanity → upload → install → verify → network, in that order
    for a, b in [("connected", "target OK"), ("target OK", "unpacked"),
                 ("unpacked", "agent installed"), ("agent installed", "service is active"),
                 ("service is active", "rebooting")]:
        assert joined.find(a) < joined.find(b), f"{a} should precede {b}: {joined}"


def test_install_and_network_run_under_sudo_from_bundle(monkeypatch, bundle):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    Provisioner(_params(), bundle).run()
    cmds = client.commands
    inst = [c for c in cmds if "provision_install.sh" in c]
    net = [c for c in cmds if "provision_network.sh" in c]
    assert inst and net
    assert all(c.startswith("sudo -S -p ''") for c in inst + net)
    assert all("/tmp/sdr-bundle/deploy/" in c for c in inst + net)
    # bundle was uploaded then unpacked before install
    assert client.sftp_log and client.sftp_log[0][0] == "put"
    assert any("tar xzf" in c for c in cmds)


def test_secrets_never_on_a_command_line(monkeypatch, bundle):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    Provisioner(_params(), bundle).run()
    all_cmds = "\n".join(client.commands)
    for secret in ("raspberry", "APIKEY123", "WIFIPASS"):
        assert secret not in all_cmds, f"{secret!r} leaked onto a command line"
    # …but they DO reach the box over stdin (env-file bodies + sudo password).
    all_stdin = "\n".join(s.buf for s in client.stdins)
    assert "APIKEY123" in all_stdin and "WIFIPASS" in all_stdin
    assert "raspberry" in all_stdin   # sudo password fed on stdin


def test_env_files_carry_identity(monkeypatch, bundle):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    Provisioner(_params(), bundle).run()
    stdin_text = "\n".join(s.buf for s in client.stdins)
    assert "SDR_UNIT_ID=broadcaster-2" in stdin_text
    assert "SDR_API_KEY=APIKEY123" in stdin_text
    assert "PROV_HOSTNAME=broadcaster-2" in stdin_text
    assert "PROV_ETH_IP=10.0.0.2" in stdin_text
    assert "PROV_WLAN_IP=10.0.1.2" in stdin_text
    assert "PROV_WLAN_SSID=LabNet" in stdin_text


def test_auth_failure_raises(monkeypatch, bundle):
    fake = types.SimpleNamespace(
        SSHClient=None, AutoAddPolicy=lambda: object(),
        AuthenticationException=type("AuthErr", (Exception,), {}),
        SSHException=type("SSHErr", (Exception,), {}),
    )
    client = FakeClient(_happy_responder, connect_error=fake.AuthenticationException())
    fake.SSHClient = lambda: client
    monkeypatch.setattr(P, "paramiko", fake)
    with pytest.raises(ProvisionError, match="authentication failed"):
        Provisioner(_params(), bundle).run()


def test_sudo_failure_in_sanity_raises(monkeypatch, bundle):
    def responder(command):
        if command.strip().startswith("sudo -S -p '' true"):
            return ("", 1, "Sorry, try again.")
        return _happy_responder(command)
    client = FakeClient(responder)
    install_fake(monkeypatch, client)
    with pytest.raises(ProvisionError, match="sudo"):
        Provisioner(_params(), bundle).run()
    assert client.closed   # connection cleaned up even on failure


def test_agent_not_active_raises(monkeypatch, bundle):
    def responder(command):
        if "systemctl is-active sdr-agent" in command:
            return ("activating\n", 3, "")
        return _happy_responder(command)
    client = FakeClient(responder)
    install_fake(monkeypatch, client)
    monkeypatch.setattr(P.time, "sleep", lambda *_: None)   # don't actually wait
    with pytest.raises(ProvisionError, match="did not become active"):
        Provisioner(_params(), bundle).run()


def test_missing_bundle_raises(monkeypatch, tmp_path):
    client = FakeClient(_happy_responder)
    install_fake(monkeypatch, client)
    with pytest.raises(ProvisionError, match="no agent bundle"):
        Provisioner(_params(), tmp_path / "nope.tar.gz").run()
