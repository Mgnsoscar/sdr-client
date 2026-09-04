# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SDR Broadcaster Control (the sdr-client GUI), --onedir.

Build (on Windows, from the repo root, in the project venv):
    pyinstaller sdr_client.spec

Produces  dist/SDR Broadcaster Control/  — a self-contained folder whose
"SDR Broadcaster Control.exe" runs with no Python installed. That folder is what
the per-user installer (packaging/installer.iss) wraps; see packaging/build.ps1
and docs/packaging-standalone.md for the full build + distribute flow.

onedir (not onefile) on purpose: a plain folder launches fast and trips AV /
SmartScreen less than a self-extracting onefile exe.

This spec is committed deliberately — the repo's .gitignore ignores *.spec, so it
was force-added — so the frozen build is reproducible.

Data-file destinations mirror the source tree (ui/assets/fonts, ui/assets, .)
because the app loads them via __file__-relative / resource_dir() paths, which
resolve inside PyInstaller's _MEIPASS at runtime.
"""
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH).resolve()          # SPECPATH: injected by PyInstaller
APP_NAME = "SDR Broadcaster Control"
ICON = ROOT / "ui" / "assets" / "app.ico"

# ── data files ───────────────────────────────────────────────────────────────
datas = [
    (str(ROOT / "ui" / "assets" / "fonts"), "ui/assets/fonts"),   # IBM Plex faces
    (str(ROOT / "ui" / "assets" / "app.ico"), "ui/assets"),       # window/runtime icon
    (str(ROOT / "ui" / "assets" / "app.png"), "ui/assets"),
    (str(ROOT / "units.yaml"), "."),        # starter, seeded into the data dir on first run
]
# Optional agent OTA bundle(s) for "Provision unit…" — collected only if present.
# Drop sdr-agent-<version>.tar.gz (from sdr-agent/deploy/build_bundle.sh) into
# bundles/ before building; agent_bundle.bundle_dir() finds it under _MEIPASS.
for tarball in sorted((ROOT / "bundles").glob("sdr-agent-*.tar.gz")):
    datas.append((str(tarball), "bundles"))

# pydantic v2 reads its own distribution metadata at runtime.
datas += copy_metadata("pydantic")

# ── hidden imports & native libraries ────────────────────────────────────────
# PyQt6.Qsci: the Scripts-tab code editor imports it lazily inside a try/except
# (ui/code_editor.py), so PyInstaller's static analysis can miss it.
hiddenimports = ["PyQt6.Qsci"]
# The known PyInstaller gotchas for this app — dynamic submodules and compiled
# extensions the import graph doesn't fully reach on its own. cryptography is
# collected whole because requirements.txt floats its version and paramiko
# optionally imports version-specific submodules (e.g. cryptography.hazmat.decrepit,
# added in cryptography 43) that a version-sensitive import graph would miss.
for pkg in ("zeroconf", "paramiko", "cryptography", "pydantic", "pydantic_core"):
    hiddenimports += collect_submodules(pkg)

binaries = collect_dynamic_libs("zeroconf")   # zeroconf's Cython _c/_utils extensions

# ── excludes (not imported by the client; keeps the bundle lean) ─────────────
excludes = ["tkinter", "numpy", "pytest", "_pytest"]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX compression tends to worsen AV/SmartScreen reputation
    console=False,           # windowed GUI — no console window on launch
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=str(ICON) if ICON.exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
