@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ======================================================================
REM  verify_upload.bat — ProMem upload health check + auto-remediation.
REM
REM  What it does, in order:
REM    1. Confirms install layout (venv, source, runner bats).
REM    2. Confirms scheduled tasks (or HKCU\Run fallback) are registered.
REM    3. Confirms tracker.db exists; if not, starts the tracker task and waits.
REM    4. Counts captured rows so we know there is something to upload.
REM    5. Confirms OAuth is alive; re-runs promem_agent init if not.
REM    6. Forces an agent run, parses the result, and remediates failures:
REM        - 401 auth expired       -> re-runs OAuth, retries
REM        - Network error          -> firewall/VPN hint
REM        - Other                  -> points to agent.log
REM
REM  Exit codes:
REM    0  upload working (with or without new rows uploaded this run)
REM    1  unrecoverable (re-run setup.bat or fix network)
REM    2  user action needed (waited but no captures yet, or OAuth required)
REM
REM  Usage: double-click this file, OR run:
REM    "%LOCALAPPDATA%\ProMem\verify_upload.bat"
REM ======================================================================

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
    pause
    exit /b 1
)
if not exist "%PY%" (
    echo   [-] Python venv missing at %PY%
    echo       Re-run setup.bat to create it.
    pause
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
    pause
    exit /b 1
)

REM ─── 3. tracker.db existence ──────────────────────────────────────────
echo.
echo -- 3. tracker.db --
if not exist "%DB%" (
    echo   [!] tracker.db not found at %DB%
    if defined TRACKER_OK (
        echo   [.] Starting ProMem Tracker task and waiting 60 seconds for first capture...
        schtasks /Run /TN "ProMem Tracker" >nul 2>&1
        timeout /t 60 /nobreak >nul
    )
    if not exist "%DB%" (
        echo   [-] tracker.db still missing. Tracker not running.
        echo       Re-run setup.bat to refresh runners.
        pause
        exit /b 1
    )
)
for %%I in ("%DB%") do echo   [+] tracker.db   : %%~zI bytes, last write %%~tI

REM ─── 4. Captured rows ─────────────────────────────────────────────────
echo.
echo -- 4. Captured data --
"%PY%" -c "import sqlite3; c=sqlite3.connect(r'%DB%'); print(c.execute('SELECT COUNT(*) FROM context_1').fetchone()[0]); print(c.execute('SELECT COUNT(*) FROM context_2').fetchone()[0])" > "%TMP_COUNTS%" 2>nul
if errorlevel 1 (
    echo   [-] Could not read tracker.db.
    pause
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
    pause
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
        echo   [.] Running promem_agent init ^(opens browser^)...
        "%PY%" -m promem_agent init
        if errorlevel 1 (
            echo   [-] OAuth flow failed. Run manually:
            echo       "%PY%" -m promem_agent init
            pause
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
    pause
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
pause
exit /b 1

:reauth_retry
echo.
echo   [!] Auth expired ^(HTTP 401^). Re-running OAuth...
"%PY%" -m promem_agent init
if errorlevel 1 (
    echo   [-] Re-auth failed. Check Supabase service status.
    del "%TMP_RUN%" 2>nul
    pause
    exit /b 1
)
echo.
echo -- Retry after re-auth --
"%PY%" -m promem_agent --verbose run
if not errorlevel 1 (
    echo   [+] Upload resumed after re-auth.
    del "%TMP_RUN%" 2>nul
    pause
    exit /b 0
)
echo   [-] Upload still failing after re-auth. See log: notepad %LOG%
del "%TMP_RUN%" 2>nul
pause
exit /b 1

:network_fail
echo.
echo   [-] Network error reaching https://promem.fly.dev
echo       Check: corporate firewall, VPN, proxy settings, internet connection.
del "%TMP_RUN%" 2>nul
pause
exit /b 1
