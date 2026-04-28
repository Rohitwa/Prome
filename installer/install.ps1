# =====================================================================
#  ProMem PowerShell installer — one-line bootstrap for Windows.
#
#  Usage:
#     iwr https://promem.fly.dev/install.ps1 -UseBasicParsing | iex
#
#  What it does:
#    1. Ensures Python 3.10+ is on this machine. If missing or only the
#       Microsoft Store stub is present, installs Python 3.12 via winget
#       in user scope. (This is the fix for the "stuck on Python" class
#       of errors that setup.bat can't always work around — winget
#       handles UAC + PATH + Store-alias displacement natively.)
#    2. Reads /agent/manifest to find the latest ProMem version.
#    3. Downloads the matching zip into %TEMP% and extracts it.
#    4. Hands off to setup.bat (which runs all the existing 11 install
#       phases — venv, pip, schtasks, OAuth, etc).
#    5. Surfaces the dashboard URL.
#
#  Why hand off to setup.bat rather than reimplementing in PS:
#    setup.bat has the most install-test mileage of anything we ship.
#    PowerShell here solves the SINGLE class of bugs setup.bat can't
#    (Python detection / install) and leaves the rest of the install to
#    the well-tested cmd flow. Smaller blast radius than a full rewrite.
# =====================================================================

$ErrorActionPreference = 'Stop'

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  ProMem PowerShell installer" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# --- 1. Locate Python -------------------------------------------------
function Find-RealPython {
    # Skip any path under \WindowsApps\ (Microsoft Store alias stub) at every
    # discovery step — those open a Store popup instead of running Python.
    $skip = '*WindowsApps*'

    # 1a. C:\Windows\py.exe (Python launcher, system-wide)
    $launcher = "$env:WINDIR\py.exe"
    if (Test-Path $launcher) {
        try {
            $exe = (& $launcher -3 -c "import sys; print(sys.executable)" 2>$null) -as [string]
            if ($exe -and (Test-Path $exe) -and ($exe -notlike $skip)) { return $exe }
        } catch {}
    }

    # 1b. py / python on PATH (filtering Store stubs)
    foreach ($cmd in 'py','python') {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike $skip }
        if ($found) {
            try {
                $arg = if ($cmd -eq 'py') { '-3' } else { '' }
                $exe = (& $found.Source $arg -c "import sys; print(sys.executable)" 2>$null) -as [string]
                if ($exe -and (Test-Path $exe) -and ($exe -notlike $skip)) { return $exe }
            } catch {}
        }
    }

    # 1c. Windows Registry (PEP 514) — HKLM and HKCU
    foreach ($hive in 'HKLM:','HKCU:') {
        $base = "$hive\Software\Python\PythonCore"
        if (Test-Path $base) {
            $hits = Get-ChildItem $base -ErrorAction SilentlyContinue
            foreach ($h in $hits) {
                $ip = "$($h.PSPath)\InstallPath"
                if (Test-Path $ip) {
                    $exe = (Get-ItemProperty $ip -Name ExecutablePath -ErrorAction SilentlyContinue).ExecutablePath
                    if ($exe -and (Test-Path $exe) -and ($exe -notlike $skip)) { return $exe }
                }
            }
        }
    }

    # 1d. Standard install dirs
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "C:\Program Files\Python3*\python.exe",
        "C:\Python3*\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe"
    )
    foreach ($pat in $patterns) {
        $hit = Get-Item $pat -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }

    return $null
}

function Test-PythonVersionOk($exe) {
    try {
        $v = (& $exe -c "import sys; print(sys.version_info[0]*100+sys.version_info[1])" 2>$null) -as [int]
        return ($v -ge 310)
    } catch { return $false }
}

Write-Host "[1/4] Locating Python 3.10+..." -ForegroundColor Yellow
$py = Find-RealPython
if ($py -and -not (Test-PythonVersionOk $py)) {
    Write-Host "      Found Python at $py but version is < 3.10. Will install 3.12 alongside." -ForegroundColor Yellow
    $py = $null
}

if (-not $py) {
    Write-Host "      Python 3.10+ not found. Installing 3.12 via winget (user scope)..." -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "      ERROR: winget is missing. Install it from https://aka.ms/getwinget OR install Python manually from https://www.python.org/downloads/" -ForegroundColor Red
        Start-Process "https://www.python.org/downloads/"
        throw "winget unavailable; cannot auto-install Python"
    }
    & winget install --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      WARNING: winget exit code $LASTEXITCODE; trying re-detect anyway..." -ForegroundColor Yellow
    }
    # Refresh PATH from registry so this session sees the new Python.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-RealPython
    if (-not $py) {
        throw "Python install via winget did not produce a discoverable interpreter. Install manually from python.org and re-run this command."
    }
}
Write-Host "      [+] Python: $py" -ForegroundColor Green

# --- 2. Resolve latest ProMem version --------------------------------
Write-Host ""
Write-Host "[2/4] Resolving latest ProMem version from manifest..." -ForegroundColor Yellow
$manifest = Invoke-RestMethod -Uri "https://promem.fly.dev/agent/manifest" -UseBasicParsing
$ver = $manifest.latest
$zipUrl = $manifest.url
Write-Host "      [+] Latest version: v$ver" -ForegroundColor Green
Write-Host "      [+] Zip URL: $zipUrl"

# --- 3. Download + extract -------------------------------------------
$tmpRoot = Join-Path $env:TEMP ("promem_install_" + $ver)
$zipPath = Join-Path $tmpRoot "promem_agent-$ver.zip"
$extractDir = Join-Path $tmpRoot "extracted"

Write-Host ""
Write-Host "[3/4] Downloading + extracting v$ver..." -ForegroundColor Yellow
if (Test-Path $tmpRoot) { Remove-Item $tmpRoot -Recurse -Force }
New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
Write-Host "      [+] Extracted to $extractDir" -ForegroundColor Green

# --- 4. Hand off to setup.bat ----------------------------------------
$setupBat = Join-Path $extractDir "setup.bat"
if (-not (Test-Path $setupBat)) {
    throw "setup.bat not found in extracted zip at $setupBat. Zip layout may have changed."
}

Write-Host ""
Write-Host "[4/4] Running setup.bat (installs venv, deps, schtasks, OAuth, health monitor)..." -ForegroundColor Yellow
Write-Host "      A new console window will open with the install steps. Wait for it to finish." -ForegroundColor Yellow
Write-Host ""
& cmd /c "`"$setupBat`""
$setupExit = $LASTEXITCODE

Write-Host ""
if ($setupExit -eq 0) {
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host "  ProMem v$ver installed successfully." -ForegroundColor Green
    Write-Host "==========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Dashboard:  https://promem.fly.dev/productivity"
    Write-Host "  Wiki:       https://promem.fly.dev/wiki"
    Write-Host ""
    Write-Host "  Health monitor runs every 15 min and auto-resumes the tracker"
    Write-Host "  if it stops. Manual diagnostic:"
    Write-Host "    `"$env:LOCALAPPDATA\ProMem\verify_health.bat`""
    Write-Host ""
} else {
    Write-Host "==========================================" -ForegroundColor Red
    Write-Host "  setup.bat exited with code $setupExit." -ForegroundColor Red
    Write-Host "  Check the console output above for the error." -ForegroundColor Red
    Write-Host "==========================================" -ForegroundColor Red
    exit $setupExit
}
