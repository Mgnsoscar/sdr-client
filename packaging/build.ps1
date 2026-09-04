<#
.SYNOPSIS
    Build SDR Broadcaster Control into a per-user Windows installer (and a
    portable ZIP), from a fresh checkout on a Windows PC. No admin required.

.DESCRIPTION
    Runs the whole freeze + package pipeline:
      1. create an isolated build venv (.venv-build)
      2. pip install -r requirements.txt + pyinstaller
      3. pyinstaller sdr_client.spec  ->  dist\SDR Broadcaster Control\
      4. zip that folder              ->  dist\<name>-<ver>-portable.zip  (fallback)
      5. compile packaging\installer.iss with Inno Setup (if ISCC is found)
                                        ->  packaging\Output\...-Setup.exe

    Everything here is per-user; nothing needs administrator rights.

.PARAMETER Clean
    Remove build\ dist\ and the build venv first, for a from-scratch build.

.PARAMETER SkipInstaller
    Stop after the frozen folder + ZIP; don't run Inno Setup.

.PARAMETER Python
    Path to the Python launcher/exe to build with (default: 'py' then 'python').
    Use Python 3.11 or 3.12 - versions with mature PyQt6 wheels.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Clean

.NOTES
    This file is deliberately pure ASCII. Windows PowerShell 5.1 reads a .ps1
    saved as UTF-8-without-BOM using the system ANSI code page, which mangles any
    non-ASCII characters (dashes, ellipses) and breaks parsing - so keep it ASCII.
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstaller,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

# ---- Resolve paths (this script lives in <repo>\packaging) ------------------
$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo         = Split-Path -Parent $PackagingDir
$VenvDir      = Join-Path $Repo ".venv-build"
$VenvPy       = Join-Path $VenvDir "Scripts\python.exe"
$DistName     = "SDR Broadcaster Control"
$DistDir      = Join-Path $Repo "dist\$DistName"

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "WARNING: $m" -ForegroundColor Yellow }

Info "Repo root: $Repo"
Set-Location $Repo

# ---- Pick a Python to bootstrap the venv ------------------------------------
# Returns the launcher as an argv array so a space in the path (or the 'py -3'
# launcher) is passed correctly, never string-concatenated into a command line.
function Resolve-PythonCmd {
    if ($Python) { return @($Python) }
    if (Get-Command "py" -ErrorAction SilentlyContinue) { return @("py", "-3") }
    if (Get-Command "python" -ErrorAction SilentlyContinue) { return @("python") }
    throw "No Python found on PATH. Install Python 3.11 or 3.12 (per-user, from python.org) and re-run."
}

if ($Clean) {
    Info "Clean: removing build\, dist\, $VenvDir"
    foreach ($p in @("build", "dist", $VenvDir)) {
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
}

# ---- 1. Build venv ----------------------------------------------------------
if (-not (Test-Path $VenvPy)) {
    $pyCmd = Resolve-PythonCmd
    $exe = $pyCmd[0]
    $rest = @(); if ($pyCmd.Count -gt 1) { $rest = $pyCmd[1..($pyCmd.Count - 1)] }
    Info "Creating build venv at $VenvDir (using: $($pyCmd -join ' '))"
    & $exe @rest -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) { throw "venv creation failed: $VenvPy not found" }
}

Info "Python: $(& $VenvPy --version)"
Info "Upgrading pip / wheel"
& $VenvPy -m pip install --upgrade pip wheel | Out-Host

Info "Installing runtime requirements + PyInstaller"
& $VenvPy -m pip install -r (Join-Path $Repo "requirements.txt") | Out-Host
& $VenvPy -m pip install pyinstaller | Out-Host

# ---- Provisioning bundle check (Provision unit... is in scope) --------------
$bundles = Get-ChildItem (Join-Path $Repo "bundles") -Filter "sdr-agent-*.tar.gz" -ErrorAction SilentlyContinue
if (-not $bundles) {
    Warn "No bundles\sdr-agent-*.tar.gz found - the built app will run, but the"
    Warn "  'Provision unit...' feature will have no agent to deploy. Drop the"
    Warn "  tarball from sdr-agent\deploy\build_bundle.sh into bundles\ to include it."
} else {
    Info "Including agent bundle: $($bundles[-1].Name)"
}

# ---- 2. Freeze --------------------------------------------------------------
Info "Running PyInstaller (sdr_client.spec, --onedir)"
& $VenvPy -m PyInstaller (Join-Path $Repo "sdr_client.spec") --noconfirm --clean | Out-Host
if (-not (Test-Path (Join-Path $DistDir "$DistName.exe"))) {
    throw "PyInstaller did not produce $DistDir\$DistName.exe"
}
Info "Frozen app: $DistDir"

# ---- 3. Portable ZIP (the no-installer fallback) ----------------------------
# Read the version straight from the .iss so ZIP + installer names agree.
$iss = Get-Content (Join-Path $PackagingDir "installer.iss") -Raw
$ver = if ($iss -match '#define\s+AppVersion\s+"([^"]+)"') { $Matches[1] } else { "1.0.0" }
$zip = Join-Path $Repo "dist\SDR-Broadcaster-Control-$ver-portable.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Info "Zipping portable build -> $zip"
Compress-Archive -Path $DistDir -DestinationPath $zip

# ---- 4. Installer (Inno Setup) ----------------------------------------------
if ($SkipInstaller) {
    Info "SkipInstaller set - done. Portable ZIP is at $zip"
    return
}

function Resolve-ISCC {
    $c = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if (-not $base) { continue }
        $p = Join-Path $base "Inno Setup 6\ISCC.exe"
        if (Test-Path $p) { return $p }
    }
    return $null
}

$iscc = Resolve-ISCC
if (-not $iscc) {
    Warn "Inno Setup (ISCC.exe) not found - skipping the installer."
    Warn "  Install Inno Setup 6.3+ (free, per-user) from https://jrsoftware.org/isdl.php"
    Warn "  then re-run, or compile manually:  ISCC.exe packaging\installer.iss"
    Info "Portable ZIP is ready at $zip"
    return
}

Info "Compiling installer with $iscc"
& $iscc (Join-Path $PackagingDir "installer.iss") | Out-Host
$setup = Join-Path $PackagingDir "Output\SDR-Broadcaster-Control-$ver-Setup.exe"
if (Test-Path $setup) {
    Info "DONE."
    Write-Host "  Installer:  $setup"       -ForegroundColor Green
    Write-Host "  Portable:   $zip"          -ForegroundColor Green
} else {
    Warn "ISCC ran but the expected installer was not found at $setup"
}
