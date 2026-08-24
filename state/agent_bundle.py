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
import re
import sys
import tarfile
from pathlib import Path
from typing import Optional, Tuple


def parse_version(v: Optional[str]) -> Tuple[int, ...]:
    """A numeric, order-correct key for a dotted version string.

    "1.0.10" → (1, 0, 10), which correctly sorts ABOVE "1.0.9" → (1, 0, 9) —
    unlike a plain string compare, where "1.0.10" < "1.0.9". Non-numeric input
    sorts lowest. Trailing non-numeric parts (e.g. a "-rc1" suffix) are ignored
    for ordering, which is fine for our simple MAJOR.MINOR.PATCH scheme."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (-1,)


def is_newer(candidate: Optional[str], current: Optional[str]) -> bool:
    """True if `candidate` is a strictly newer version than `current` (numeric
    compare). A missing candidate is never newer; a missing current is always
    older than any real candidate."""
    if not candidate:
        return False
    if not current:
        return True
    return parse_version(candidate) > parse_version(current)


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
    cands = list(d.glob("sdr-agent-*.tar.gz"))
    if not cands:
        return None
    # Pick the NEWEST by numeric version (from the sdr-agent-<version>.tar.gz name),
    # not the alphabetically-last filename — otherwise 1.0.9 beats 1.0.10.
    return max(cands, key=lambda p: (parse_version(_version_from_name(p)), p.name))


def _version_from_name(path: Path) -> str:
    """The <version> in sdr-agent-<version>.tar.gz (else the bare stem)."""
    name = path.name
    if name.startswith("sdr-agent-"):
        name = name[len("sdr-agent-"):]
    for suffix in (".tar.gz", ".tgz"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


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
