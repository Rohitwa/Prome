@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ======================================================================
REM  ProMem Agent installer (Windows .bat)
REM  Per-user install at %LOCALAPPDATA%\ProMem; no admin rights needed.
REM  Run by double-clicking, after extracting the agent zip.
REM ======================================================================

set "APPNAME=ProMem"
set "TASK_NAME=ProMem Agent"
set "INSTALL_DIR=%LOCALAPPDATA%\%APPNAME%"
set "SRC_DIR=%~dp0"

echo.
echo ========================================================
echo   ProMem Agent installer
echo ========================================================
echo.

REM --- Step 1: Detect Python ----------------------------------------------
echo [1/8] Checking for Python on PATH...
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
echo [2/8] Verifying Python version is 3.10 or newer...
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
for /f "delims=" %%v in ('python --version') do echo       Found: %%v

REM --- Step 3: Create install dir ----------------------------------------
echo [3/8] Creating install dir at %INSTALL_DIR% ...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM --- Step 4: Copy agent files ------------------------------------------
echo [4/8] Copying agent files...
xcopy /E /I /Y /Q "%SRC_DIR%promem_agent" "%INSTALL_DIR%\promem_agent\" >nul
if errorlevel 1 (
    echo ERROR: Could not copy promem_agent\ from %SRC_DIR%
    pause
    exit /b 1
)
copy /Y "%SRC_DIR%requirements-agent.txt" "%INSTALL_DIR%\" >nul
if exist "%SRC_DIR%uninstall.bat" copy /Y "%SRC_DIR%uninstall.bat" "%INSTALL_DIR%\" >nul

REM --- Step 5: Create venv -----------------------------------------------
echo [5/8] Creating virtual environment at %INSTALL_DIR%\.venv ...
if exist "%INSTALL_DIR%\.venv" (
    echo       venv already exists; skipping create.
) else (
    python -m venv "%INSTALL_DIR%\.venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv. Python's venv module may be missing.
        echo        Try reinstalling Python from python.org.
        pause
        exit /b 1
    )
)

REM --- Step 6: Install deps ----------------------------------------------
echo [6/8] Installing Python dependencies (keyring, httpx, pyjwt)...
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check --upgrade -r "%INSTALL_DIR%\requirements-agent.txt"
if errorlevel 1 (
    echo ERROR: pip install failed. Check your internet connection and re-run.
    pause
    exit /b 1
)

REM --- Step 7: Write runner.bat + register Task Scheduler ----------------
echo [7/8] Writing runner.bat and registering scheduled task...
> "%INSTALL_DIR%\runner.bat" (
    echo @echo off
    echo REM ProMem runner - invoked by Task Scheduler every 5 min.
    echo "%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent run
    echo exit /b %%errorlevel%%
)

schtasks /Create /TN "%TASK_NAME%" /TR "\"%INSTALL_DIR%\runner.bat\"" /SC MINUTE /MO 5 /F /RL LIMITED >nul
if errorlevel 1 (
    echo WARNING: Failed to register the scheduled task automatically.
    echo          You can register it manually:
    echo            schtasks /Create /TN "%TASK_NAME%" /TR "\"%INSTALL_DIR%\runner.bat\"" /SC MINUTE /MO 5 /F
)

REM --- Step 8: Trigger one-time OAuth login ------------------------------
echo [8/8] Opening browser for one-time login (Google / Supabase)...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent init
if errorlevel 1 (
    echo.
    echo NOTE: OAuth login did not complete. You can re-run it later:
    echo   "%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent init
)

echo.
echo ========================================================
echo  ProMem Agent installed!
echo.
echo  Status check at any time:
echo    "%INSTALL_DIR%\.venv\Scripts\python.exe" -m promem_agent status
echo.
echo  Log file:
echo    %INSTALL_DIR%\agent.log
echo.
echo  Uninstall:
echo    "%INSTALL_DIR%\uninstall.bat"
echo ========================================================
echo.
pause
