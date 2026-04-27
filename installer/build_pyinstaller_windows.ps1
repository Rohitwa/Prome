#requires -Version 5.1
<#
.SYNOPSIS
Builds Windows x64 PyInstaller payload binaries for ProMem installer.

.DESCRIPTION
Produces:
  installer\payload\bin\promem_agent\promem_agent.exe
  installer\payload\bin\promem_tracker\promem_tracker.exe

Run this on a Windows x64 machine. The resulting installer/payload directory
can then be used by installer/build.sh (makensis) to compile the final setup EXE.

Optional environment variables:
  PROMEM_TRACKER_SRC   External productivity-tracker source path.
                       Default keeps the current release-script value.

  PROMEM_TRACKER_ENTRY Optional explicit tracker entry script path.
                       Defaults to <PROMEM_TRACKER_SRC>\src\agent\tracker.py

  PROMEM_BUILD_PYTHON  Python executable to use (default: py -3.12, then py -3.11, then py -3, then python)

#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not [Environment]::Is64BitOperatingSystem) {
  throw "Windows x64 build host required."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot

$DefaultTrackerSrc = '/Users/rohitsingh/Desktop/memory/.claude/worktrees/tracker-act-monitor/productivity-tracker'
$TrackerSrc = if ($env:PROMEM_TRACKER_SRC) { $env:PROMEM_TRACKER_SRC } else { $DefaultTrackerSrc }
$TrackerEntry = if ($env:PROMEM_TRACKER_ENTRY) {
  $env:PROMEM_TRACKER_ENTRY
} else {
  Join-Path $TrackerSrc 'src\agent\tracker.py'
}

if (-not (Test-Path (Join-Path $RepoRoot 'promem_agent\__main__.py'))) {
  throw "promem_agent/__main__.py not found. Run from repo root."
}
if (-not (Test-Path $TrackerSrc)) {
  throw "Tracker source not found at '$TrackerSrc'. Set PROMEM_TRACKER_SRC."
}
if (-not (Test-Path $TrackerEntry)) {
  throw "Tracker entry script not found at '$TrackerEntry'. Set PROMEM_TRACKER_ENTRY if needed."
}

$PythonCmdParts = @()
if ($env:PROMEM_BUILD_PYTHON) {
  $PythonCmdParts = @($env:PROMEM_BUILD_PYTHON)
} else {
  try {
    py -3.12 -c "import sys" *> $null
    $PythonCmdParts = @('py', '-3.12')
  } catch {
    try {
      py -3.11 -c "import sys" *> $null
      $PythonCmdParts = @('py', '-3.11')
    } catch {
      try {
        py -3 -c "import sys" *> $null
        $PythonCmdParts = @('py', '-3')
      } catch {
        $PythonCmdParts = @('python')
      }
    }
  }
}

$PythonCmdDisplay = ($PythonCmdParts -join ' ')

$BuildRoot = Join-Path $RepoRoot 'build\pyinstaller_windows'
$VenvDir = Join-Path $BuildRoot '.venv'
$DistDir = Join-Path $BuildRoot 'dist'
$WorkDir = Join-Path $BuildRoot 'work'
$SpecDir = Join-Path $BuildRoot 'spec'
$PayloadDir = Join-Path $RepoRoot 'installer\payload'
$PayloadBinDir = Join-Path $PayloadDir 'bin'

Write-Host "== ProMem PyInstaller payload build ==" -ForegroundColor Cyan
Write-Host "Repo root      : $RepoRoot"
Write-Host "Python command : $PythonCmdDisplay"
Write-Host "Tracker source : $TrackerSrc"
Write-Host "Tracker entry  : $TrackerEntry"

if (Test-Path $BuildRoot) {
  Remove-Item -Recurse -Force $BuildRoot
}
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

Write-Host "Creating build virtual environment..."
if ($PythonCmdParts.Count -eq 1) {
  & $PythonCmdParts[0] -m venv $VenvDir
} else {
  $pythonExe = $PythonCmdParts[0]
  $pythonArgs = $PythonCmdParts[1..($PythonCmdParts.Count - 1)]
  & $pythonExe @pythonArgs -m venv $VenvDir
}

$PyExe = Join-Path $VenvDir 'Scripts\python.exe'
$PipExe = Join-Path $VenvDir 'Scripts\pip.exe'
if (-not (Test-Path $PyExe) -or -not (Test-Path $PipExe)) {
  throw "Failed to create venv at $VenvDir"
}
$PyBits = (& $PyExe -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($PyBits -ne '64') {
  throw "Python interpreter is ${PyBits}-bit; use 64-bit Python for x64 payload builds."
}

Write-Host "Installing build dependencies..."
& $PipExe install --upgrade pip
& $PipExe install pyinstaller
& $PipExe install -r (Join-Path $RepoRoot 'requirements-agent.txt')
& $PipExe install $TrackerSrc

Write-Host "Building promem_agent.exe (onedir)..."
& $PyExe -m PyInstaller `
  --noconfirm --clean --onedir `
  --name promem_agent `
  --distpath $DistDir `
  --workpath $WorkDir `
  --specpath $SpecDir `
  --collect-submodules keyring.backends `
  --collect-submodules jwt `
  (Join-Path $RepoRoot 'promem_agent\__main__.py')

Write-Host "Building promem_tracker.exe (onedir)..."
& $PyExe -m PyInstaller `
  --noconfirm --clean --onedir `
  --name promem_tracker `
  --distpath $DistDir `
  --workpath $WorkDir `
  --specpath $SpecDir `
  $TrackerEntry

$AgentOut = Join-Path $DistDir 'promem_agent\promem_agent.exe'
$TrackerOut = Join-Path $DistDir 'promem_tracker\promem_tracker.exe'
if (-not (Test-Path $AgentOut)) {
  throw "PyInstaller output missing: $AgentOut"
}
if (-not (Test-Path $TrackerOut)) {
  throw "PyInstaller output missing: $TrackerOut"
}

Write-Host "Staging installer payload under installer/payload ..."
if (Test-Path $PayloadDir) {
  Remove-Item -Recurse -Force $PayloadDir
}
New-Item -ItemType Directory -Force -Path (Join-Path $PayloadBinDir 'promem_agent') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PayloadBinDir 'promem_tracker') | Out-Null

Copy-Item -Recurse -Force (Join-Path $DistDir 'promem_agent\*') (Join-Path $PayloadBinDir 'promem_agent')
Copy-Item -Recurse -Force (Join-Path $DistDir 'promem_tracker\*') (Join-Path $PayloadBinDir 'promem_tracker')

$Version = & $PyExe -c "import re,pathlib; s=pathlib.Path('promem_agent/__init__.py').read_text(); m=re.search(r'__version__\s*=\s*\"([^\"]+)\"', s); print(m.group(1) if m else 'unknown')"
Set-Content -Path (Join-Path $PayloadDir 'VERSION.txt') -Value $Version.Trim()

Write-Host ""
Write-Host "Payload build complete." -ForegroundColor Green
Write-Host "  Agent exe  : $AgentOut"
Write-Host "  Tracker exe: $TrackerOut"
Write-Host "  Staged at  : $PayloadDir"
Write-Host ""
Write-Host "Next step (macOS/Linux with makensis installed):"
Write-Host "  ./installer/build.sh"
