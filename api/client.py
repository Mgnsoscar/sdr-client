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

import httpx

from . import models as m

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 10.0   # seconds


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
        use_https: bool = False,
        keepalive_expiry: float = 120.0,
    ):
        """
        hostname : e.g. "hostname-1.local" or an IP. No scheme, no port.
        unit_id  : label for errors/logs; defaults to hostname until /info is read.
        api_key  : sent as X-API-Key if the agent has auth enabled.
        keepalive_expiry : seconds httpx keeps an idle pooled connection. Set well
                           above the poll interval so connections aren't churned.
        """
        self.hostname = hostname
        self.unit_id = unit_id or hostname
        self.api_key = api_key
        self.port = port
        self.timeout = timeout
        self.use_https = use_https
        self.keepalive_expiry = keepalive_expiry
        self.scheme = "https" if use_https else "http"

        # The address httpx actually connects to. Starts as the hostname; after a
        # successful resolve (in warmup) we pin it to the IP so reconnects skip the
        # slow mDNS lookup. The Host header still carries the hostname.
        self._resolved_ip: Optional[str] = None
        self.base_url = f"{self.scheme}://{hostname}:{port}"

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

        headers = {"Content-Type": "application/json"}
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
            # Connect to the cached IP; keep the Host header as the hostname so the
            # agent still sees the expected host.
            base = f"{self.scheme}://{self._resolved_ip}:{self.port}"
            headers = {**self._headers, "Host": f"{self.hostname}:{self.port}"}
        else:
            base = self.base_url
            headers = self._headers

        return httpx.Client(
            base_url=base, headers=headers, timeout=self.timeout, transport=transport
        )

    # ── Low-level request helper ───────────────────────────────────────────────

    # httpx errors that indicate a transport/connection problem (as opposed to the
    # server returning an HTTP error status). A stale keep-alive connection that
    # was silently dropped while idle surfaces as one of these — and is safe to
    # retry once on a fresh connection.
    _TRANSPORT_ERRORS = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.RemoteProtocolError,   # server closed an idle keep-alive connection
        httpx.PoolTimeout,
        httpx.WriteError,
    )

    def _request(self, method: str, path: str, *, json=None, params=None, files=None,
                 _retry: bool = True):
        # Capture the current client under the lock. The request itself runs
        # OUTSIDE the lock (so concurrent requests to this unit still overlap and a
        # slow call doesn't block others), but because we hold our own reference,
        # a concurrent rebuild swapping self._client can't pull it out from under
        # us mid-request. This removes the startup race where warmup's IP-pin
        # rebuild collided with an in-flight poll request (the intermittent "—").
        with self._lock:
            client = self._client
        try:
            if files is not None:
                resp = client.request(method, path, params=params, files=files,
                                      headers={"Content-Type": None})
            else:
                resp = client.request(method, path, json=json, params=params)
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
                                     files=files, _retry=False)
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

    def _resolve_and_pin_ip(self) -> None:
        """
        Resolve the hostname to an IP once and rebuild the client to connect to that
        IP directly. Makes future reconnects fast (no repeat mDNS lookup). If the
        hostname is already an IP, or resolution fails, this is a no-op and we keep
        using the hostname. The slow mDNS cost is paid here, in warmup, off the UI
        thread — not on every reconnect.
        """
        if self._resolved_ip is not None:
            return
        # If hostname is already a bare IP, nothing to resolve.
        try:
            socket.inet_aton(self.hostname)
            return  # it's an IPv4 address already
        except OSError:
            pass
        try:
            ip = socket.gethostbyname(self.hostname)   # resolves .local via the OS resolver
        except OSError:
            return  # leave hostname-based; resolution may work later
        if ip and ip != self.hostname:
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
        """
        t0 = time.perf_counter()
        try:
            data = self._request("GET", "/info")
            info = m.AgentInfo(**data)
            self.unit_id = info.unit_id
            self.state = ConnectionState.ONLINE
            self.last_error = ""
            self.last_latency_s = time.perf_counter() - t0
            self.last_contact_ok = time.perf_counter()
            # Pin the resolved IP now that we know the host is reachable.
            self._resolve_and_pin_ip()
        except AgentConnectionError as exc:
            self.state = ConnectionState.OFFLINE
            self.last_error = str(exc)
            self.last_latency_s = None
        except AgentHTTPError as exc:
            # Reachable but unhappy (e.g. auth) — that's ERROR, not OFFLINE.
            self.state = ConnectionState.ERROR
            self.last_error = str(exc)
            self.last_latency_s = time.perf_counter() - t0
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
        # Adopt the real unit_id once we know it
        self.unit_id = info.unit_id
        return info

    def system(self) -> m.SystemHealth:
        return m.SystemHealth(**self._request("GET", "/system"))

    def sdr(self) -> m.SdrStatus:
        return m.SdrStatus(**self._request("GET", "/sdr"))

    def reload(self) -> dict:
        return self._request("POST", "/reload")

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

    def task_logs(self, name: str, lines: int = 100) -> List[str]:
        return self._request("GET", f"/tasks/{name}/logs", params={"lines": lines})

    def task_history(self, name: str) -> List[m.ExitRecord]:
        return [m.ExitRecord(**r) for r in self._request("GET", f"/tasks/{name}/history")]

    def log_stream_url(self, name: str, lines: int = 50) -> str:
        """WebSocket URL for live log streaming (used by a separate stream thread)."""
        ws_scheme = "wss" if self.base_url.startswith("https") else "ws"
        host_port = self.base_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host_port}/tasks/{name}/logs/stream?lines={lines}"
        if self.api_key:
            url += f"&api_key={self.api_key}"
        return url

    # ══════════════════════════════════════════════════════════════════════════
    # Scripts & task registry
    # ══════════════════════════════════════════════════════════════════════════

    def list_scripts(self) -> List[str]:
        return self._request("GET", "/scripts")

    def upload_script(self, filename: str, content: bytes) -> dict:
        files = {"file": (filename, content, "text/x-python")}
        return self._request("POST", "/scripts/upload", files=files)

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
        url = f"{self.base_url}/events/stream"
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