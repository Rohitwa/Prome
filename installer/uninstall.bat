@echo off
setlocal EnableExtensions

REM ======================================================================
REM  ProMem Agent uninstaller.
REM  Removes the scheduled task and (optionally) install dir + venv.
REM  Refresh token in Windows Credential Manager must be removed manually
REM  via Control Panel -> Credential Manager -> 'ProMem'.
REM ======================================================================

set "TASK_NAME=ProMem Agent"
set "INSTALL_DIR=%LOCALAPPDATA%\ProMem"

echo.
echo Uninstalling ProMem Agent...
echo.

REM --- Step 1: Remove scheduled task -------------------------------------
echo [1/2] Removing scheduled task...
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>&1

REM --- Step 2: Remove install dir ----------------------------------------
echo [2/2] Removing install dir at %INSTALL_DIR% ...
echo       (state files, logs, refresh_token in Credential Manager NOT removed)
echo.
choice /M "Delete %INSTALL_DIR% and all its contents?"
if errorlevel 2 (
    echo Skipped. You can delete it manually later.
    pause
    exit /b 0
)

REM Self-delete trick: spawn a detached cmd to rmdir after this script exits.
REM (rmdir can't delete the directory containing the running .bat file.)
start "" /b cmd /c "timeout /t 1 /nobreak >nul && rmdir /s /q ""%INSTALL_DIR%"" && exit"
echo.
echo Uninstall scheduled. Files will be removed momentarily.
echo Refresh token: Control Panel -> Credential Manager -> Windows Credentials -> 'ProMem' -> Remove.
echo.
exit /b 0
