"""
Event classification for events arriving from an agent.

Wraps a raw event payload (delivered over the SSE stream) in the matching
client-side model based on its "type" discriminator. This was previously part
of webhook/receiver.py (the inbound HTTP server), which has been removed — the
client now reads events via an outbound SSE stream (see webhook/stream_client.py),
so the classification logic lives here in a neutral module both can import.

The agent's event payloads all carry a "type" discriminator:
    crash
    event_started | event_stopped | event_aborted | event_modified
    sequence_started | sequence_step | sequence_stopped | sequence_aborted | sequence_modified
    task_started | task_stopped | task_restarted
"""
from __future__ import annotations

import logging
from typing import Callable, Union

from api import models as m

logger = logging.getLogger(__name__)

# A classified event is one of these model types (already parsed), or the raw
# dict if the type is unknown / the payload didn't match its model.
ReceivedEvent = Union[
    m.CrashEvent, m.EventWebhook, m.SequenceWebhook, m.TaskEvent, dict
]

# Callback signature: fn(event) -> None
EventCallback = Callable[[ReceivedEvent], None]


def classify(payload: dict) -> ReceivedEvent:
    """Wrap a raw event payload in the right model based on its 'type' field."""
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("Event payload didn't match model for type=%s: %s", etype, exc)
    # Unknown type or parse failure — pass the raw dict through so nothing is lost.
    return payload