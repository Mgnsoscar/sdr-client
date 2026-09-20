"""paramkit param MARKERS that a unit's agent must understand before a script using them is deployed.

A transmit script declares a marker as a paramkit builder kwarg (`number(..., is_elapsed=True)`);
paramkit ships INSIDE the agent release, so a script that uses a kwarg the unit's paramkit does not
know CRASHES at `build_script()` (`TypeError: unexpected keyword argument`) on every launch — while
the agent's static upload validator and this client's static reader both read the file fine. Each
marker therefore maps to the agent capability that says its paramkit accepts the kwarg; the deploy
paths refuse a unit that lacks it (like the `CAL_*`/`SEQUENCE_*` gates) instead of shipping a script
that cannot start. The static reader (`api.argspec`, drift-guarded) exposes each marker as a key on
the param dict, so the check is one pass over `extract_params`.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Set, Tuple

from .argspec import extract_params

# marker key on a param dict → the agent capability that proves the unit's paramkit takes the kwarg
SCRIPT_MARKER_CAPABILITIES: Dict[str, str] = {
    "is_elapsed": "paramkit-is-elapsed",         # agent 1.32.0: number/integer(..., is_elapsed=True)
    "resets_elapsed": "paramkit-resets-elapsed", # agent 1.33.0: flag(..., resets_elapsed=True)
}


def script_marker_capabilities(content) -> Set[str]:
    """The capabilities a script's declared markers require (empty for a plain script or an
    unreadable one — the agent's own validator refuses an unparseable upload)."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) \
        else str(content or "")
    try:
        params = (extract_params(text) or {}).get("params", []) or []
    except Exception:                                    # noqa: BLE001 — static read, best effort
        return set()
    out: Set[str] = set()
    for p in params:
        for key, cap in SCRIPT_MARKER_CAPABILITIES.items():
            if p.get(key):
                out.add(cap)
    return out


def missing_marker_capabilities(content, advertised: Iterable[str]) -> List[Tuple[str, str]]:
    """[(marker_key, capability)] a script needs that `advertised` (a unit's /info capabilities)
    lacks — the reasons a deploy to that unit must be refused."""
    have = set(advertised or [])
    need = script_marker_capabilities(content)
    return sorted((k, c) for k, c in SCRIPT_MARKER_CAPABILITIES.items() if c in need and c not in have)
