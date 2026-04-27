@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ======================================================================
REM  ProMem installer (Windows .bat)
REM
REM  Installs both:
REM    - productivity-tracker  (captures screenshots -> tracker.db, local)
REM    - promem_agent          (uploads tracker.db.context_1 -> cloud, every 5 min)
REM
REM  Per-user install at %LOCALAPPDATA%\ProMem; no admin rights needed.
REM  Run by double-clicking, after extracting the zip.
REM ======================================================================

set "APPNAME=ProMem"
set "TASK_AGENT=ProMem Agent"
set "TASK_TRACKER=ProMem Tracker"
set "INSTALL_DIR=%LOCALAPPDATA%\%APPNAME%"
set "TRACKER_DIR=%INSTALL_DIR%\productivity-tracker"
set "TRACKER_DB=%INSTALL_DIR%\tracker.db"
set "SRC_DIR=%~dp0"

cd /d "%SRC_DIR%"

echo.
echo ========================================================
echo   ProMem installer (tracker + cloud agent)
echo ========================================================
echo.

REM --- Step 1: Locate Python (admin-context-safe) ------------------------
REM Under "Run as administrator" on machines where Python was installed
REM with "Just me" + "Add to PATH", the elevated session may not see the
REM user's PATH and `where python` fails. Fall through several known
REM locations so admin-mode installs never falsely report missing Python.
echo [1/11] Locating Python 3.10+...

set "PYTHON_EXE="

REM 1. Python launcher (system-wide, most reliable under admin)
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
    )
)

REM 2. python on PATH
if not defined PYTHON_EXE (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%p in ('where python') do (
            if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
        )
    )
)

REM 3. Standard install dirs (user install, "Just me")
if not defined PYTHON_EXE (
    for %%v in (313 312 311 310) do (
        if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
            set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
        )
    )
)

REM 4. Standard install dirs (all-users install)
if not defined PYTHON_EXE (
    for %%v in (313 312 311 310) do (
        if not defined PYTHON_EXE if exist "C:\Program Files\Python%%v\python.exe" (
            set "PYTHON_EXE=C:\Program Files\Python%%v\python.exe"
        )
    )
)

if not defined PYTHON_EXE (
    echo.
    echo ERROR: Python 3.10+ not found.
    echo.
    echo Install Python 3.12 from:  https://www.python.org/downloads/
    echo IMPORTANT: Check both:
    echo   * "Add Python to PATH"
    echo   * "Install for all users"   ^(avoids PATH issues under admin^)
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

echo        Using: !PYTHON_EXE!

REM --- Step 2: Verify version >= 3.10 ------------------------------------
echo [2/11] Verifying Python version is 3.10 or newer...
"!PYTHON_EXE!" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: Python at !PYTHON_EXE! is too old. Need 3.10+.
    "!PYTHON_EXE!" -c "import sys; print('Found:', sys.version)"
    echo Upgrade from:  https://www.python.org/downloads/
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)
for /f "delims=" %%v in ('"!PYTHON_EXE!" --version') do echo        Version: %%v

REM --- Step 3: Create install dir ----------------------------------------
echo [3/11] Creating install dir at %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM --- Step 4: Copy agent + tracker source -------------------------------
echo [4/11] Copying agent and tracker source...
xcopy /E /I /Y /Q "%SRC_DIR%promem_agent" "%INSTALL_DIR%\promem_agent\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy promem_agent\ from %SRC_DIR%
    pause
    exit /b 1
)
xcopy /E /I /Y /Q "%SRC_DIR%productivity-tracker" "%TRACKER_DIR%\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy productivity-tracker\ from %SRC_DIR%
    pause
    exit /b 1
)
copy /Y "%SRC_DIR%requirements-agent.txt" "%INSTALL_DIR%\" >nul
if exist "%SRC_DIR%uninstall.bat" copy /Y "%SRC_DIR%uninstall.bat" "%INSTALL_DIR%\" >nul

