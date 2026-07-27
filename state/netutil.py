"""
Small network helpers for the client.

The webhook receiver binds 0.0.0.0, but agents need a concrete address to POST
to. local_ip() returns the laptop's primary LAN IP — the one the Pis can reach
this laptop at — so we can build the webhook URL to register with each agent.
"""
from __future__ import annotations

import socket
from typing import Optional


def local_ip() -> Optional[str]:
    """
    Best-effort primary LAN IP of this machine (the interface used for outbound
    traffic). Doesn't actually send anything — connecting a UDP socket just makes
    the OS pick the right source interface. Returns None if it can't be found.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None