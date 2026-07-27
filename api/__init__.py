"""
SDR Broadcaster GUI — API client package.

Public surface:
    from api import AgentClient, Fleet, ConnectionState
    from api import models
    from api.client import AgentError, AgentConnectionError, AgentHTTPError
"""
from .client import (
    AgentClient, ConnectionState,
    AgentError, AgentConnectionError, AgentHTTPError,
)
from .fleet import Fleet
from . import models

__all__ = [
    "AgentClient", "Fleet", "ConnectionState", "models",
    "AgentError", "AgentConnectionError", "AgentHTTPError",
]