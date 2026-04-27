@echo off
setlocal EnableExtensions

REM ======================================================================
REM  ProMem uninstaller helper.
REM  Primary uninstall path is %LOCALAPPDATA%\ProMem\uninstall.exe
REM ======================================================================

set "TASK_AGENT=ProMem Agent"
set "TASK_TRACKER=ProMem Tracker"
set "INSTALL_DIR=%LOCALAPPDATA%\ProMem"

echo.
echo Uninstalling ProMem...
echo.

REM --- Step 1: Remove scheduled tasks ------------------------------------
echo [1/3] Removing scheduled tasks...
schtasks /Delete /TN "%TASK_AGENT%" /F >nul 2>&1
schtasks /Delete /TN "%TASK_TRACKER%" /F >nul 2>&1

REM --- Step 2: Stop running processes (best effort) ----------------------
echo [2/3] Stopping running processes...
taskkill /F /IM promem_tracker.exe >nul 2>&1
taskkill /F /IM promem_agent.exe >nul 2>&1
REM legacy fallback cleanup (older Python-based installs)
taskkill /F /IM python.exe /FI "WINDOWTITLE eq ProMem Tracker*" >nul 2>&1

REM --- Step 3: Remove install dir ----------------------------------------
echo [3/3] Removing install dir at %INSTALL_DIR% ...
echo       (Credential Manager token is NOT removed automatically)
echo.
choice /M "Delete %INSTALL_DIR% and all its contents?"
if errorlevel 2 (
    echo Skipped. You can delete it manually later.
    pause
    exit /b 0
)

start "" /b cmd /c "timeout /t 1 /nobreak >nul && rmdir /s /q ""%INSTALL_DIR%"" && exit"
echo.
echo Uninstall scheduled. Files will be removed momentarily.
echo Remove token manually: Control Panel ^> Credential Manager ^> Windows Credentials ^> 'ProMem'.
echo.
exit /b 0
