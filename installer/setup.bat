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

REM --- Step 1: Locate Python (exhaustive auto-detection) ----------------
REM Seven progressively-broader checks. The goal: if a real Python 3.10+
REM exists ANYWHERE the user might have installed it, find it and skip
REM straight to step 2. Only fail if all seven miss.
REM
REM Skip Microsoft Store stubs (\WindowsApps\) at every step — those are
REM aliases that open a Store popup and a sandboxed Python that breaks
REM venv creation later.
REM
REM Manual escape hatch: set PYTHON_EXE in your shell BEFORE running
REM setup.bat (e.g. `set PYTHON_EXE=C:\my\python.exe ^&^& setup.bat`)
REM and the auto-detection is skipped entirely.
echo [1/11] Locating Python 3.10+...

REM Honor user-provided PYTHON_EXE if it points to an existing file.
if defined PYTHON_EXE (
    if exist "%PYTHON_EXE%" (
        echo        User-provided PYTHON_EXE = %PYTHON_EXE%
    ) else (
        echo        WARNING: PYTHON_EXE was set to %PYTHON_EXE% but file does not exist; ignoring.
        set "PYTHON_EXE="
    )
)

REM 1. Direct path to the Python launcher — always at C:\Windows\py.exe
REM    when Python was installed via python.org for all users. No PATH
REM    dependency, works under "Run as administrator" without inheriting
REM    the regular user's PATH.
if not defined PYTHON_EXE if exist "C:\Windows\py.exe" (
    for /f "delims=" %%q in ('"C:\Windows\py.exe" -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%q"
    )
)

REM 2. py launcher on PATH (skip Store stub).
if not defined PYTHON_EXE (
    for /f "delims=" %%p in ('where py 2^>nul') do (
        if not defined PYTHON_EXE (
            echo %%p | findstr /I /C:"WindowsApps" >nul
            if errorlevel 1 (
                for /f "delims=" %%q in ('"%%p" -3 -c "import sys; print(sys.executable)" 2^>nul') do (
                    if not defined PYTHON_EXE set "PYTHON_EXE=%%q"
                )
            )
        )
    )
)

REM 3. python on PATH (skip Store stub).
if not defined PYTHON_EXE (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PYTHON_EXE (
            echo %%p | findstr /I /C:"WindowsApps" >nul
            if errorlevel 1 (
                for /f "delims=" %%q in ('"%%p" -c "import sys; print(sys.executable)" 2^>nul') do (
                    if not defined PYTHON_EXE set "PYTHON_EXE=%%q"
                )
            )
        )
    )
)

REM 4. Windows Registry (PEP 514). Both python.org installers and most
REM    third-party Pythons (Anaconda, etc.) register their ExecutablePath
REM    here. This survives admin/UAC context mismatches, missing PATH, and
REM    non-standard install directories — it answers "where did Python say
REM    it lives" rather than guessing where files might be.
if not defined PYTHON_EXE (
    for %%H in (HKLM HKCU) do (
        if not defined PYTHON_EXE (
            for /f "tokens=2,*" %%a in ('reg query "%%H\Software\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| findstr /I "ExecutablePath"') do (
                if not defined PYTHON_EXE if exist "%%b" set "PYTHON_EXE=%%b"
            )
        )
    )
)

REM 5. Standard "Just me" install dirs (versions 3.10 through 3.16).
if not defined PYTHON_EXE (
    for %%v in (316 315 314 313 312 311 310) do (
        if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
            set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
        )
    )
)

REM 6. Standard "Install for all users" dirs.
if not defined PYTHON_EXE (
    for %%v in (316 315 314 313 312 311 310) do (
        if not defined PYTHON_EXE if exist "C:\Program Files\Python%%v\python.exe" (
            set "PYTHON_EXE=C:\Program Files\Python%%v\python.exe"
        )
    )
)

REM 7. Common third-party / legacy install paths (Anaconda, miniconda,
REM    bare C:\PythonXX, ProgramData installs).
if not defined PYTHON_EXE (
    for %%P in (
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "%USERPROFILE%\anaconda3\python.exe"
        "%USERPROFILE%\miniconda3\python.exe"
        "C:\ProgramData\Anaconda3\python.exe"
        "C:\ProgramData\miniconda3\python.exe"
    ) do (
        if not defined PYTHON_EXE if exist %%P set "PYTHON_EXE=%%~P"
    )
)

REM Final guard: if the resolved sys.executable ends up under WindowsApps
REM (rare hybrid installs where a non-stub launcher returned a Store path),
REM reject it — Store Python's sandbox breaks venv create later.
if defined PYTHON_EXE (
    echo !PYTHON_EXE! | findstr /I /C:"WindowsApps" >nul
    if not errorlevel 1 (
        echo        WARNING: Discovered Python is a Microsoft Store stub at !PYTHON_EXE!; rejecting.
        set "PYTHON_EXE="
    )
)

