@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ======================================================================
REM  verify_health.bat -- ProMem combined tracker + upload health check
REM                       with auto-remediation.
REM
REM  Two modes:
REM    INTERACTIVE (default)        user double-clicks; verbose output,
REM                                 if not defined SILENT pauses at terminal states for review.
REM    SILENT  ("silent" arg)       unattended; no pauses (intended for
REM                                 scheduled-task invocation via the
REM                                 health_runner.bat wrapper that
REM                                 redirects all output to health.log).
REM
REM  Six checks (each remediates what it can):
REM    1. Install layout intact (venv + source + runner bats).
REM    2. Tracker + agent registered as schtasks OR HKCU\Run.
REM    3. tracker.db exists + python tracker process is alive +
REM       db freshness.  If process dead -> auto-resume via
REM       schtasks /Run, falling back to `start /B tracker_runner.bat`.
REM    4. Captured row counts (informational).
REM    5. OAuth state; runs `promem_agent init` only when interactive
REM       (silent mode can't drive a browser).
REM    6. Force an agent run; remediates 401 (re-init), network errors,
REM       prints failure summary with log path.
REM
REM  Exit codes:
REM    0  healthy (with or without new rows uploaded this run)
REM    1  unrecoverable (re-run setup.bat or fix network)
REM    2  user action needed (no captures yet OR OAuth required in silent mode)
REM ======================================================================

set "SILENT="
if /I "%~1"=="silent" set "SILENT=1"

set "INSTALL=%LOCALAPPDATA%\ProMem"
set "PY=%INSTALL%\.venv\Scripts\python.exe"
set "DB=%INSTALL%\tracker.db"
set "LOG=%INSTALL%\agent.log"
set "TMP_RUN=%TEMP%\promem_verify_run.txt"
set "TMP_STATUS=%TEMP%\promem_verify_status.txt"
set "TMP_COUNTS=%TEMP%\promem_verify_counts.txt"

REM `python -m promem_agent` resolves the package via cwd / PYTHONPATH —
REM the runner.bat that schtasks uses cd's to %INSTALL% first. We do the
REM same here (and also export PYTHONPATH as belt-and-suspenders) so this
REM script works regardless of where the user double-clicked it from.
if exist "%INSTALL%" cd /d "%INSTALL%"
set "PYTHONPATH=%INSTALL%"

echo.
echo ============================================================
echo  ProMem Upload Verifier
echo ============================================================

REM ─── 1. Install integrity ──────────────────────────────────────────────
echo.
echo -- 1. Install integrity --
if not exist "%INSTALL%" (
    echo   [-] ProMem not installed at %INSTALL%
    echo       Run setup.bat first.
    if not defined SILENT pause
    exit /b 1
)
if not exist "%PY%" (
    echo   [-] Python venv missing at %PY%
    echo       Re-run setup.bat to create it.
    if not defined SILENT pause
    exit /b 1
)
echo   [+] Install dir : %INSTALL%
echo   [+] Python venv : %PY%

REM ─── 2. Scheduled tasks / HKCU\Run keys ───────────────────────────────
echo.
echo -- 2. Startup mechanisms --
set "TRACKER_OK="
set "AGENT_OK="

schtasks /Query /TN "ProMem Tracker" /FO LIST >nul 2>&1
if not errorlevel 1 (
    set "TRACKER_OK=1"
    echo   [+] ProMem Tracker  : scheduled task registered
)
schtasks /Query /TN "ProMem Agent" /FO LIST >nul 2>&1
if not errorlevel 1 (
    set "AGENT_OK=1"
    echo   [+] ProMem Agent    : scheduled task registered
)

if not defined TRACKER_OK (
    reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Tracker" >nul 2>&1
    if not errorlevel 1 (
        set "TRACKER_OK=1"
        echo   [+] ProMem Tracker  : HKCU\Run fallback registered
    )
)
if not defined AGENT_OK (
    reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Agent" >nul 2>&1
    if not errorlevel 1 (
        set "AGENT_OK=1"
        echo   [+] ProMem Agent    : HKCU\Run fallback registered
    )
)

if not defined TRACKER_OK echo   [-] ProMem Tracker  : NOT registered
if not defined AGENT_OK   echo   [-] ProMem Agent    : NOT registered

if not defined TRACKER_OK if not defined AGENT_OK (
    echo.
    echo   Neither tracker nor agent is registered.
    echo   Re-run setup.bat ^(right-click -^> Run as administrator if needed^).
    if not defined SILENT pause
    exit /b 1
)

REM ─── 3. tracker.db existence + tracker process liveness ──────────────
echo.
echo -- 3. tracker --
if not exist "%DB%" (
    echo   [!] tracker.db not found at %DB%
    call :start_tracker
    timeout /t 60 /nobreak >nul
    if not exist "%DB%" (
        echo   [-] tracker.db still missing after start attempt. Tracker not running.
        echo       Re-run setup.bat to refresh runners.
        if not defined SILENT pause
        exit /b 1
    )
)
for %%I in ("%DB%") do echo   [+] tracker.db   : %%~zI bytes, last write %%~tI

REM Is the tracker python process actually alive? Use PowerShell + WMI
REM (CommandLine isn't visible to plain `tasklist`). 0 = not running, >=1 = running.
set "TRACKER_PROC=0"
for /f "delims=" %%C in ('powershell -NoProfile -Command "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue ^| Where-Object { $_.CommandLine -like '*src.agent.tracker*' }).Count" 2^>nul') do set "TRACKER_PROC=%%C"

if "!TRACKER_PROC!"=="0" (
    echo   [!] tracker python process is NOT running — attempting to resume...
    call :start_tracker
    timeout /t 30 /nobreak >nul
    REM Re-check after restart attempt.
    set "TRACKER_PROC=0"
    for /f "delims=" %%C in ('powershell -NoProfile -Command "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue ^| Where-Object { $_.CommandLine -like '*src.agent.tracker*' }).Count" 2^>nul') do set "TRACKER_PROC=%%C"
    if "!TRACKER_PROC!"=="0" (
        echo   [-] tracker still not running after restart attempt.
        echo       Try: schtasks /Run /TN "ProMem Tracker"
        echo       Or:  start "" /B "%INSTALL%\tracker_runner.bat"
        echo       Or re-run setup.bat to refresh the runners.
        if not defined SILENT pause
        exit /b 1
    )
    echo   [+] tracker process : RESUMED ^(!TRACKER_PROC! python process^(es^) running^)
) else (
    echo   [+] tracker process : alive ^(!TRACKER_PROC! python process^(es^) running^)
)

REM Quick freshness check: if tracker.db hasn't been touched in >5 min while a
REM tracker process is alive, that's odd — surface it as a soft warning.
REM (User idle >5min is normal — the tracker pauses then. So just informational.)
for /f "delims=" %%A in ('powershell -NoProfile -Command "[int](((Get-Date) - (Get-Item '%DB%').LastWriteTime).TotalSeconds)" 2^>nul') do set "DB_AGE_SEC=%%A"
if defined DB_AGE_SEC (
    if !DB_AGE_SEC! GTR 300 (
        echo   [.] tracker.db last write was !DB_AGE_SEC!s ago ^(>5min — user may be idle, or tracker is starved^)
    ) else (
        echo   [+] tracker.db   : last write !DB_AGE_SEC!s ago ^(active^)
    )
)

REM ─── 4. Captured rows ─────────────────────────────────────────────────
echo.
echo -- 4. Captured data --
"%PY%" -c "import sqlite3; c=sqlite3.connect(r'%DB%'); print(c.execute('SELECT COUNT(*) FROM context_1').fetchone()[0]); print(c.execute('SELECT COUNT(*) FROM context_2').fetchone()[0])" > "%TMP_COUNTS%" 2>nul
if errorlevel 1 (
    echo   [-] Could not read tracker.db.
    if not defined SILENT pause
    exit /b 1
)
set "N_SEGS="
set "N_FRAMES="
set /a _LINE=0
for /f "delims=" %%n in (%TMP_COUNTS%) do (
    set /a _LINE+=1
    if !_LINE!==1 set "N_SEGS=%%n"
    if !_LINE!==2 set "N_FRAMES=%%n"
)
del "%TMP_COUNTS%" 2>nul
echo   context_1      : !N_SEGS! segment(s)
echo   context_2      : !N_FRAMES! frame(s)

if "!N_SEGS!"=="0" (
    echo   [!] No segments captured yet. Wait 1-2 minutes after the tracker starts, then re-run.
    if not defined SILENT pause
    exit /b 2
)

REM ─── 5. OAuth / auth state ────────────────────────────────────────────
echo.
echo -- 5. OAuth / Supabase auth --
"%PY%" -m promem_agent status > "%TMP_STATUS%" 2>&1
findstr /C:"logged in as" "%TMP_STATUS%" >nul
if not errorlevel 1 (
    for /f "tokens=*" %%L in ('findstr /C:"logged in as" "%TMP_STATUS%"') do echo   [+] %%L
) else (
    findstr /C:"no refresh_token" "%TMP_STATUS%" >nul
    if not errorlevel 1 (
        echo   [!] No refresh token in Windows Credential Manager.
        if defined SILENT (
            echo   [-] Cannot run OAuth init in silent mode ^(needs browser^).
            echo       User must double-click verify_health.bat to re-auth interactively.
            del "%TMP_STATUS%" 2>nul
            exit /b 2
        )
        echo   [.] Running promem_agent init ^(opens browser^)...
        "%PY%" -m promem_agent init
        if errorlevel 1 (
            echo   [-] OAuth flow failed. Run manually:
            echo       "%PY%" -m promem_agent init
            if not defined SILENT pause
            exit /b 2
        )
        echo   [+] OAuth completed.
    ) else (
        echo   [!] Auth state unclear; status output:
        type "%TMP_STATUS%"
    )
)
del "%TMP_STATUS%" 2>nul

REM ─── 6. Force agent run ───────────────────────────────────────────────
echo.
echo -- 6. Force agent run (live upload to Supabase) --
"%PY%" -m promem_agent --verbose run > "%TMP_RUN%" 2>&1
set "RUN_EXIT=!errorlevel!"
type "%TMP_RUN%"

if "!RUN_EXIT!"=="0" (
    echo.
    findstr /R /C:"inserted=[0-9][0-9]*" "%TMP_RUN%" >nul
    if not errorlevel 1 (
        echo   [+] Upload OK — see "received=N inserted=M" line above.
    ) else (
        echo   [+] Agent run completed without errors.
    )
    del "%TMP_RUN%" 2>nul
    echo.
    if not defined SILENT pause
    exit /b 0
)

REM Failure path: 401 / auth expired
findstr /C:"401" "%TMP_RUN%" >nul
if not errorlevel 1 goto :reauth_retry
findstr /C:"re-running OAuth" "%TMP_RUN%" >nul
if not errorlevel 1 goto :reauth_retry

REM Failure path: network
findstr /C:"Network error" "%TMP_RUN%" >nul
if not errorlevel 1 goto :network_fail
findstr /C:"getaddrinfo" "%TMP_RUN%" >nul
if not errorlevel 1 goto :network_fail
findstr /C:"resolve host" "%TMP_RUN%" >nul
if not errorlevel 1 goto :network_fail
findstr /C:"Connection" "%TMP_RUN%" >nul
if not errorlevel 1 goto :network_fail

REM Failure path: unknown
echo.
echo   [-] Upload failed for an unrecognized reason.
echo       Open the log: notepad %LOG%
del "%TMP_RUN%" 2>nul
if not defined SILENT pause
exit /b 1

:reauth_retry
echo.
echo   [!] Auth expired ^(HTTP 401^). Re-running OAuth...
"%PY%" -m promem_agent init
if errorlevel 1 (
    echo   [-] Re-auth failed. Check Supabase service status.
    del "%TMP_RUN%" 2>nul
    if not defined SILENT pause
    exit /b 1
)
echo.
echo -- Retry after re-auth --
"%PY%" -m promem_agent --verbose run
if not errorlevel 1 (
    echo   [+] Upload resumed after re-auth.
    del "%TMP_RUN%" 2>nul
    if not defined SILENT pause
    exit /b 0
)
echo   [-] Upload still failing after re-auth. See log: notepad %LOG%
del "%TMP_RUN%" 2>nul
if not defined SILENT pause
exit /b 1

:network_fail
echo.
echo   [-] Network error reaching https://promem.fly.dev
echo       Check: corporate firewall, VPN, proxy settings, internet connection.
del "%TMP_RUN%" 2>nul
if not defined SILENT pause
exit /b 1

REM ─── Subroutine: start the tracker via the best available mechanism ──
REM Tries schtasks first (works if the schtask was registered); falls back
REM to launching tracker_runner.bat directly via `start /B` (works for the
REM HKCU\Run fallback case or when the schtask is missing entirely).
:start_tracker
schtasks /Query /TN "ProMem Tracker" /FO LIST >nul 2>&1
if not errorlevel 1 (
    echo   [.] Starting tracker via schtasks /Run "ProMem Tracker"...
    schtasks /Run /TN "ProMem Tracker" >nul 2>&1
    if not errorlevel 1 exit /b 0
)
if exist "%INSTALL%\tracker_runner.bat" (
    echo   [.] Starting tracker via tracker_runner.bat directly...
    start "ProMem Tracker" /B /MIN cmd /c "%INSTALL%\tracker_runner.bat"
    exit /b 0
)
echo   [!] No way to start the tracker — neither schtasks nor tracker_runner.bat available.
exit /b 1
