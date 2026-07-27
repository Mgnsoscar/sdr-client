"""
Webhook package — local HTTP listener that receives crash/lifecycle events
POSTed by the agents.

Public surface:
    from webhook import WebhookReceiver
"""
from .receiver import WebhookReceiver

__all__ = ["WebhookReceiver"]