if not defined PYTHON_EXE (
    echo.
    echo ERROR: Python 3.10+ was not found in any standard location.
    echo.
    echo Tried: C:\Windows\py.exe, "where py", "where python", Windows
    echo Registry ^(HKLM and HKCU Software\Python\PythonCore^),
    echo %LOCALAPPDATA%\Programs\Python\, C:\Program Files\Python*\,
    echo C:\Python*\, Anaconda/miniconda dirs.
    echo.
    echo If your Python lives at a non-standard path, set it manually:
    echo   set "PYTHON_EXE=C:\path\to\your\python.exe"
    echo   setup.bat
    echo.
    echo Or install fresh from:  https://www.python.org/downloads/
    echo IMPORTANT: Check "Add Python to PATH" + "Install for all users".
    echo.
    echo If a Microsoft Store window opened, CANCEL it — that's the Store
    echo alias, not a real Python. Disable it via:
    echo   Settings -^> Apps -^> Advanced app settings -^> App execution aliases
    echo   Toggle OFF "App Installer python" and "App Installer python3"
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)

echo        Using: !PYTHON_EXE!

REM Diagnostic: report whether Python's directory is on PATH. Setup.bat
REM doesn't NEED it (we use absolute paths everywhere from here on), but
REM a user typing `python` in a fresh shell later will only get a hit
REM if it's on PATH — flagging this helps surface the common "I installed
REM Python but `python` does nothing" gotcha.
for %%D in ("!PYTHON_EXE!") do set "_PY_DIR=%%~dpD"
REM strip trailing backslash for clean PATH comparison
if defined _PY_DIR set "_PY_DIR=!_PY_DIR:~0,-1!"
echo ;!PATH!; | findstr /I /C:";!_PY_DIR!;" >nul
if errorlevel 1 (
    echo        On PATH: NO — adding !_PY_DIR! to your user PATH ^(takes effect in new shells^)...
    REM Append to the user's HKCU\Environment\Path via PowerShell. User scope
    REM doesn't need admin. Skips if the dir is already there. New shells will
    REM see python on PATH; the current setup.bat session keeps using
    REM absolute paths so nothing breaks here.
    powershell -NoProfile -Command "$d='!_PY_DIR!'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p='' }; if (-not (($p -split ';') -contains $d)) { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $d).TrimStart(';'), 'User'); Write-Host '       Added to user PATH. Open a new terminal to use python directly.' } else { Write-Host '       Already in user PATH.' }" 2>nul
) else (
    echo        On PATH: yes
)
set "_PY_DIR="

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
REM
REM Two-step install:
REM  (a) `pip install --upgrade <dir>` — installs/upgrades the tracker package
REM      AND its declared dependencies (python-dotenv, sqlalchemy, openai,
REM      Pillow, mss, pywin32, pynput, etc). Without this, a fresh venv has
REM      none of the tracker's runtime imports and the tracker crashes on
REM      first launch with `ModuleNotFoundError: No module named 'dotenv'`.
REM      No-op when deps are already at requested versions, so re-installs
REM      stay fast.
REM  (b) `pip install --force-reinstall --no-deps <dir>` — refreshes ONLY
REM      the tracker package source (no re-resolution of deps). Required
REM      because productivity-tracker's version field is static at 0.1.0,
REM      so step (a)'s --upgrade no-ops on a same-version source change.
REM      This guarantees the latest source from the bundled zip lands in
REM      the venv even when pip thinks 0.1.0 == 0.1.0.
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --upgrade "%TRACKER_DIR%"
if errorlevel 1 (
    echo ERROR: pip install of productivity-tracker dependencies failed. Check your internet connection.
    pause
    exit /b 1
)
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --force-reinstall --no-deps "%TRACKER_DIR%"
if errorlevel 1 (
    echo ERROR: pip force-reinstall of productivity-tracker source failed.
    pause
    exit /b 1
)

REM Pre-create tracker.db with the proper schema so the agent's init step
REM (step 9) doesn't print "tracker.db not found", and any startup pre-flight
REM check finds a valid empty DB. Idempotent: Database.initialize() uses
REM SQLAlchemy create_all + a custom migrator, both no-op on an existing DB.
"%INSTALL_DIR%\.venv\Scripts\python.exe" -c "from src.storage.db import Database; Database(db_path=r'%TRACKER_DB%').initialize()" 2>nul
if errorlevel 1 (
    echo        NOTE: tracker.db pre-init failed; tracker will create it on first launch.
) else (
    echo        tracker.db schema initialized at %TRACKER_DB%
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

REM --- Step 11: Verify ingest end-to-end (wait + force agent recover) ----
echo [11/11] Waiting 60s for tracker to capture a first segment, then forcing a backfill...
REM Tracker needs ~30-60s to capture screenshots, finalize a segment via SSIM,
REM classify it via the Worker, and write it to context_1. We then run the
REM agent's `recover --apply` (rather than plain `run`) so any leftover data
REM from prior install layouts (legacy ~/.productivity-tracker/tracker.db,
REM rows beyond the existing state cutoff, etc.) gets uploaded on the same
REM trip — not just the most recent segment. ON CONFLICT DO NOTHING server-
REM side makes re-uploads cheap.
timeout /t 60 /nobreak >nul
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent --verbose recover --apply --days 30
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
