"""
Live log tailing over WebSocket.

The agent exposes ws://<unit>/tasks/{name}/logs/stream which sends the recent
backlog then follows the file in real time as plain-text frames. This client
opens that socket on a background thread and hands each chunk of text to a
callback. A thin Qt adapter turns the callback into a signal so the log view
(on the GUI thread) can append text safely.

Unlike the SSE event stream (one persistent stream per unit, always on), a log
tail is opened on demand — when the user opens the Logs sub-tab for a specific
task — and closed when they switch away. So this manages a SINGLE active tail at
a time per LogTailer instance (open replaces any previous tail).

Uses the `websocket-client` library (already a dependency), which is a simple
synchronous WebSocket suited to a background thread.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import websocket  # websocket-client

logger = logging.getLogger(__name__)

# Callback signatures
TextCallback = Callable[[str], None]            # fn(chunk_of_log_text)
StatusCallback = Callable[[bool, str], None]    # fn(connected, detail)


class _TailThread(threading.Thread):
    def __init__(self, url: str, on_text: TextCallback,
                 on_status: Optional[StatusCallback]):
        super().__init__(name="log-tail", daemon=True)
        self._url = url
        self._on_text = on_text
        self._on_status = on_status
        self._ws: Optional[websocket.WebSocket] = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        # Closing the socket unblocks the recv loop.
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def run(self) -> None:
        try:
            self._ws = websocket.create_connection(self._url, timeout=10)
        except Exception as exc:
            logger.debug("log tail connect failed: %s", exc)
            if self._on_status:
                self._on_status(False, str(exc))
            return

        if self._on_status:
            self._on_status(True, "")
        try:
            while not self._stop_event.is_set():
                try:
                    msg = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    # No data within the read timeout — this is normal for an idle
                    # task (nothing being logged). Loop back, re-check the stop
                    # flag, and keep waiting instead of killing the tail thread.
                    continue
                except (websocket.WebSocketConnectionClosedException, OSError):
                    break
                if msg is None or msg == "":
                    # Could be a close frame or keepalive; loop will re-check stop.
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="replace")
                try:
                    self._on_text(msg)
                except Exception:
                    logger.exception("log tail callback raised")
        finally:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            if self._on_status and not self._stop_event.is_set():
                self._on_status(False, "stream ended")


class LogTailer:
    """
    Manages a single active log tail. Call start(url) to begin; calling it again
    stops the previous tail and starts a new one. stop() ends the current tail.
    """

    def __init__(self):
        self._thread: Optional[_TailThread] = None
        self._on_text: Optional[TextCallback] = None
        self._on_status: Optional[StatusCallback] = None

    def set_callbacks(self, on_text: TextCallback,
                      on_status: Optional[StatusCallback] = None) -> None:
        self._on_text = on_text
        self._on_status = on_status

    def start(self, url: str) -> None:
        if self._on_text is None:
            raise RuntimeError("set_callbacks() must be called before start()")
        self.stop()   # end any existing tail first
        self._thread = _TailThread(url, self._on_text, self._on_status)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.stop()
            self._thread.join(timeout=2.0)
            self._thread = None