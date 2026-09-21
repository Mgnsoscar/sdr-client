"""
Internet-synchronized wall clock (SNTP, RFC 4330) — pure stdlib, no Qt.

The client shows an always-visible clock so an operator doesn't need a browser tab open on
time.is to know the real time of day. The clock is NOT the PC's clock: `query_ntp` asks an
NTP server (UDP/123) and derives the PC clock's OFFSET from true time with the standard
four-timestamp formula; `SyncedClock.now()` is then `time.time() + offset`. When no server
can be reached (no internet, UDP blocked — a units-only field LAN) the clock falls back to
the PC's own time and says so (`source == "local"`), and the widget keeps retrying.

Design notes
- Only the OFFSET is stored, never a frozen time: the display advances with the PC's
  monotonic-ish `time.time()` between syncs, so it stays smooth and a re-sync just nudges it.
- The uncertainty of one sample is about half its round-trip time (`rtt_s / 2`); that is what
  the widget shows as "±N ms". A sample whose server flags itself unsynchronized (LI == 3,
  stratum 0 kiss-o'-death, zero transmit stamp) is rejected.
- 2036 NTP era rollover is not handled (the era-0 epoch conversion is used); fine until then.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

NTP_PORT = 123
# Public pools first (geo-distributed anycast); each is tried in order until one answers.
NTP_SERVERS: Tuple[str, ...] = ("pool.ntp.org", "time.google.com", "time.cloudflare.com", "time.nist.gov")
_NTP_EPOCH_DELTA = 2208988800          # seconds between 1900-01-01 (NTP) and 1970-01-01 (Unix)
_REQUEST = b"\x1b" + 47 * b"\0"         # LI=0, VN=3, Mode=3 (client); all stamps zero


class NtpError(RuntimeError):
    """No usable NTP answer (network, timeout, or a rejected reply)."""


@dataclass(frozen=True)
class NtpSample:
    offset_s: float        # true time − this PC's time (add it to time.time())
    rtt_s: float           # round-trip time of the exchange (uncertainty ≈ rtt/2)
    server: str
    stratum: int
    sampled_at: float      # time.time() on this PC when the sample was taken


def _ts_to_unix(raw: bytes) -> float:
    """An 8-byte NTP timestamp (32.32 fixed point since 1900) → Unix seconds (float)."""
    secs, frac = struct.unpack("!II", raw)
    return secs - _NTP_EPOCH_DELTA + frac / 2 ** 32


def parse_ntp_response(data: bytes, t_send: float, t_recv: float) -> Tuple[float, float, int]:
    """Decode a server reply → (offset_s, rtt_s, stratum) against the client's own send /
    receive instants (`time.time()` values). Pure; raises NtpError for a reply we must not
    trust (wrong mode, unsynchronized server, empty transmit stamp)."""
    if len(data) < 48:
        raise NtpError(f"short NTP reply ({len(data)} bytes)")
    li = (data[0] >> 6) & 0x3
    mode = data[0] & 0x7
    stratum = data[1]
    if mode != 4:
        raise NtpError(f"not a server reply (mode {mode})")
    if li == 3:
        raise NtpError("server clock is unsynchronized (leap indicator alarm)")
    if stratum == 0 or stratum > 15:
        raise NtpError(f"server refused / unusable (stratum {stratum})")
    t2 = _ts_to_unix(data[32:40])       # server receive
    t3 = _ts_to_unix(data[40:48])       # server transmit
    if data[40:48] == b"\0" * 8:
        raise NtpError("empty transmit timestamp")
    offset = ((t2 - t_send) + (t3 - t_recv)) / 2.0
    rtt = (t_recv - t_send) - (t3 - t2)
    return offset, max(rtt, 0.0), stratum


def query_ntp(server: str, timeout: float = 2.0, port: int = NTP_PORT) -> NtpSample:
    """One SNTP exchange with `server` → an NtpSample. Raises NtpError on any failure
    (resolution, timeout, a rejected reply). Blocking — call it off the UI thread."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        raise NtpError(f"no UDP socket: {exc}") from exc
    try:
        sock.settimeout(timeout)
        t_send = time.time()
        try:
            sock.sendto(_REQUEST, (server, port))
            data, _addr = sock.recvfrom(512)
        except (OSError, socket.timeout) as exc:      # gaierror / timeout / network unreachable
            raise NtpError(f"{server}: {exc.__class__.__name__}: {exc}") from exc
        t_recv = time.time()
    finally:
        sock.close()
    offset, rtt, stratum = parse_ntp_response(data, t_send, t_recv)
    return NtpSample(offset_s=offset, rtt_s=rtt, server=server, stratum=stratum, sampled_at=t_recv)


def sync(servers: Iterable[str] = NTP_SERVERS, timeout: float = 2.0, port: int = NTP_PORT) -> NtpSample:
    """The first server that answers wins. Raises NtpError naming every failure when none does."""
    errors = []
    for server in servers:
        try:
            return query_ntp(server, timeout=timeout, port=port)
        except NtpError as exc:
            errors.append(str(exc))
    raise NtpError("; ".join(errors) if errors else "no NTP servers configured")


class SyncedClock:
    """The display's time source: the PC clock corrected by the last good NTP offset, or the
    PC clock alone (`source == "local"`) until a sync succeeds. Pure state, no threads."""

    def __init__(self, time_fn=time.time):
        self._time = time_fn
        self.sample: Optional[NtpSample] = None
        self.last_error: str = ""
        self.attempts: int = 0

    # ── state ──
    @property
    def source(self) -> str:
        return "ntp" if self.sample is not None else "local"

    @property
    def offset_s(self) -> float:
        return self.sample.offset_s if self.sample is not None else 0.0

    def apply(self, sample: NtpSample) -> None:
        self.sample = sample
        self.last_error = ""
        self.attempts += 1

    def fail(self, error: str) -> None:
        """A sync attempt failed. A previously good offset is KEPT (the PC clock drifts far
        less than it is likely to be wrong outright), only the error is recorded."""
        self.last_error = error
        self.attempts += 1

    # ── read-outs ──
    def now(self) -> float:
        """Unix seconds: the PC clock plus the NTP offset when synced."""
        return self._time() + self.offset_s

    def uncertainty_s(self) -> Optional[float]:
        return self.sample.rtt_s / 2.0 if self.sample is not None else None

    def age_s(self) -> Optional[float]:
        return (self._time() - self.sample.sampled_at) if self.sample is not None else None

    def status_text(self) -> str:
        """The compact chip text: 'NTP ✓ ±12 ms' / 'PC clock' / 'syncing…'."""
        if self.sample is not None:
            unc = self.uncertainty_s() or 0.0
            return f"NTP ✓ ±{unc * 1000:.0f} ms"
        if self.attempts == 0:
            return "syncing…"
        return "PC clock"

    def describe(self) -> str:
        """A fuller tooltip line."""
        if self.sample is not None:
            off = self.sample.offset_s
            drift = ("in step with" if abs(off) < 0.05 else
                     f"{abs(off):.2f} s {'behind' if off > 0 else 'ahead of'}")
            note = f" — the last re-sync failed ({self.last_error})" if self.last_error else ""
            return (f"Internet time from {self.sample.server} (stratum {self.sample.stratum}); "
                    f"this PC's clock is {drift} true time.{note}")
        if self.attempts == 0:
            return "Contacting an internet time server…"
        return ("Internet time (NTP) is unreachable — showing this PC's own clock. "
                f"Retrying in the background. Last error: {self.last_error}")
