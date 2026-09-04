# Packaging sdr-client as a standalone, no-admin, self-installed app — context handoff

**Status:** NOT STARTED. This document is *context only* — it gathers the repo facts, the hard
constraints, the one real code change the app needs, and the open decisions, so a fresh session can
start the packaging work without re-deriving any of it. It deliberately does **not** commit to a
build tool or write a build script yet.

**Goal (owner's words):** an installer the owner can send to coworkers so they install this as a
desktop application. Two hard constraints from the company's security posture:

1. **No administrator rights.** The installer and the app must install and run entirely per-user —
   no elevation, no writing to `Program Files`, no machine-wide registry, no service install.
2. **Unknown-publisher execution is often blocked.** Coworkers frequently *cannot* run executables
   from an unknown publisher (Windows SmartScreen / "Windows protected your PC"; on macOS,
   Gatekeeper). The packaging plan has to have an honest answer for this, not assume a click-through
   is always available.

---

## 1. What the app is (facts from this repo)

- **`sdr-client`** is a **PyQt6 desktop GUI** (the fleet-control + calibration authoring app). It is
  the thing to package. It talks to on-unit agents over **HTTP + mDNS**; it does **not** need
  `sdr-agent` or `sdr-scripts` present to run.
- **Entry point:** `main.py` at the repo root — `python main.py`, `def main()`,
  `QApplication(...)`, `app.setApplicationName("SDR Broadcaster Control")`. That application name is
  the natural product name for the installer / shortcut / per-user data folder.
- **Build Python in this container:** 3.11.15. `requirements.txt` deliberately floats versions
  (`>=`) to avoid "no wheel for Python 3.x, compile from source" — so pin the *build* interpreter to
  a version with mature PyQt6 wheels (3.11 or 3.12 are safe; avoid the newest release until PyQt6
  ships wheels for it).
- **Runtime dependencies** (`requirements.txt`, the real client set — the larger list in `CLAUDE.md`
  is for running the agent-side tests, NOT for the app):
  - `PyQt6>=6.7` — Qt6; the big native dependency (Qt plugins, `platforms/` etc. must be collected).
  - `PyQt6-QScintilla>=2.14` — native; the Scripts-tab code editor. **Optional** — the app falls
    back to a plain editor if it's missing, so it can be dropped to shrink/simplify the bundle.
  - `httpx>=0.27`, `pydantic>=2.9`, `zeroconf>=0.131` (mDNS discovery), `websocket-client>=1.8`,
    `PyYAML>=6.0`, `paramiko>=3.4` (SSH; pulls in `cryptography`, a native wheel).
  - No `numpy` at client runtime, no `inotify-simple` (that's agent-only) — confirmed by import scan.
- **Data files that must be collected into the bundle:**
  - `ui/assets/fonts/*.ttf` — the IBM Plex faces. `ui/theme.py::_load_fonts` loads them from
    `Path(__file__).resolve().parent / "assets" / "fonts"` and **falls back to system fonts if
    absent**, so a bundle that forgets them still runs but looks wrong. Collect them.
  - `bundles/sdr-agent-*.tar.gz` — **optional.** Only needed for the "Provision unit…" feature
    (SSH-bootstrap a fresh Pi). `state/agent_bundle.py` already understands a frozen app: it uses
    `sys.frozen` / `sys._MEIPASS` and also honours a `SDR_AGENT_BUNDLE` env override. If provisioning
    is in scope for coworkers, ship a tarball under `bundles/`; otherwise the feature simply no-ops.
- **No application icon asset exists** (only fonts under `ui/assets/`). A packaging pass should add
  one (`.ico` for Windows, `.icns` for macOS, a PNG for Linux) for the window, taskbar, shortcut and
  installer. `QIcon(` today is only used for standard icons in `ui/scripts_panel.py`.
- `G_ICD.pdf` (~5 MB) at the repo root is a reference document, **not** an app asset — exclude it.

## 2. The one real code change the app needs first: writable state location

**This is the load-bearing prerequisite and should be done before any freeze.** Every config/state
file is currently written *next to the source* via `Path(__file__).parent` / `parents[1]`. In a
frozen, per-user-installed app the code lives in a read-only (or temp-extracted `_MEIPASS`) location,
so these writes must move to a **per-user writable data directory** (e.g. `%APPDATA%\SDR Broadcaster
Control\` on Windows, `~/Library/Application Support/…` on macOS, `$XDG_CONFIG_HOME` on Linux —
`QStandardPaths.AppDataLocation` gives all three for free).

Files/locations to relocate (all resolve relative to source today):

| File | Defined in |
|------|-----------|
| `units.yaml` | `config.py::DEFAULT_UNITS_FILE` |
| `plans.json` | `state/plan_store.py` |
| `schedule.json` | `state/schedule_store.py` |
| `library.json` | `state/library_store.py` |
| `components.json` | `state/component_catalog.py` |
| `address_cache.json` | `state/address_cache.py` |
| `calibration_cache.json` | `state/calibration_cache.py` |
| `unit_ledger.json` | `state/unit_ledger.py` |

Suggested shape (for the fresh session to decide/implement): a single `paths.py` helper — e.g.
`data_dir()` returning a per-user dir, honouring an env override (`SDR_CLIENT_DATA_DIR`) so dev runs
keep using the repo, and seeding first-run defaults (a starter `units.yaml`) if none exists. Then
each store's `DEFAULT_*` reads from `data_dir()`. Keep it backward-compatible for the dev workflow
(tests write into temp dirs / the repo today — don't break them). `state/agent_bundle.py` already
shows the `sys.frozen`/`_MEIPASS` pattern to copy.

## 3. Open decisions the fresh session must confirm with the owner FIRST

These change the whole approach, so ask before building:

- **Target OS.** The constraints ("run as administrator", "executables from unknown publishers")
  are Windows-flavoured (SmartScreen). Confirm: **Windows only**, or also macOS / Linux? The dev/CI
  box is Linux; coworkers are presumably Windows. Everything below assumes Windows-primary.
- **Windows version + architecture** coworkers run (Win10/11, x64/arm64) — sets the build target.
- **Is ANY code-signing certificate available?** This is the single biggest lever on the
  unknown-publisher problem (see §5). Options to probe: an existing company Authenticode cert, an
  OV/EV cert the owner could buy, or none. If none, the plan must lean on the no-signing mitigations.
- **Is "Provision unit…" (SSH bootstrap) in scope** for coworkers? If yes, an agent tarball must be
  bundled (§1); if no, drop it and skip `paramiko`/`bundles` weight.
- **Build/CI host.** A Windows app is cleanest built on Windows (native PyInstaller). Is there a
  Windows machine / CI runner, or must it cross-build? (Cross-building a Windows PyInstaller exe from
  Linux is not supported; Wine is fragile. Plan for a Windows build step.)

## 4. Freezing approach (menu — do not pre-commit)

- **PyInstaller** is the most likely fit for a PyQt6 app (mature Qt hooks). Prefer **`--onedir`**
  over `--onefile`: onefile extracts to a temp dir on every launch (slow, and more likely to trip
  AV/security tooling); onedir is a plain folder that pairs naturally with a per-user installer.
  Keep the `.spec` in version control (note: `*.spec` is currently git-ignored — force-add it).
- Alternatives to weigh: **Nuitka** (compiles to C; sometimes better AV reputation, heavier build),
  **cx_Freeze**. Briefcase/PyOxidizer are less battle-tested for PyQt6.
- Collect: the Qt plugins (`platforms/`, `styles/`, `imageformats/`), `ui/assets/fonts/`, and (if in
  scope) `bundles/`. Verify `zeroconf`, `paramiko`/`cryptography`, `pydantic` v2 all get their hidden
  imports / native libs — these three are the usual PyInstaller gotchas here.
- Smoke-test the frozen app on a CLEAN machine (no Python, no Qt installed) before anything else.

## 5. No-admin install + unknown-publisher — the honest options

**Per-user, no-admin install (Windows):**
- **Inno Setup** with `PrivilegesRequired=lowest` installing into `%LOCALAPPDATA%\Programs\…` with a
  Start-menu + desktop shortcut, per-user uninstall entry. Mature, free, scriptable. Most likely pick.
- **A plain ZIP** of the onedir build + a "create shortcut" step (no installer at all) — simplest,
  fully no-admin, but no uninstall/Start-menu polish.
- **MSIX** per-user — modern, clean per-user story, but signing requirements are stricter and
  enterprise deployment policy may block sideloading; probably not worth it unless the company
  already uses MSIX.
- Do **not** use an MSI/WiX per-machine install (wants admin) or install a service.

**Unknown-publisher / SmartScreen (the real blocker):**
- **The only robust fix is code signing.** An **OV** Authenticode cert signs the exe/installer but
  still needs to *build reputation* before SmartScreen goes quiet; an **EV** cert gets instant
  SmartScreen reputation but is pricier and needs a hardware token. This depends entirely on §3's
  cert question.
- **Without a cert**, be honest that there is no way to make Windows treat it as a known publisher.
  Mitigations to document for coworkers/IT rather than pretend around:
  - SmartScreen "More info → Run anyway" (may be disabled by company policy — confirm).
  - Ship the **onedir folder + shortcut** (an unsigned *folder of files* launched by a shortcut trips
    SmartScreen less than a downloaded unsigned *installer .exe*, though it is not a guarantee).
  - Ask IT to **allow-list** the app's hash/path, or distribute via the company's software portal /
    Intune — often the sanctioned route in a locked-down shop, and it sidesteps SmartScreen entirely.
  - Self-signed certs do **not** help (trusting one needs admin) — don't propose them.
- **macOS** (if in scope): Gatekeeper wants a Developer-ID signature **and notarization**; an
  unsigned `.app` needs right-click-Open or `xattr -d com.apple.quarantine`. Per-user `.app` in
  `~/Applications` needs no admin. Same "signing is the real fix" story.

## 6. First steps for the fresh session (suggested order)

1. Confirm §3 decisions with the owner (OS, arch, signing cert, provisioning scope, build host).
2. Relocate writable state to a per-user data dir (§2) with an env override; keep tests green.
3. Freeze a `--onedir` PyInstaller build; smoke-test on a clean Windows VM.
4. Wrap it in a per-user (no-admin) installer (Inno Setup `PrivilegesRequired=lowest`, or a ZIP +
   shortcut) and add an app icon.
5. Address unknown-publisher per the cert decision (sign, or document the IT-allow-list/portal route).

**Guardrails carried over:** the calibration/temporal-power work is client-only; nothing here should
touch the drift-guarded files (`api/argspec.py`, `api/ramp.py`, `state/power_law.py`) or the
agent/scripts. Packaging is additive.
