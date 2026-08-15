"""
agent_bundle — locate the agent OTA bundle the client ships with.

The client carries a built agent bundle (``sdr-agent-<version>.tar.gz``, produced by
the agent repo's ``deploy/build_bundle.sh``) so the "Update" button can push it to a
unit. This finds it whether running from source or from a PyInstaller-frozen app,
so the same code works in dev and in the standalone installer.

Search order:
  1. ``$SDR_AGENT_BUNDLE`` — an explicit path (dev convenience / override).
  2. ``<app>/bundles/sdr-agent-*.tar.gz`` — where ``<app>`` is PyInstaller's
     ``sys._MEIPASS`` when frozen, else the client repo root. Newest wins.
"""
from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path
from typing import Optional


def bundle_dir() -> Path:
    """The directory the embedded bundle lives in, dev or frozen."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    else:
        base = Path(__file__).resolve().parents[1]   # client repo root
    return base / "bundles"


def find_bundle() -> Optional[Path]:
    """The agent bundle to deploy, or None if the client wasn't built with one."""
    env = os.environ.get("SDR_AGENT_BUNDLE")
    if env and Path(env).is_file():
        return Path(env)
    d = bundle_dir()
    if not d.is_dir():
        return None
    cands = sorted(d.glob("sdr-agent-*.tar.gz"))
    return cands[-1] if cands else None


def bundle_version(path: Optional[Path] = None) -> Optional[str]:
    """The version the bundle contains (from its VERSION file), or None."""
    path = path or find_bundle()
    if not path or not Path(path).is_file():
        return None
    try:
        with tarfile.open(path, "r:gz") as tar:
            for name in ("VERSION", "./VERSION"):
                try:
                    f = tar.extractfile(name)
                except KeyError:
                    continue
                if f is not None:
                    return f.read().decode("utf-8").strip() or None
    except (OSError, tarfile.TarError):
        return None
    return None
