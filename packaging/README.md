# Building the Windows app

Quick reference for building **SDR Broadcaster Control** into a per-user Windows
installer (and a portable ZIP). No admin rights are needed at any step. The full
guide — how the freeze is put together, distribution and the unknown-publisher
mitigations — is in [`../docs/packaging-standalone.md`](../docs/packaging-standalone.md).

## The command

Open **PowerShell** in the repo root (the folder that contains `sdr_client.spec`)
and run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

That's the whole build. It creates an isolated build venv, installs the
requirements + PyInstaller, freezes the app with `sdr_client.spec`, zips a
portable copy, and — if Inno Setup is installed — compiles the installer.

When it finishes it prints the two artifacts:

```
dist\SDR-Broadcaster-Control-<ver>-portable.zip          (portable, no install)
packaging\Output\SDR-Broadcaster-Control-<ver>-Setup.exe (per-user installer)
```

## Useful variations

```powershell
# From-scratch build (wipes build\, dist\ and the build venv first)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Clean

# Stop after the frozen folder + portable ZIP; skip the installer
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller

# Build with a specific Python (use 3.11 or 3.12 — mature PyQt6 wheels)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python "C:\Python312\python.exe"
```

## Prerequisites

- **Python 3.11 or 3.12**, per-user, from [python.org](https://www.python.org/downloads/windows/)
  (the script finds it via the `py` launcher or `python` on PATH).
- **Inno Setup 6.3+** (free, per-user) from <https://jrsoftware.org/isdl.php> — only
  needed for the `.exe` installer. Without it the build still produces the portable
  ZIP and tells you how to compile the installer later.
- **`bundles\sdr-agent-*.tar.gz`** — optional. Drop the tarball from
  `sdr-agent\deploy\build_bundle.sh` here so the built app's *Provision unit…*
  feature has an agent to deploy. The build warns (doesn't fail) if it's missing.

## Where the version comes from

The version stamped on both artifacts is read from `AppVersion` in
[`installer.iss`](installer.iss). Bump it there and the ZIP + installer names follow.
