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

REM --- Step 1: Detect Python ----------------------------------------------
echo [1/10] Checking for Python on PATH...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python is not installed or not on PATH.
    echo.
    echo Install Python 3.10+ from:  https://www.python.org/downloads/
    echo IMPORTANT: Check 'Add Python to PATH' on the first install screen.
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

REM --- Step 2: Verify version >= 3.10 ------------------------------------
echo [2/10] Verifying Python version is 3.10 or newer...
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo.
    echo ERROR: Python 3.10 or newer is required.
    echo Upgrade from:  https://www.python.org/downloads/
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo        Found: %%v

REM --- Step 3: Create install dir ----------------------------------------
echo [3/10] Creating install dir at %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM --- Step 4: Copy agent + tracker source -------------------------------
echo [4/10] Copying agent and tracker source...
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
echo [5/10] Creating virtual environment at %INSTALL_DIR%\.venv ...
if exist "%INSTALL_DIR%\.venv" (
    echo        venv already exists; skipping create.
) else (
    python -m venv "%INSTALL_DIR%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Python's venv module may be missing.
        echo        Try reinstalling Python from python.org.
        pause
        exit /b 1
    )
)

REM --- Step 6: Install agent dependencies --------------------------------
echo [6/10] Installing agent dependencies (keyring, httpx, pyjwt)...
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --upgrade -r "%INSTALL_DIR%\requirements-agent.txt"
if errorlevel 1 (
    echo ERROR: pip install of agent dependencies failed. Check your internet connection.
    pause
    exit /b 1
)

REM --- Step 7: Install productivity-tracker package (Promem-minimal) -----
echo [7/10] Installing productivity-tracker package (Promem-minimal mode)...
REM No `[pmis]` extras: chromadb stays out, PMIS subsystems gate to no-op.
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check "%TRACKER_DIR%"
if errorlevel 1 (
    echo ERROR: pip install of productivity-tracker failed. Check your internet connection.
    pause
    exit /b 1
)

REM --- Step 8: Write runner scripts and register scheduled tasks ---------
echo [8/10] Writing runner scripts and registering 2 scheduled tasks...

REM agent runner: every 5 min upload of tracker.db -> cloud
> "%INSTALL_DIR%\runner.bat" (
    echo @echo off
    echo REM ProMem agent runner - invoked by Task Scheduler every 5 min.
    echo set PROMEM_TRACKER_DB=%TRACKER_DB%
    echo cd /d "%INSTALL_DIR%"
    echo "%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent run
    echo exit /b %%errorlevel%%
)

REM tracker runner: long-lived, captures screenshots and writes context_1/2.
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

schtasks /Create /TN "%TASK_AGENT%" /TR "\"%INSTALL_DIR%\runner.bat\"" /SC MINUTE /MO 5 /F /RL LIMITED >nul
if errorlevel 1 (
    echo WARNING: Failed to register the agent scheduled task.
)

schtasks /Create /TN "%TASK_TRACKER%" /TR "\"%INSTALL_DIR%\tracker_runner.bat\"" /SC ONLOGON /F /RL LIMITED >nul
if errorlevel 1 (
    echo WARNING: Failed to register the tracker scheduled task.
)

REM --- Step 9: Trigger one-time OAuth login ------------------------------
echo [9/10] Opening browser for one-time login (Google / Supabase)...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent init
if errorlevel 1 (
    echo.
    echo NOTE: OAuth login did not complete. You can re-run it later:
    echo   cd /d "%INSTALL_DIR%"
    echo   .venv\Scripts\python.exe -m promem_agent init
)

REM --- Step 10: Start tracker now + open wiki dashboard ------------------
echo [10/10] Starting tracker now and opening your wiki...
REM Spawn tracker_runner.bat in a new window so this script can finish.
start "ProMem Tracker" /B cmd /c "%INSTALL_DIR%\tracker_runner.bat"
start "" "https://promem.fly.dev/wiki"

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
