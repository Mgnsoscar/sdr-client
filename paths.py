"""
paths — the per-user writable data directory and the read-only resource location.

In a frozen, per-user-installed app (PyInstaller ``--onedir`` under
``%LOCALAPPDATA%\\Programs\\…``) the code and its bundled assets live in a
read-only location — PyInstaller's temporary ``sys._MEIPASS`` for a onefile
build, or the install folder for onedir — so every config/state file the client
writes must go to a **per-user writable data directory**, not next to the source
as it did when the client only ever ran from a checkout.

This module is the single source of truth for both locations:

  * :func:`data_dir` / :func:`data_file` — where WRITABLE state lives
    (``units.yaml``, ``plans.json``, the caches, …).
  * :func:`resource_dir` — where READ-ONLY bundled assets live (fonts, the
    starter ``units.yaml``); ``ui/theme.py`` and ``state/agent_bundle.py`` keep
    their own resource lookups, this is for first-run seeding.

``data_dir()`` resolves in this order:

  1. ``$SDR_CLIENT_DATA_DIR`` — an explicit override (dev / CI / tests).
  2. When **frozen** — a per-user OS data directory named for the app:
     ``%APPDATA%\\SDR Broadcaster Control`` on Windows,
     ``~/Library/Application Support/SDR Broadcaster Control`` on macOS,
     ``$XDG_CONFIG_HOME`` (else ``~/.config``) ``/SDR Broadcaster Control`` elsewhere.
  3. When running **from source** (dev + the test suite) — the repo root, i.e.
     byte-for-byte the same directory the stores wrote to before this module
     existed, so nothing about the dev/test workflow changes.

The frozen redirect is the whole point of the packaging work; the source
fallback is what keeps the ~600-test suite green without a single test edit.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# The application/product name — also the shortcut name and the per-user folder
# name. Mirrors ``QApplication.setApplicationName`` in ``main.py``.
APP_NAME = "SDR Broadcaster Control"

# Files seeded into a fresh per-user data dir on first launch, if the app ships a
# read-only copy and the user doesn't have one yet. Only ``units.yaml`` is seeded;
# every other store creates its file on first save.
_SEED_FILES = ("units.yaml",)


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) frozen build."""
    return bool(getattr(sys, "frozen", False))


def _repo_root() -> Path:
    """The client checkout root (this file lives at the repo root)."""
    return Path(__file__).resolve().parent


def _per_user_base() -> Path:
    """The OS per-user application-data directory for this app (not created)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """The per-user writable directory for this client's state (see module docs).

    Not created here — call :func:`ensure_data_dir` (or :func:`data_file`, which
    ensures it) before writing.
    """
    override = os.environ.get("SDR_CLIENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return _per_user_base()
    return _repo_root()


def ensure_data_dir() -> Path:
    """:func:`data_dir`, guaranteeing the directory exists."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_file(name: str) -> Path:
    """The path to a writable state file ``name`` inside :func:`data_dir`.

    Ensures the data directory exists so a store's ``tmp.replace(path)`` save can
    never fail on a missing directory (in source mode the directory is the repo
    root, which already exists — a harmless no-op).
    """
    return ensure_data_dir() / name


def resource_dir() -> Path:
    """The directory read-only bundled resources are shipped in.

    ``sys._MEIPASS`` when frozen (PyInstaller unpacks data files there), else the
    repo root when running from source.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return _repo_root()


def seed_defaults() -> None:
    """First-run seeding: copy any shipped starter file into the per-user data
    dir when the user doesn't already have one.

    A no-op when running from source (the resource dir *is* the data dir, so the
    file already lives at the destination) and best-effort otherwise — a failure
    to seed is not fatal (``ClientConfig.load`` already tolerates a missing
    ``units.yaml`` by starting with no units).
    """
    dst_dir = ensure_data_dir()
    src_dir = resource_dir()
    if src_dir.resolve() == dst_dir.resolve():
        return
    for name in _SEED_FILES:
        src, dst = src_dir / name, dst_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copyfile(src, dst)
            except OSError:
                pass  # best-effort; the app runs fine without a seeded starter
