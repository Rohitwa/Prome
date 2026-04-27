# ProMem Windows Installer Build Guide

This guide is for the **build machine** (Windows x64), not end users.

End users only run:

`promem_setup_<version>_x64.exe`

They do not need Python or NSIS installed.

## 1) Prerequisites (Build Machine)

- Windows x64
- Git
- Python 3.12 x64 with `py` launcher
- NSIS (`makensis` on PATH)

## 2) Install Python 3.12

Install:

```powershell
winget install Python.Python.3.12
```

Verify:

```powershell
py -3.12 -V
```

## 3) Install NSIS

Install:

```powershell
winget install NSIS.NSIS
```

Verify:

```powershell
makensis /VERSION
```

If `makensis` is not recognized, add NSIS to user PATH:

```powershell
$nsis = "C:\Program Files (x86)\NSIS"
if (-not (Test-Path $nsis)) { $nsis = "C:\Program Files\NSIS" }

$userPath = [Environment]::GetEnvironmentVariable("Path","User")
if (-not (($userPath -split ';') -contains $nsis)) {
  [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";" + $nsis).Trim(';'), "User")
}
```

Close and reopen terminal, then run:

```powershell
makensis /VERSION
```

## 4) Clone Repo and Checkout Branch

```powershell
git clone https://github.com/YantrAILabs/Promem.git
cd Promem
git checkout feature/windows-single-exe-installer-silent
git pull
```

## 5) Build PyInstaller Payload (Windows)

Set tracker source path first:

```powershell
$env:PROMEM_TRACKER_SRC = "C:\path\to\productivity-tracker"
```

Optional override if tracker entry script differs:

```powershell
$env:PROMEM_TRACKER_ENTRY = "C:\path\to\productivity-tracker\src\agent\tracker.py"
```

Build payload:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build_pyinstaller_windows.ps1
```

Expected output folders:

- `installer\payload\bin\promem_agent\promem_agent.exe`
- `installer\payload\bin\promem_tracker\promem_tracker.exe`

## 6) Build Final NSIS Setup EXE

```powershell
$version = py -3.12 -c "from promem_agent import __version__; print(__version__)"
cd installer
makensis /DAGENT_VERSION=$version setup.nsi
```

Expected output:

- `installer\promem_setup_<version>_x64.exe`

## 7) Quick Verification on Windows

Run installer EXE and complete login in browser.

Then verify:

```bat
cd /d "%LOCALAPPDATA%\ProMem"
bin\promem_agent\promem_agent.exe status
schtasks /Query /TN "ProMem Agent"
schtasks /Query /TN "ProMem Tracker"
```

Trigger upload now (optional):

```bat
schtasks /Run /TN "ProMem Agent"
```

Check:

- `%LOCALAPPDATA%\ProMem\agent.log`
- Supabase table `tracker_segments` for new rows

## 8) Notes

- Install is per-user (`%LOCALAPPDATA%\ProMem`) and does not require admin rights.
- Installer flow is: login first, then scheduled task creation, then immediate first upload check.
- Scheduled runs are hidden (no command prompt popups).

## 9) Troubleshooting

- `py -3.12` not found:
  - Reinstall Python 3.12 and ensure launcher is included.
- `makensis` not found:
  - Reopen terminal after PATH update, or verify NSIS install directory.
- Payload build fails with tracker path error:
  - Set `PROMEM_TRACKER_SRC` correctly and confirm file exists.
- Installer login times out:
  - Re-run installer and complete Google login in the opened browser tab.
