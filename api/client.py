"""
AgentClient — a typed, synchronous client for ONE broadcasting unit's agent.

Wraps every agent endpoint with a Python method that returns parsed Pydantic
models. Synchronous (httpx) by design — the GUI dispatches calls to Qt worker
threads to keep the UI responsive (see ui/ layer). One AgentClient holds one
unit's hostname + optional API key.

Errors:
  - AgentConnectionError  — couldn't reach the unit (DNS, timeout, refused)
  - AgentHTTPError        — unit responded with 4xx/5xx (carries status + detail)
Both subclass AgentError so callers can catch broadly.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from enum import Enum
from typing import List, Optional
from urllib.parse import quote, urlencode

import httpx

from . import models as m

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 10.0   # seconds (read/write/pool)
DEFAULT_CONNECT_TIMEOUT = 2.0   # seconds — fail fast on an unreachable unit


# ── Exceptions ─────────────────────────────────────────────────────────────────

class AgentError(Exception):
    """Base for all client errors. Carries the unit id for context."""
    def __init__(self, unit: str, message: str):
        self.unit = unit
        super().__init__(f"[{unit}] {message}")


class AgentConnectionError(AgentError):
    """Could not reach the unit (DNS failure, timeout, connection refused)."""


class AgentHTTPError(AgentError):
    """Unit responded with an HTTP error status."""
    def __init__(self, unit: str, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(unit, f"HTTP {status_code}: {detail}")


# ── Connection state ───────────────────────────────────────────────────────────

class ConnectionState(str, Enum):
    UNKNOWN  = "unknown"     # never contacted yet (fresh client)
    ONLINE   = "online"      # last contact succeeded
    OFFLINE  = "offline"     # last contact failed to connect (DNS/timeout/refused)
    ERROR    = "error"       # reachable but returned an error (e.g. auth failure)


# ── Client ─────────────────────────────────────────────────────────────────────

class AgentClient:
    def __init__(
        self,
        hostname: str,
        unit_id: Optional[str] = None,
        api_key: str = "",
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        use_https: bool = False,
        keepalive_expiry: float = 120.0,
        addresses: Optional[List[str]] = None,
        label: str = "",
        unit_type: str = m.DEFAULT_UNIT_TYPE,
    ):
        """
        hostname : the unit's STABLE identity — the fleet key, and the value used in
                   plans/drift/UI. Normally a permanent uid ("unit_ab12…"); it never
                   changes and is NOT what we connect to.
        label    : the human-facing display name (renamable, unlike hostname).
        addresses: the hosts/IPs to actually connect to, tried in order until one
                   answers (home wifi, work ethernet, an mDNS .local name…).
                   Defaults to [hostname] so a single-address unit behaves as before.
        unit_id  : label for errors/logs; defaults to hostname until /info is read.
        api_key  : sent as X-API-Key if the agent has auth enabled.
        keepalive_expiry : seconds httpx keeps an idle pooled connection. Set well
                           above the poll interval so connections aren't churned.
        """
        self.hostname = hostname                    # identity / fleet key (not a target)
        self.label = label or hostname              # display name
        self.unit_type = unit_type or m.DEFAULT_UNIT_TYPE   # unit kind → library scope
        self.unit_id = unit_id or self.label        # agent's reported id (from /info)
        self.machine_id = ""                        # physical Pi fingerprint (from /info)
        self.api_key = api_key
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.use_https = use_https
        self.keepalive_expiry = keepalive_expiry
        self.scheme = "https" if use_https else "http"

        # Candidate connect targets. The active one is what httpx talks to; on
        # warmup we probe them in order and stick to the first that answers.
        # Drop anything with a ':' — IPv6 (incl. link-local fe80::) which this
        # IPv4/hostname stack can't form a valid URL for (httpx raises InvalidURL).
        self._addresses: List[str] = [a for a in (addresses or [hostname])
                                      if a and ":" not in a] or [hostname]
        self._active_addr: str = self._addresses[0]
        # After a successful resolve (in warmup) we pin the active address to its IP
        # so reconnects skip the slow mDNS lookup. The Host header carries the addr.
        self._resolved_ip: Optional[str] = None
        self.base_url = f"{self.scheme}://{self._active_addr}:{port}"

        # Connection status — updated by warmup()/health() and every request.
        # Construction does NO network I/O; state starts UNKNOWN.
        self.state: ConnectionState = ConnectionState.UNKNOWN
        self.last_error: str = ""
        self.last_contact_ok: Optional[float] = None   # perf_counter timestamp of last success
        self.last_latency_s: Optional[float] = None    # round-trip of the warmup/health probe

        # Guards the underlying httpx client so a request can't run while the
        # client is being rebuilt (e.g. warmup pinning the IP on another thread,
        # or a stale-connection retry). Without this, a poll request mid-flight
        # during a rebuild would spuriously fail — an intermittent, timing-based
        # bug. RLock because _request may re-enter via retry.
        self._lock = threading.RLock()

        # No hardcoded Content-Type: httpx sets it per request from the body type
        # (application/json for json=, multipart/form-data + boundary for files=).
        # A fixed default would clobber the multipart boundary type on uploads.
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        self._headers = headers
        self._client = self._new_client()

    def _new_client(self) -> httpx.Client:
        """
        Build the httpx client. Two robustness measures vs. idle-connection drops:
          1. A generous keepalive_expiry so httpx doesn't retire idle connections
             out from under us between polls.
          2. OS-level TCP keepalive on the socket so the connection stays genuinely
             alive end-to-end (not just marked-alive in httpx's pool), which the Pi
             or a NAT/AP would otherwise drop.
        If we've resolved the hostname to an IP, connect to the IP directly (fast
        reconnects) while keeping the Host header as the hostname.
        """
        limits = httpx.Limits(keepalive_expiry=self.keepalive_expiry)

        # Enable TCP keepalive at the socket level.
        socket_options = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        # Tune keepalive timing where the platform supports it (Linux/most Windows).
        if hasattr(socket, "TCP_KEEPIDLE"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30))
        if hasattr(socket, "TCP_KEEPINTVL"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10))
        if hasattr(socket, "TCP_KEEPCNT"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))

        transport = httpx.HTTPTransport(
            limits=limits, retries=0, socket_options=socket_options
        )

        if self._resolved_ip:
            # Connect to the cached IP; keep the Host header as the active address so
            # the agent still sees the expected host.
            base = f"{self.scheme}://{self._resolved_ip}:{self.port}"
            headers = {**self._headers, "Host": f"{self._active_addr}:{self.port}"}
        else:
            base = self.base_url
            headers = self._headers

        # Short connect timeout so an unreachable unit fails fast (it shows as
        # offline in ~2s, not ~10s); read/write stay generous for slow endpoints.
        timeout = httpx.Timeout(self.timeout, connect=self.connect_timeout)
        return httpx.Client(
            base_url=base, headers=headers, timeout=timeout, transport=transport
        )

    # ── Low-level request helper ───────────────────────────────────────────────

    # httpx errors that indicate a transport/connection problem (as opposed to the
    # server returning an HTTP error status). A stale keep-alive connection that
    # was silently dropped while idle surfaces as one of these — and is safe to
    # retry once on a fresh connection.
    # NOTE: ConnectTimeout is intentionally NOT retried — it means a *new*
    # connection couldn't be established (the unit is unreachable), so retrying
    # only doubles the wait before the unit shows offline. Stale keep-alive drops
    # surface as the errors below on a *reused* connection and are worth one retry.
    _TRANSPORT_ERRORS = (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,   # server closed an idle keep-alive connection
        httpx.PoolTimeout,
        httpx.WriteError,
    )

    def _request(self, method: str, path: str, *, json=None, params=None, files=None,
                 timeout=None, _retry: bool = True):
        # Capture the current client under the lock. The request itself runs
        # OUTSIDE the lock (so concurrent requests to this unit still overlap and a
        # slow call doesn't block others), but because we hold our own reference,
        # a concurrent rebuild swapping self._client can't pull it out from under
        # us mid-request. This removes the startup race where warmup's IP-pin
        # rebuild collided with an in-flight poll request (the intermittent "—").
        with self._lock:
            client = self._client
        # A long per-request timeout (default: the client's) — updates stage deps
        # on the Pi and can run for minutes.
        extra = {"timeout": timeout} if timeout is not None else {}
        try:
            if files is not None:
                resp = client.request(method, path, params=params, files=files, **extra)
            else:
                resp = client.request(method, path, json=json, params=params, **extra)
        except self._TRANSPORT_ERRORS as exc:
            # A pooled connection may have gone stale while idle. Rebuild and retry
            # ONCE on a fresh connection before declaring the unit down.
            if _retry:
                logger.debug("[%s] transport error (%s) — retrying on fresh connection",
                             self.unit_id, type(exc).__name__)
                with self._lock:
                    # Only rebuild if nobody else already replaced this client.
                    if self._client is client:
                        try:
                            self._client.close()
                        except Exception:
                            pass
                        self._client = self._new_client()
                return self._request(method, path, json=json, params=params,
                                     files=files, timeout=timeout, _retry=False)
            raise AgentConnectionError(self.unit_id, f"cannot reach {self.base_url}: {exc}")
        except httpx.HTTPError as exc:
            raise AgentConnectionError(self.unit_id, f"request failed: {exc}")

        if resp.status_code >= 400:
            detail = self._extract_detail(resp)
            raise AgentHTTPError(self.unit_id, resp.status_code, detail)

        if resp.content:
            return resp.json()
        return None

    @staticmethod
    def _extract_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            if isinstance(body, dict) and "detail" in body:
                d = body["detail"]
                return d if isinstance(d, str) else str(d)
            return str(body)
        except Exception:
            return resp.text or f"HTTP {resp.status_code}"

    def close(self) -> None:
        self._client.close()

    # ══════════════════════════════════════════════════════════════════════════
    # Meta / health
    # ══════════════════════════════════════════════════════════════════════════

    def addresses(self) -> List[str]:
        """The candidate connect targets, in order."""
        return list(self._addresses)

    def active_address(self) -> str:
        """The address currently in use (the one that last answered)."""
        return self._active_addr

    def active_ip(self) -> Optional[str]:
        """The IP the client is actually connected to — the resolved/pinned IP, or the
        active address itself when it's already a bare IP. None if not yet resolved.
        Used to persist a unit's last-known IP for a fast reconnect next launch."""
        if self._resolved_ip:
            return self._resolved_ip
        try:
            socket.inet_aton(self._active_addr)
            return self._active_addr
        except OSError:
            return None

    def add_address(self, addr: str) -> bool:
        """Add a candidate connect address (e.g. learned from discovery on a new
        network). Returns True if it was new. The next warmup can then use it."""
        if addr and addr not in self._addresses:
            self._addresses.append(addr)
            return True
        return False

    def _connect_to(self, addr: str) -> None:
        """Point the client at a specific candidate address and rebuild it."""
        self._active_addr = addr
        self._resolved_ip = None
        self.base_url = f"{self.scheme}://{addr}:{self.port}"
        with self._lock:
            self._client = self._new_client()

    def _resolve_and_pin_ip(self) -> None:
        """
        Resolve the active address to an IPv4 IP once and rebuild the client to connect
        to that IP directly. This is done BEFORE the first request (see warmup) for a
        crucial reason: a unit's mDNS name (broadcaster-N.local) resolves to BOTH an
        A (IPv4) and an AAAA (IPv6 link-local, fe80::) record, and the OS resolver
        often prefers IPv6 — which our IPv4-only stack (and httpx) can't use, so the
        connect fails even though the unit is reachable on IPv4 (the exact "reachable
        by `ping -4` but the app says offline" symptom on a direct cable). Forcing an
        IPv4 A-record lookup and connecting to that bare IP sidesteps it entirely, and
        also makes later reconnects fast (no repeat mDNS lookup). No-op for a bare IP
        or if IPv4 resolution fails (we then try the name as-is).
        """
        if self._resolved_ip is not None:
            return
        # If the active address is already a bare IP, nothing to resolve.
        try:
            socket.inet_aton(self._active_addr)
            return  # it's an IPv4 address already
        except OSError:
            pass
        ip = None
        try:
            ip = socket.gethostbyname(self._active_addr)   # IPv4-only (A record) resolver
        except OSError:
            # Fall back to an explicit IPv4 getaddrinfo — some resolvers answer here
            # for an mDNS .local name where gethostbyname didn't.
            try:
                infos = socket.getaddrinfo(self._active_addr, self.port,
                                           socket.AF_INET, socket.SOCK_STREAM)
                ip = infos[0][4][0] if infos else None
            except OSError:
                return  # leave address-based; resolution may work later
        if ip and ip != self._active_addr:
            self._resolved_ip = ip
            with self._lock:
                # Swap in a client that targets the pinned IP. We deliberately do
                # NOT close the old client here: a concurrent request may have just
                # captured it and be mid-flight. Closing it out from under that
                # request would fail it (the intermittent startup "—"). The old
                # client has at most a handful of in-flight requests that finish in
                # milliseconds; once no references remain, httpx releases its
                # connections on garbage collection. This pin runs only once.
                self._client = self._new_client()
            logger.debug("[%s] pinned %s -> %s", self.unit_id, self.hostname, ip)

    def warmup(self) -> ConnectionState:
        """
        Pay the one-time first-contact cost (mDNS resolve + TCP connect + connection
        pool warmup) and establish connection state. Safe to call repeatedly. Never
        raises — it records the outcome in self.state / self.last_error and returns
        the resulting state. Intended to be called from a worker thread at startup
        (e.g. fleet.warmup_all) so the slow first hostname resolution doesn't block
        the UI or hide inside the constructor.

        Resolves and pins the IP so later reconnects skip the slow mDNS lookup.
        Uses /info so it doubles as adopting the real unit_id.

        With several candidate addresses it probes them in order (the currently
        active one first, so a working unit isn't disturbed) and sticks to the first
        that answers — so a unit that moved between networks reconnects without any
        manual address change.
        """
        # Try the active address first (sticky), then the rest.
        ordered = [self._active_addr] + [a for a in self._addresses if a != self._active_addr]
        # If we already know this unit's fingerprint, an address that answers as a
        # DIFFERENT Pi is not our unit — DHCP may have handed a cached/known IP to
        # another device. Skip it rather than adopt the wrong unit's identity.
        expected_mid = self.machine_id
        last_conn_err: Optional[str] = None
        for addr in ordered:
            if addr != self._active_addr or self._resolved_ip is not None:
                try:
                    self._connect_to(addr)
                except Exception as exc:  # noqa: BLE001 — a malformed address (e.g.
                    # a bare IPv6 literal) shouldn't abort probing the others
                    last_conn_err = f"bad address '{addr}': {exc}"
                    continue
            # Resolve a hostname (e.g. an mDNS .local name) to IPv4 and connect to that
            # bare IP BEFORE the first request — otherwise httpx may resolve the name to
            # the unit's IPv6 link-local (fe80::) and fail, even though it's reachable on
            # IPv4. No-op for a bare IP or if resolution fails.
            self._resolve_and_pin_ip()
            t0 = time.perf_counter()
            try:
                data = self._request("GET", "/info")
                info = m.AgentInfo(**data)
                if expected_mid and info.machine_id and info.machine_id != expected_mid:
                    # Reachable, but it's a different unit — this address is stale.
                    last_conn_err = (f"address '{addr}' is a different unit "
                                     f"({info.machine_id[:8]}…), skipping")
                    continue
                self.unit_id = info.unit_id
                if info.machine_id:
                    self.machine_id = info.machine_id
                self.state = ConnectionState.ONLINE
                self.last_error = ""
                self.last_latency_s = time.perf_counter() - t0
                self.last_contact_ok = time.perf_counter()
                self._resolve_and_pin_ip()   # pin the IP of the address that worked
                return self.state
            except AgentConnectionError as exc:
                last_conn_err = str(exc)
                continue   # this address is unreachable — try the next
            except AgentHTTPError as exc:
                # Reachable but unhappy (e.g. auth). The address works at the TCP
                # level, so stop here rather than probing further.
                self.state = ConnectionState.ERROR
                self.last_error = str(exc)
                self.last_latency_s = time.perf_counter() - t0
                return self.state
        # No address answered.
        self.state = ConnectionState.OFFLINE
        self.last_error = last_conn_err or "no address reachable"
        self.last_latency_s = None
        return self.state

    def health(self) -> bool:
        """True if the agent answers /health. Never raises on HTTP — only on connect."""
        t0 = time.perf_counter()
        try:
            data = self._request("GET", "/health")
            ok = bool(data and data.get("status") == "ok")
            self.state = ConnectionState.ONLINE if ok else ConnectionState.ERROR
            if ok:
                self.last_latency_s = time.perf_counter() - t0
                self.last_contact_ok = time.perf_counter()
                self.last_error = ""
            return ok
        except AgentConnectionError as exc:
            # The active address stopped answering. If the unit has other known
            # addresses (e.g. it moved from wifi to ethernet), re-probe them so it
            # recovers on its own without a manual address change.
            if len(self._addresses) > 1:
                return self.warmup() == ConnectionState.ONLINE
            self.state = ConnectionState.OFFLINE
            self.last_error = str(exc)
            return False
        except AgentHTTPError as exc:
            self.state = ConnectionState.ERROR
            self.last_error = str(exc)
            return False

    def info(self) -> m.AgentInfo:
        data = self._request("GET", "/info")
        info = m.AgentInfo(**data)
        # Adopt the real unit_id + machine-id fingerprint once we know them
        self.unit_id = info.unit_id
        if info.machine_id:
            self.machine_id = info.machine_id
        return info

    def system(self) -> m.SystemHealth:
        return m.SystemHealth(**self._request("GET", "/system"))

    def set_time(self, epoch: Optional[float] = None) -> dict:
        """Set the unit's system clock to `epoch` (UTC seconds; defaults to this
        PC's current time). Corrects a Pi with no NTP so scheduled plans — which
        fire on the unit's own clock — land at the intended moment."""
        if epoch is None:
            epoch = time.time()
        return self._request("POST", "/time", json={"epoch": epoch})

    def sdr(self) -> m.SdrStatus:
        return m.SdrStatus(**self._request("GET", "/sdr"))

    def reload(self) -> dict:
        return self._request("POST", "/reload")

    # ── OTA agent update ───────────────────────────────────────────────────────

    def update_agent(self, bundle_path: str, timeout: float = 300.0) -> m.UpdateResult:
        """Upload an agent bundle and apply it (stage → swap → restart). The unit
        replies before restarting, so this returns quickly; the caller then polls
        info() until agent_version changes (or the unit rolls back). Uses a long
        timeout since staging installs dependencies on the Pi."""
        with open(bundle_path, "rb") as fh:
            files = {"bundle": ("bundle.tar.gz", fh, "application/gzip")}
            data = self._request("POST", "/admin/update", files=files, timeout=timeout)
        return m.UpdateResult(**data)

    def rollback_agent(self) -> m.UpdateResult:
        return m.UpdateResult(**self._request("POST", "/admin/rollback"))

    def agent_releases(self) -> List[m.AgentRelease]:
        return [m.AgentRelease(**r) for r in self._request("GET", "/admin/releases")]

    # ══════════════════════════════════════════════════════════════════════════
    # Tasks
    # ══════════════════════════════════════════════════════════════════════════

    def list_tasks(self) -> List[m.ProcessStatus]:
        return [m.ProcessStatus(**t) for t in self._request("GET", "/tasks")]

    def task_status(self, name: str) -> m.ProcessStatus:
        return m.ProcessStatus(**self._request("GET", f"/tasks/{name}"))

    def start_task(self, name: str, request: Optional[m.StartRequest] = None) -> m.ProcessStatus:
        body = request.model_dump() if request else None
        return m.ProcessStatus(**self._request("POST", f"/tasks/{name}/start", json=body))

    def stop_task(self, name: str) -> m.ProcessStatus:
        return m.ProcessStatus(**self._request("POST", f"/tasks/{name}/stop"))

    def restart_task(self, name: str, request: Optional[m.StartRequest] = None) -> m.ProcessStatus:
        body = request.model_dump() if request else None
        return m.ProcessStatus(**self._request("POST", f"/tasks/{name}/restart", json=body))

    def set_task_params(self, name: str, values: dict, wait: float = 1.0) -> dict:
        """Retune a running task's live parameters. Returns the agent's
        {ok, accepted, rejected, applied, pending}."""
        return self._request("POST", f"/tasks/{name}/params",
                             json={"values": values, "wait": wait})

    def get_task_params(self, name: str) -> dict:
        """Current + applied live-parameter values of a running task."""
        return self._request("GET", f"/tasks/{name}/params/live")

    def task_logs(self, name: str, lines: int = 100) -> List[str]:
        return self._request("GET", f"/tasks/{name}/logs", params={"lines": lines})

    def task_history(self, name: str) -> List[m.ExitRecord]:
        return [m.ExitRecord(**r) for r in self._request("GET", f"/tasks/{name}/history")]

    def _stream_base(self) -> str:
        """Base 'scheme://host:port' for streaming connections (WebSocket logs, SSE
        events). Prefers the IP the active address already resolved to during
        warmup, so OPENING a stream doesn't re-run a slow .local/mDNS lookup every
        time — that lookup is noticeably slow on Windows and is exactly what made
        opening a unit's logs lag. Falls back to the active address (e.g. a .local
        name) until an IP has been pinned."""
        if self._resolved_ip:
            return f"{self.scheme}://{self._resolved_ip}:{self.port}"
        return self.base_url

    def log_stream_url(self, name: str, lines: int = 50) -> str:
        """WebSocket URL for live log streaming (used by a separate stream thread)."""
        base = self._stream_base()
        ws_scheme = "wss" if base.startswith("https") else "ws"
        host_port = base.split("://", 1)[1]
        # URL-encode the task name (it may contain spaces or other characters) and
        # the query string. A raw space here yields a malformed request line that
        # uvicorn rejects with 400 "Invalid HTTP request received".
        params = {"lines": lines}
        if self.api_key:
            params["api_key"] = self.api_key
        return (f"{ws_scheme}://{host_port}"
                f"/tasks/{quote(name, safe='')}/logs/stream?{urlencode(params)}")

    def sequence_log_stream_url(self, seq_id: str, lines: int = 200) -> str:
        """WebSocket URL for a sequence run's log (whole-run timeline + output)."""
        base = self._stream_base()
        ws_scheme = "wss" if base.startswith("https") else "ws"
        host_port = base.split("://", 1)[1]
        params = {"lines": lines}
        if self.api_key:
            params["api_key"] = self.api_key
        return (f"{ws_scheme}://{host_port}"
                f"/sequences/{quote(seq_id, safe='')}/logs/stream?{urlencode(params)}")

    # ══════════════════════════════════════════════════════════════════════════
    # Scripts & task registry
    # ══════════════════════════════════════════════════════════════════════════

    def list_scripts(self) -> List[str]:
        return self._request("GET", "/scripts")

    def upload_script(self, filename: str, content: bytes) -> dict:
        files = {"file": (filename, content, "text/x-python")}
        return self._request("POST", "/scripts/upload", files=files)

    def get_script(self, name: str) -> str:
        """Return a script's source text."""
        data = self._request("GET", f"/scripts/{name}")
        return data.get("content", "")

    def delete_script(self, name: str) -> dict:
        return self._request("DELETE", f"/scripts/{name}")

    def get_script_params(self, name: str) -> dict:
        """Statically-extracted argparse parameters for a script (for building a form)."""
        return self._request("GET", f"/scripts/{name}/params")

    def create_task(self, spec: dict) -> dict:
        """Create a task from a spec (name, command, working_dir, env, autostart,
        restart_on_crash) — the agent appends it to tasks.yaml and reloads live."""
        return self._request("POST", "/tasks", json=spec)

    def update_task(self, name: str, spec: dict) -> dict:
        return self._request("PUT", f"/tasks/{name}", json=spec)

    def delete_task(self, name: str) -> dict:
        return self._request("DELETE", f"/tasks/{name}")

    def get_tasks_yaml(self) -> str:
        data = self._request("GET", "/config/tasks-yaml")
        return data.get("content", "")

    def put_tasks_yaml(self, content: str) -> dict:
        return self._request("PUT", "/config/tasks-yaml", json={"content": content})

    # ══════════════════════════════════════════════════════════════════════════
    # Event stream (SSE)
    # ══════════════════════════════════════════════════════════════════════════

    def event_stream_url(self) -> str:
        """URL of this unit's SSE event stream (used by the stream client thread)."""
        url = f"{self._stream_base()}/events/stream"
        if self.api_key:
            url += f"?api_key={self.api_key}"
        return url

    # ══════════════════════════════════════════════════════════════════════════
    # Scheduled events (simple, single-task)
    # ══════════════════════════════════════════════════════════════════════════

    def create_event(self, request: m.CreateEventRequest) -> m.ScheduledEvent:
        return m.ScheduledEvent(**self._request("POST", "/events", json=request.model_dump()))

    def list_events(self) -> List[m.ScheduledEvent]:
        return [m.ScheduledEvent(**e) for e in self._request("GET", "/events")]

    def get_event(self, event_id: str) -> m.ScheduledEvent:
        return m.ScheduledEvent(**self._request("GET", f"/events/{event_id}"))

    def patch_event(self, event_id: str, stop_at: str) -> m.ScheduledEvent:
        body = m.PatchEventRequest(stop_at=stop_at).model_dump()
        return m.ScheduledEvent(**self._request("PATCH", f"/events/{event_id}", json=body))

    def delete_event(self, event_id: str) -> m.ScheduledEvent:
        return m.ScheduledEvent(**self._request("DELETE", f"/events/{event_id}"))

    # ══════════════════════════════════════════════════════════════════════════
    # Sequences (definitions on this unit)
    # ══════════════════════════════════════════════════════════════════════════

    def create_sequence(self, request: m.CreateSequenceRequest) -> m.Sequence:
        return m.Sequence(**self._request("POST", "/sequences", json=request.model_dump()))

    def list_sequences(self) -> List[m.Sequence]:
        return [m.Sequence(**s) for s in self._request("GET", "/sequences")]

    def get_sequence(self, seq_id: str) -> m.Sequence:
        return m.Sequence(**self._request("GET", f"/sequences/{seq_id}"))

    def update_sequence(self, seq_id: str, request: m.CreateSequenceRequest) -> m.Sequence:
        return m.Sequence(**self._request("PUT", f"/sequences/{seq_id}", json=request.model_dump()))

    def delete_sequence(self, seq_id: str) -> dict:
        return self._request("DELETE", f"/sequences/{seq_id}")

    def arm_sequence(self, seq_id: str, request: m.ArmSequenceRequest) -> m.SequenceRun:
        return m.SequenceRun(**self._request("POST", f"/sequences/{seq_id}/arm",
                                             json=request.model_dump()))

    # ══════════════════════════════════════════════════════════════════════════
    # Sequence runs (executing instances)
    # ══════════════════════════════════════════════════════════════════════════

    def list_sequence_runs(self) -> List[m.SequenceRun]:
        return [m.SequenceRun(**r) for r in self._request("GET", "/sequence-runs")]

    def get_sequence_run(self, run_id: str) -> m.SequenceRun:
        return m.SequenceRun(**self._request("GET", f"/sequence-runs/{run_id}"))

    def patch_sequence_run(self, run_id: str, on_air_end: str) -> m.SequenceRun:
        body = m.PatchSequenceRunRequest(on_air_end=on_air_end).model_dump()
        return m.SequenceRun(**self._request("PATCH", f"/sequence-runs/{run_id}", json=body))

    def cancel_sequence_run(self, run_id: str) -> m.SequenceRun:
        return m.SequenceRun(**self._request("DELETE", f"/sequence-runs/{run_id}"))

    # ══════════════════════════════════════════════════════════════════════════
    # Library (deploy / snapshot the whole definition set)
    # ══════════════════════════════════════════════════════════════════════════

    def get_library(self) -> m.Library:
        """This unit's current definitions (scripts + tasks + sequences), for drift
        comparison against the canonical library."""
        return m.Library(**self._request("GET", "/library"))

    def deploy_library(self, library: m.Library, prune: bool = True) -> m.DeployLibraryResult:
        """Converge this unit to `library`. Definition-only and safe on air: the
        agent keeps running tasks alive and never deletes a sequence with an active
        run. prune=True makes the unit match exactly; prune=False only adds/updates."""
        body = {"library": library.model_dump(), "prune": prune}
        return m.DeployLibraryResult(**self._request("PUT", "/library", json=body))

    # ── Client-state replica (plans + schedule stored on the unit) ─────────────

    def get_plans(self) -> List[m.Plan]:
        """This unit's replica of the PC's plans (opaque storage — not executed here)."""
        return [m.Plan(**p) for p in self._request("GET", "/plans")]

    def put_plans(self, plans: List[m.Plan]) -> List[m.Plan]:
        """Replace this unit's plan replica with `plans` (wholesale)."""
        body = {"plans": [p.model_dump() for p in plans]}
        return [m.Plan(**p) for p in self._request("PUT", "/plans", json=body)]

    def get_schedule(self) -> List[m.ScheduledPlan]:
        """This unit's replica of the PC's schedule."""
        return [m.ScheduledPlan(**s) for s in self._request("GET", "/schedule")]

    def put_schedule(self, schedule: List[m.ScheduledPlan]) -> List[m.ScheduledPlan]:
        """Replace this unit's schedule replica with `schedule` (wholesale)."""
        body = {"schedule": [s.model_dump() for s in schedule]}
        return [m.ScheduledPlan(**s) for s in self._request("PUT", "/schedule", json=body)]

    # ══════════════════════════════════════════════════════════════════════════
    # Panic
    # ══════════════════════════════════════════════════════════════════════════

    def panic(self) -> m.PanicResult:
        """EMERGENCY STOP this unit: stop all tasks, cancel events, abort runs."""
        return m.PanicResult(**self._request("POST", "/panic"))

    # ── Context manager support ────────────────────────────────────────────────

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()