"""
Webhook package — event classification + the outbound SSE stream client.

The client used to run an inbound HTTP receiver (WebhookReceiver) that agents
POSTed to. That required the laptop to accept inbound connections, which laptop
firewalls block without admin. It has been replaced by an outbound SSE stream
(EventStreamManager in webhook/stream_client.py): the laptop opens a long-lived
GET to each unit's /events/stream. The old receiver has been removed; only the
shared event classification remains here.

Public surface:
    from webhook import classify, ReceivedEvent
    from webhook.stream_client import EventStreamManager
"""
from .classify import classify, ReceivedEvent

__all__ = ["classify", "ReceivedEvent"]