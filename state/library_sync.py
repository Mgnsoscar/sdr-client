"""
library_sync — populate the shared library from a connected unit.

pull_library snapshots a unit's scripts (+ their parameter schemas), tasks, and
sequences into a Library. It runs off the GUI thread (several sequential agent
calls) and returns the assembled Library; the caller stores it. Because the fleet
is meant to hold one identical library, pulling from any one unit is enough to
seed a fresh PC.
"""
from __future__ import annotations

import logging
from typing import List

import yaml

from api import models as m

logger = logging.getLogger(__name__)


def parse_tasks_yaml(text) -> List[m.TaskConfig]:
    """tasks.yaml document → TaskConfig list (unknown keys ignored)."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    out: List[m.TaskConfig] = []
    for entry in (doc.get("tasks") or []):
        if isinstance(entry, dict) and entry.get("name") and entry.get("command"):
            try:
                out.append(m.TaskConfig(**entry))
            except Exception as exc:  # noqa: BLE001 — skip a malformed task
                logger.warning("Skipping malformed task '%s': %s", entry.get("name"), exc)
    return out


def pull_library(client) -> m.Library:
    """Snapshot one unit's scripts/tasks/sequences into a Library. Worker thread —
    performs several agent calls; individual failures degrade gracefully."""
    scripts: List[m.LibraryScript] = []
    try:
        names = client.list_scripts()
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: list_scripts failed: %s", exc)
        names = []
    for name in names:
        try:
            content = client.get_script(name)
        except Exception as exc:  # noqa: BLE001 — keep the script even without its body
            logger.warning("pull_library: get_script('%s') failed: %s", name, exc)
            content = ""
        try:
            params = (client.get_script_params(name) or {}).get("params", [])
        except Exception:  # noqa: BLE001 — a script without a schema still belongs
            params = []
        scripts.append(m.LibraryScript(
            name=name, content=content if isinstance(content, str) else "", params=params))

    try:
        tasks = parse_tasks_yaml(client.get_tasks_yaml())
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: get_tasks_yaml failed: %s", exc)
        tasks = []

    try:
        sequences = client.list_sequences()
    except Exception as exc:  # noqa: BLE001
        logger.error("pull_library: list_sequences failed: %s", exc)
        sequences = []

    return m.Library(scripts=scripts, tasks=tasks, sequences=list(sequences))