REM --- Step 5: Create venv -----------------------------------------------
echo [5/11] Creating virtual environment at %INSTALL_DIR%\.venv ...
if exist "%INSTALL_DIR%\.venv" (
    echo        venv already exists; skipping create.
) else (
    "!PYTHON_EXE!" -m venv "%INSTALL_DIR%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Python's venv module may be missing.
        echo        Try reinstalling Python from python.org.
        pause
        exit /b 1
    )
)

REM --- Step 6: Install agent dependencies --------------------------------
echo [6/11] Installing agent dependencies (keyring, httpx, pyjwt)...
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --upgrade -r "%INSTALL_DIR%\requirements-agent.txt"
if errorlevel 1 (
    echo ERROR: pip install of agent dependencies failed. Check your internet connection.
    pause
    exit /b 1
)

REM --- Step 7: Install productivity-tracker package (Promem-minimal) -----
echo [7/11] Installing productivity-tracker package (Promem-minimal mode)...
REM No `[pmis]` extras: chromadb stays out, PMIS subsystems gate to no-op.
REM --force-reinstall --no-deps: refreshes the tracker package code without
REM re-pulling its (large) dependency tree on every install. Required because
REM productivity-tracker's version field is static at 0.1.0 and pip would
REM otherwise skip reinstall when the same version is already in the venv,
REM leaving stale code from a prior install.
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --force-reinstall --no-deps "%TRACKER_DIR%"
if errorlevel 1 (
    echo ERROR: pip install of productivity-tracker failed. Check your internet connection.
    pause
    exit /b 1
)

REM --- Step 8: Write runner scripts and register startup mechanism --------
echo [8/11] Writing runner scripts and registering startup mechanism...

REM agent runner: single-shot upload (called by schtasks every 5 min, OR by agent_loop.bat).
> "%INSTALL_DIR%\runner.bat" (
    echo @echo off
    echo REM ProMem agent runner - one upload of tracker.db -^> cloud.
    echo set PROMEM_TRACKER_DB=%TRACKER_DB%
    echo cd /d "%INSTALL_DIR%"
    echo "%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent run
    echo exit /b %%errorlevel%%
)

REM agent loop: HKCU\Run fallback when schtasks denied. Calls runner.bat every 5 min.
> "%INSTALL_DIR%\agent_loop.bat" (
    echo @echo off
    echo REM ProMem agent loop - HKCU\Run fallback ^(used when schtasks denied^).
    echo :loop
    echo call "%INSTALL_DIR%\runner.bat" ^>nul 2^>^&1
    echo timeout /t 300 /nobreak ^>nul
    echo goto :loop
)

REM tracker runner: long-lived, started at logon (schtask or HKCU\Run).
REM OPENAI_USE_PROXY=true routes OpenAI calls through the Promem Cloudflare
REM Worker (no per-user OpenAI key needed).
> "%INSTALL_DIR%\tracker_runner.bat" (
    echo @echo off
    echo REM ProMem tracker runner - long-lived, started at logon.
    echo set OPENAI_USE_PROXY=true
    echo set PROMEM_TRACKER_DB=%TRACKER_DB%
    echo set PYTHONPATH=%INSTALL_DIR%
    echo cd /d "%TRACKER_DIR%"
    echo "%INSTALL_DIR%\.venv\Scripts\python.exe" -m src.agent.tracker
    echo exit /b %%errorlevel%%
)

