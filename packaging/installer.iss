; Inno Setup script — per-user, NO-ADMIN installer for SDR Broadcaster Control.
;
; Wraps the PyInstaller --onedir output (dist\SDR Broadcaster Control\) into a
; single Setup.exe that installs entirely per-user:
;   * PrivilegesRequired=lowest  — never prompts for admin, never elevates.
;   * installs into %LOCALAPPDATA%\Programs\SDR Broadcaster Control
;   * per-user Start-menu (and optional desktop) shortcut + per-user uninstall.
; No service, no machine-wide registry, no writes to Program Files.
;
; Build it with Inno Setup 6.3+ (free, itself installable per-user):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; or just let packaging\build.ps1 find and run ISCC for you.
;
; NOTE ON SIGNING: this Setup.exe is UNSIGNED. Windows SmartScreen will warn
; ("unknown publisher") on a freshly-downloaded copy. There is no cert here to
; fix that — see docs\packaging-standalone.md §"Unknown publisher / SmartScreen"
; for the honest distribution options (IT allow-list / software portal, the
; onedir ZIP + shortcut route, "More info -> Run anyway"). If a code-signing
; cert is obtained later, add a [Setup] SignTool + sign the exe/installer.

#define AppName "SDR Broadcaster Control"
#define AppVersion "1.0.0"
#define AppPublisher "SDR Broadcaster"
#define AppExeName "SDR Broadcaster Control.exe"
; The PyInstaller onedir output, relative to this script (packaging\).
#define DistDir "..\dist\SDR Broadcaster Control"

[Setup]
; A stable, unique id for this app — keeps upgrades/uninstall coherent. Do NOT
; change it across releases.
AppId={{E0801016-8FBC-4D7C-A81A-BCBCAF4A0C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}

; ── Per-user, no-admin install ───────────────────────────────────────────────
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; ── Target ───────────────────────────────────────────────────────────────────
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; ── Output + look ────────────────────────────────────────────────────────────
OutputDir=Output
OutputBaseFilename=SDR-Broadcaster-Control-{#AppVersion}-Setup
SetupIconFile=..\ui\assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; The whole frozen onedir folder (the .exe, _internal\, everything).
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; The installer removes only what it installed (the program folder + shortcuts).
; The user's own data in %APPDATA%\SDR Broadcaster Control (units.yaml, plans,
; caches) is intentionally left behind on uninstall, so a reinstall keeps it.
