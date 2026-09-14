"""
RF output gate — the client's mirror of ``sdr-agent/paramkit/rf.py`` (pure stdlib).

A transmit script declares one parameter as its RF gate: an on/off control that, when off, means
the unit is not emitting. It is recognised either by the explicit ``is_rf`` marker
(``paramkit .choice(is_rf=True)``, carried by the drift-guarded ``api.argspec``) or, for scripts
that predate the marker, by the ``--rf`` on/off convention. The timeline editor uses it to
auto-gate a duration task dragged across on-air / off-air (an RF-off launch + an RF-on tune at
T0, an RF-off tune at off-air). Works on the argspec parameter dicts the editor already caches.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# The tokens that mean "transmitting" — mirrors every transmit script's own
# ``str(value).strip().lower() in (...)`` parse of ``--rf``.
ON_TOKENS = frozenset({"on", "1", "true", "yes"})


def is_on(value: Any) -> bool:
    """Whether an RF-gate value means the output is ON. Matches the scripts' own parsing."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ON_TOKENS


def _looks_like_rf(param: Dict[str, Any]) -> bool:
    """The ``--rf`` on/off convention: dest ``rf`` or a ``--rf``/``-RF`` flag, whose choices (if
    declared) are the on/off set — so a script that predates the ``is_rf`` marker is still
    recognised. (Declared choices that aren't on/off rule it out — some other ``rf*`` control.)"""
    dest = param.get("dest") or param.get("name")
    flags = [str(f).lower() for f in (param.get("flags") or [])]
    named_rf = dest == "rf" or "--rf" in flags or "-rf" in flags
    if not named_rf:
        return False
    choices = {_choice_token(c).lower() for c in (param.get("choices") or [])}
    return not choices or choices <= {"on", "off"}


def _choice_token(c: Any) -> str:
    """A choice's CLI token — a plain string, or the ``value`` of a {label, value} dict."""
    if isinstance(c, dict):
        return str(c.get("value", c.get("key", "")))
    return str(c)


def gate(params: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The RF-gate parameter among ``params`` (argspec dicts), or None. An explicit ``is_rf``
    marker wins; otherwise the ``--rf`` on/off convention."""
    if not params:
        return None
    for p in params:
        if p.get("is_rf"):
            return p
    for p in params:
        if _looks_like_rf(p):
            return p
    return None


def gate_dest(params: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """The dest of the RF gate, or None if there is no gate."""
    g = gate(params)
    return (g.get("dest") or g.get("name")) if g else None


def gate_tokens(param: Dict[str, Any]) -> Tuple[str, str]:
    """(on_token, off_token) for the gate — its declared on/off choice tokens when it has them,
    else the ``on`` / ``off`` convention."""
    on_tok, off_tok = "on", "off"
    for c in (param.get("choices") or []):
        tok = _choice_token(c)
        if is_on(tok):
            on_tok = tok
        else:
            off_tok = tok
    return on_tok, off_tok


def gate_flag(param: Dict[str, Any]) -> str:
    """The CLI flag to set the gate with: its first long flag, else its first flag, else
    ``--<dest>``."""
    flags = [str(f) for f in (param.get("flags") or [])]
    for f in flags:
        if f.startswith("--"):
            return f
    if flags:
        return flags[0]
    return "--" + str(param.get("dest") or param.get("name") or "rf")


def gate_arg_state(args: List[str], param: Dict[str, Any]) -> Optional[bool]:
    """The gate's value in a launch arg list: True (on) / False (off), or None when the launch
    doesn't set it (the script default applies)."""
    flags = {str(f) for f in (param.get("flags") or [])} | {gate_flag(param)}
    args = list(args or [])
    for i, a in enumerate(args):
        if a in flags and i + 1 < len(args):
            return is_on(args[i + 1])
    return None


def set_gate_arg(args: List[str], param: Dict[str, Any], on: bool) -> List[str]:
    """A copy of ``args`` with the gate set ON/OFF: the value after an existing gate flag is
    replaced, else ``<flag> <token>`` is appended."""
    flags = {str(f) for f in (param.get("flags") or [])} | {gate_flag(param)}
    on_tok, off_tok = gate_tokens(param)
    tok = on_tok if on else off_tok
    out = list(args or [])
    for i, a in enumerate(out):
        if a in flags and i + 1 < len(out):
            out[i + 1] = tok
            return out
    out += [gate_flag(param), tok]
    return out
