"""
SSE event stream client.

Replaces the old inbound-webhook approach. Instead of the Pi connecting to the
laptop (blocked by laptop firewalls without admin), the laptop opens an outbound
GET to each unit's /events/stream and holds it open, reading Server-Sent Events
as they arrive. Outbound on the existing agent port — no firewall rule, no admin.

One StreamThread per unit holds that unit's connection. It parses SSE frames,
wraps each event in the matching client model (via the shared classifier in
webhook.classify), and hands it to a callback — the same callback the alert
feed consumes. On disconnect it retries with backoff, so units coming and going
self-heal, and the connection state per unit is observable.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import httpx

from api import AgentClient
from api import models as m

logger = logging.getLogger(__name__)

# Shared event classification (see webhook/classify.py).
from webhook.classify import classify, ReceivedEvent  # noqa: E402

EventCallback = Callable[[ReceivedEvent], None]
# Optional per-unit stream-status callback: fn(unit_id, connected: bool)
StatusCallback = Callable[[str, bool], None]


class _StreamThread(threading.Thread):
    """Holds one unit's SSE connection open, reconnecting with backoff."""

    def __init__(
        self,
        client: AgentClient,
        on_event: EventCallback,
        on_status: Optional[StatusCallback],
    ):
        super().__init__(name=f"sse-{client.unit_id}", daemon=True)
        self._client = client
        self._on_event = on_event
        self._on_status = on_status
        # NB: NOT named `_stop` — that shadows threading.Thread._stop(), which
        # Thread.join() calls internally, so join() would blow up with
        # "'Event' object is not callable" once the thread has finished.
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _url(self) -> str:
        return self._client.event_stream_url()

    def run(self) -> None:
        backoff = 1.0
        max_backoff = 20.0
        while not self._stop_event.is_set():
            try:
                self._consume_stream()
                backoff = 1.0  # reset after a clean run
            except Exception as exc:
                logger.debug("[%s] SSE stream error: %s", self._client.hostname, exc)
                if self._on_status:
                    self._on_status(self._client.hostname, False)
            # Wait before reconnecting (unless stopping)
            if not self._stop_event.is_set():
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _consume_stream(self) -> None:
        """Open the stream and yield events until it drops or we're told to stop."""
        # A dedicated client with no read timeout — the stream is long-lived.
        # connect timeout stays short so a dead unit fails fast and we back off.
        timeout = httpx.Timeout(connect=8.0, read=None, write=8.0, pool=8.0)
        with httpx.Client(timeout=timeout) as client:
            with client.stream("GET", self._url()) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"stream returned HTTP {resp.status_code}")
                if self._on_status:
                    self._on_status(self._client.hostname, True)
                logger.info("[%s] SSE stream connected", self._client.hostname)

                event_type = "message"
                data_buf: list[str] = []

                for line in resp.iter_lines():
                    if self._stop_event.is_set():
                        break
                    # httpx yields str lines already decoded, without the newline.
                    if line == "":
                        # blank line = end of one SSE event; dispatch it
                        if data_buf:
                            self._dispatch(event_type, "\n".join(data_buf))
                        event_type = "message"
                        data_buf = []
                        continue
                    if line.startswith(":"):
                        continue  # comment / keepalive
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_buf.append(line[len("data:"):].strip())
                    # other SSE fields (id:, retry:) ignored

    def _dispatch(self, event_type: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.debug("[%s] bad SSE data: %r", self._client.hostname, data)
            return
        event = classify(payload)
        try:
            self._on_event(event)
        except Exception:
            logger.exception("SSE event callback raised")


class EventStreamManager:
    """
    Manages one SSE stream per unit in the fleet — feeds events to a callback via
    outbound, firewall-friendly streams (no inbound listener needed).
    """

    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._threads: Dict[str, _StreamThread] = {}
        self._on_event: Optional[EventCallback] = None
        self._on_status: Optional[StatusCallback] = None

    def set_callback(self, callback: EventCallback) -> None:
        self._on_event = callback

    def set_status_callback(self, callback: StatusCallback) -> None:
        self._on_status = callback

    def start_for(self, client: AgentClient) -> None:
        """Begin streaming from one unit."""
        if client.hostname in self._threads:
            return
        if self._on_event is None:
            raise RuntimeError("set_callback() must be called before starting streams")
        t = _StreamThread(client, self._on_event, self._on_status)
        self._threads[client.hostname] = t
        t.start()

    def start_all(self, clients) -> None:
        for c in clients:
            self.start_for(c)

    def stop_for(self, hostname: str) -> None:
        """Stop and drop the stream for one unit (e.g. it was removed)."""
        t = self._threads.pop(hostname, None)
        if t is not None:
            t.stop()
            t.join(timeout=2.0)

    def stop(self) -> None:
        for t in self._threads.values():
            t.stop()
        for t in self._threads.values():
            t.join(timeout=2.0)
        self._threads.clear()
        logger.info("All SSE streams stopped")