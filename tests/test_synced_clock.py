"""The always-visible, internet-synchronized clock (state/ntp_clock + ui/clock_widget).

The protocol is exercised for real against a loopback fake NTP server (no internet needed);
the fallback (no server answers → this PC's clock, keep retrying) against a closed port.
"""
import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from state import ntp_clock as nc

_NTP_DELTA = 2208988800


def _ts(unix: float) -> bytes:
    secs = int(unix) + _NTP_DELTA
    frac = int((unix - int(unix)) * 2 ** 32)
    return struct.pack("!II", secs, frac)


def _reply(server_time: float, *, mode=4, li=0, stratum=2, originate=b"\0" * 8, xmit=None) -> bytes:
    head = bytes([(li << 6) | (4 << 3) | mode, stratum, 0, 0]) + b"\0" * 12      # 16-byte header
    return head + b"\0" * 8 + originate + _ts(server_time) + (xmit if xmit is not None else _ts(server_time))


# ── protocol (pure) ─────────────────────────────────────────────────────────────

def test_parse_derives_offset_and_rtt_with_the_four_timestamp_formula():
    # The server is 5 s ahead; the exchange took 0.2 s with the server answering instantly.
    offset, rtt, stratum = nc.parse_ntp_response(_reply(1005.1), t_send=1000.0, t_recv=1000.2)
    assert offset == pytest.approx(5.0, abs=1e-6)
    assert rtt == pytest.approx(0.2, abs=1e-6)
    assert stratum == 2


@pytest.mark.parametrize("bad, why", [
    (_reply(1005.0, mode=3), "mode"),                      # a client packet, not a server reply
    (_reply(1005.0, li=3), "unsynchronized"),              # leap-indicator alarm
    (_reply(1005.0, stratum=0), "stratum"),                # kiss-o'-death
    (_reply(1005.0, xmit=b"\0" * 8), "transmit"),          # empty transmit stamp
    (b"\x24\x02" + b"\0" * 10, "short"),
])
def test_parse_rejects_replies_that_must_not_be_trusted(bad, why):
    with pytest.raises(nc.NtpError, match=why):
        nc.parse_ntp_response(bad, 1000.0, 1000.1)


# ── the wire, against a loopback fake server ─────────────────────────────────────

class _FakeNtpServer(threading.Thread):
    """Answers one request with 'server time = now + ahead_s'."""

    def __init__(self, ahead_s: float):
        super().__init__(daemon=True)
        self.ahead = ahead_s
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.got = None

    def run(self):
        try:
            data, addr = self.sock.recvfrom(512)
            self.got = data
            now = time.time() + self.ahead
            self.sock.sendto(_reply(now, originate=data[40:48]), addr)
        finally:
            self.sock.close()


def test_query_ntp_measures_this_pcs_offset_against_a_server():
    srv = _FakeNtpServer(ahead_s=5.0)
    srv.start()
    sample = nc.query_ntp("127.0.0.1", timeout=2.0, port=srv.port)
    srv.join(2)
    assert srv.got is not None and srv.got[0] == 0x1B                  # a v3 client request
    assert sample.offset_s == pytest.approx(5.0, abs=0.1)
    assert 0.0 <= sample.rtt_s < 0.5
    assert sample.server == "127.0.0.1" and sample.stratum == 2
    assert abs(sample.sampled_at - time.time()) < 2


def test_sync_falls_back_with_an_error_when_no_server_answers():
    # A closed loopback port: the kernel answers with ICMP port-unreachable (or we time out).
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    t0 = time.time()
    with pytest.raises(nc.NtpError):
        nc.sync(["127.0.0.1"], timeout=0.3, port=closed_port)
    assert time.time() - t0 < 3


def test_sync_takes_the_first_server_that_answers():
    srv = _FakeNtpServer(ahead_s=-2.0)
    srv.start()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]; probe.close()
    # Same host list, but the port decides who answers; a dead first entry doesn't stop the sync.
    calls = []
    real = nc.query_ntp

    def fake_query(server, timeout=2.0, port=nc.NTP_PORT):
        calls.append(server)
        return real("127.0.0.1", timeout=0.3, port=dead if server == "dead" else srv.port)

    nc_query = nc.query_ntp
    nc.query_ntp = fake_query
    try:
        sample = nc.sync(["dead", "alive"], timeout=0.3)
    finally:
        nc.query_ntp = nc_query
    assert calls == ["dead", "alive"]
    assert sample.offset_s == pytest.approx(-2.0, abs=0.1)