REM Try schtasks for agent (every 5 min). On failure (locked-down policies),
REM fall back to HKCU\Run with a self-looping bat. Both paths are non-admin
REM in their respective Windows configs, so the install never blocks here.
schtasks /Create /TN "%TASK_AGENT%" /TR "\"%INSTALL_DIR%\runner.bat\"" /SC MINUTE /MO 5 /F /RL LIMITED >nul 2>&1
if errorlevel 1 (
    echo        Agent: schtasks denied -^> using HKCU\Run fallback
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Agent" /t REG_SZ /d "cmd /c start \"\" /min \"%INSTALL_DIR%\agent_loop.bat\"" /f >nul
    REM HKCU\Run fires only at next logon — kick the loop now so the agent runs this session too.
    start "ProMem Agent" /B /MIN cmd /c "%INSTALL_DIR%\agent_loop.bat"
) else (
    echo        Agent: scheduled task registered ^(every 5 min^)
    REM Also clean any stale HKCU\Run entry from a prior fallback install.
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Agent" /f >nul 2>&1
)

REM Try schtasks for tracker (at logon). On failure, fall back to HKCU\Run.
schtasks /Create /TN "%TASK_TRACKER%" /TR "\"%INSTALL_DIR%\tracker_runner.bat\"" /SC ONLOGON /F /RL LIMITED >nul 2>&1
if errorlevel 1 (
    echo        Tracker: schtasks denied -^> using HKCU\Run fallback
    reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Tracker" /t REG_SZ /d "cmd /c start \"\" /min \"%INSTALL_DIR%\tracker_runner.bat\"" /f >nul
    REM Tracker is started inline by step 10, so HKCU\Run handles future logons only.
) else (
    echo        Tracker: scheduled task registered ^(at logon^)
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "ProMem Tracker" /f >nul 2>&1
)

REM --- Step 9: Trigger one-time OAuth login ------------------------------
echo [9/11] Opening browser for one-time login (Google / Supabase)...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent init
if errorlevel 1 (
    echo.
    echo NOTE: OAuth login did not complete. You can re-run it later:
    echo   cd /d "%INSTALL_DIR%"
    echo   .venv\Scripts\python.exe -m promem_agent init
)

REM --- Step 10: Start tracker now + open wiki dashboard ------------------
echo [10/11] Starting tracker now and opening your wiki...
REM Spawn tracker_runner.bat in a new window so this script can finish.
start "ProMem Tracker" /B cmd /c "%INSTALL_DIR%\tracker_runner.bat"
start "" "https://promem.fly.dev/wiki"

REM --- Step 11: Verify ingest end-to-end (wait + force agent run) --------
echo [11/11] Waiting 60s for tracker to capture a first segment, then forcing an upload...
REM Tracker needs ~30-60s to capture screenshots, finalize a segment via SSIM,
REM classify it via the Worker, and write it to context_1. We then run the
REM agent once explicitly (same code path the schtask fires every 5 min) so
REM the user sees data in the dashboard immediately rather than waiting for
REM the next scheduled run.
timeout /t 60 /nobreak >nul
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent --verbose run
if errorlevel 1 (
    echo.
    echo ============================================================
    echo  Tracker is running but the first agent upload failed.
    echo  Open the log to see why:
    echo    notepad "%INSTALL_DIR%\agent.log"
    echo  The agent will retry every 5 minutes regardless. Dashboard:
    echo    https://promem.fly.dev/productivity
    echo ============================================================
) else (
    echo.
    echo ============================================================
    echo  ProMem is live. Your dashboard:
    echo    https://promem.fly.dev/productivity
    echo  Refresh in 1-2 minutes to see your first segment.
    echo ============================================================
)

echo.
echo ========================================================
echo  ProMem installed!
echo.
echo  Tracker:  running now (and on every logon)
echo  Agent:    runs every 5 min, uploads to cloud
echo  Wiki:     https://promem.fly.dev/wiki
echo.
echo  Check status:
echo    cd /d "%INSTALL_DIR%"
echo    .venv\Scripts\python.exe -m promem_agent status
echo.
echo  Logs:
echo    Agent:   %INSTALL_DIR%\agent.log
echo    Tracker: %TRACKER_DIR%\logs\ (if configured)
echo.
echo  Uninstall:
echo    "%INSTALL_DIR%\uninstall.bat"
echo ========================================================
echo.
pause
