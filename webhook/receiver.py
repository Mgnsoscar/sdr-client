"""
Webhook receiver — a small local HTTP server that receives crash and lifecycle
events POSTed by the agents.

Design:
  - Pure-Python core using http.server in a background thread (no Qt, no deps).
  - Parses the incoming JSON, classifies it by its "type" field, wraps it in the
    matching client-side model, and hands it to a callback.
  - The callback is set by the caller. A thin Qt adapter (built with the UI) will
    set the callback to "emit a Qt signal", marshalling the event to the UI thread.

The agent's webhook payloads all carry a "type" discriminator:
    crash
    event_started | event_stopped | event_aborted | event_modified
    sequence_started | sequence_step | sequence_stopped | sequence_aborted | sequence_modified

Register units to point here via AgentClient.register_webhook(url) where url is
this receiver's advertised address, e.g. http://<laptop-ip>:8766/events
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Union

from api import models as m

logger = logging.getLogger(__name__)

# A received event is one of these model types (already parsed).
ReceivedEvent = Union[m.CrashEvent, m.EventWebhook, m.SequenceWebhook, dict]

# Callback signature: fn(event) -> None
EventCallback = Callable[[ReceivedEvent], None]


def _classify(payload: dict) -> ReceivedEvent:
    """Wrap a raw webhook payload in the right model based on its 'type' field."""
    etype = payload.get("type", "")
    try:
        if etype == "crash":
            return m.CrashEvent(**payload)
        if etype.startswith("event_"):
            return m.EventWebhook(**payload)
        if etype.startswith("sequence_"):
            return m.SequenceWebhook(**payload)
        if etype.startswith("task_"):
            return m.TaskEvent(**payload)
    except Exception as exc:
        logger.warning("Webhook payload didn't match model for type=%s: %s", etype, exc)
    # Unknown type or parse failure — pass the raw dict through so nothing is lost.
    return payload


class WebhookReceiver:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8766,
        path: str = "/events",
        expected_secret: str = "",
    ):
        """
        host : bind address. 0.0.0.0 so units on the LAN can reach it.
        port : the port units POST to.
        path : the URL path units POST to (must match the registered webhook URL).
        expected_secret : if set, reject POSTs whose X-Webhook-Secret doesn't match.
        """
        self.host = host
        self.port = port
        self.path = path
        self.expected_secret = expected_secret
        self._callback: Optional[EventCallback] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def set_callback(self, callback: EventCallback) -> None:
        """Set the function called for each received event. Replace freely; the
        Qt adapter sets this to a signal-emit."""
        self._callback = callback

    def _dispatch(self, event: ReceivedEvent) -> None:
        if self._callback is not None:
            try:
                self._callback(event)
            except Exception:
                logger.exception("Webhook callback raised")

    # ── Server control ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._server is not None:
            return
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            # Silence the default noisy stderr logging
            def log_message(self, *args):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                if self.path.rstrip("/") != receiver.path.rstrip("/"):
                    self.send_error(404, "Not Found")
                    return

                # Optional shared-secret check
                if receiver.expected_secret:
                    got = self.headers.get("X-Webhook-Secret", "")
                    if got != receiver.expected_secret:
                        self.send_error(401, "Unauthorized")
                        return

                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return

                # Acknowledge immediately, then dispatch — keeps the agent's POST fast.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

                event = _classify(payload)
                receiver._dispatch(event)

            def do_GET(self):  # noqa: N802
                # A trivial liveness endpoint so you can curl the receiver to test it.
                if self.path.rstrip("/") in ("/health", ""):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_error(404, "Not Found")

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-receiver", daemon=True
        )
        self._thread.start()
        logger.info("Webhook receiver listening on http://%s:%d%s",
                    self.host, self.port, self.path)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Webhook receiver stopped")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def url_for(self, advertised_host: str) -> str:
        """Build the URL to register with agents, given the laptop's LAN address.
        advertised_host is the IP/hostname the Pis can reach this laptop at."""
        return f"http://{advertised_host}:{self.port}{self.path}"

    def __enter__(self) -> "WebhookReceiver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()