# ── the time source ──────────────────────────────────────────────────────────────

def test_synced_clock_applies_the_offset_and_falls_back_to_the_pc_clock():
    t = {"now": 1_700_000_000.0}
    clk = nc.SyncedClock(time_fn=lambda: t["now"])
    assert clk.source == "local" and clk.now() == t["now"] and clk.status_text() == "syncing…"
    clk.fail("boom")                                            # no server yet → the PC clock, honestly labelled
    assert clk.source == "local" and clk.now() == t["now"] and clk.status_text() == "PC clock"
    assert "unreachable" in clk.describe() and "boom" in clk.describe()
    clk.apply(nc.NtpSample(offset_s=5.0, rtt_s=0.2, server="pool.ntp.org", stratum=2, sampled_at=t["now"]))
    assert clk.source == "ntp" and clk.now() == t["now"] + 5.0
    assert clk.status_text() == "NTP ✓ ±100 ms" and clk.uncertainty_s() == pytest.approx(0.1)
    assert "5.00 s behind" in clk.describe()
    t["now"] += 30
    assert clk.now() == t["now"] + 5.0 and clk.age_s() == pytest.approx(30)
    clk.fail("later outage")                                    # a good offset is KEPT through a failed re-sync
    assert clk.source == "ntp" and clk.now() == t["now"] + 5.0 and "later outage" in clk.describe()


# ── the widget ───────────────────────────────────────────────────────────────────

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication            # noqa: E402
from ui.clock_widget import SyncedClockWidget       # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)
_T = 1_700_000_000.0


def _pump(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_widget_shows_ntp_time_and_the_source_chip():
    clk = nc.SyncedClock(time_fn=lambda: _T)
    w = SyncedClockWidget(autostart=False, clock=clk)
    assert w.chip_text() == "syncing…"
    assert w.time_text() == datetime.fromtimestamp(_T).strftime("%H:%M:%S")
    w._apply_sample(nc.NtpSample(offset_s=3600.0, rtt_s=0.05, server="s", stratum=1, sampled_at=_T))
    assert w.time_text() == datetime.fromtimestamp(_T + 3600).strftime("%H:%M:%S")   # one hour ahead of the PC
    assert w.chip_text() == "NTP ✓ ±25 ms"
    assert "UTC " in w._sub_lbl.text()
    assert w._resync.isActive()                                    # a re-sync is scheduled
    w.stop()


def test_widget_falls_back_to_the_pc_clock_when_ntp_fails():
    clk = nc.SyncedClock(time_fn=lambda: _T)
    w = SyncedClockWidget(autostart=False, clock=clk)
    w._apply_failure("no route to host")
    assert w.time_text() == datetime.fromtimestamp(_T).strftime("%H:%M:%S")   # the PC's own time
    assert w.chip_text() == "PC clock" and "unreachable" in w.toolTip()
    assert w._resync.isActive()                                    # …and it keeps retrying
    w.stop()


def test_widget_syncs_off_thread_and_delivers_the_result_to_the_gui():
    clk = nc.SyncedClock(time_fn=lambda: _T)
    sample = nc.NtpSample(offset_s=7.0, rtt_s=0.1, server="fake", stratum=2, sampled_at=_T)
    w = SyncedClockWidget(autostart=False, clock=clk, sync_fn=lambda: sample)
    w.sync_now()
    assert _pump(lambda: w.chip_text().startswith("NTP"))
    assert clk.now() == _T + 7.0
    # …and a failing exchange lands as the fallback, never as an exception.
    def boom():
        raise nc.NtpError("blocked")
    w2 = SyncedClockWidget(autostart=False, clock=nc.SyncedClock(time_fn=lambda: _T), sync_fn=boom)
    w2.sync_now()
    assert _pump(lambda: w2.chip_text() == "PC clock")
    w.stop(); w2.stop()
