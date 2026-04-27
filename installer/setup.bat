@echo off
setlocal EnableExtensions

REM ----------------------------------------------------------------------
REM ProMem setup bootstrap.
REM
REM The supported install path is the compiled NSIS setup EXE:
REM   promem_setup_<version>_x64.exe
REM
REM This script simply finds that EXE next to itself and launches it.
REM ----------------------------------------------------------------------

set "SRC_DIR=%~dp0"
set "SETUP_EXE="

for /f "delims=" %%F in ('dir /b /a:-d "%SRC_DIR%promem_setup_*_x64.exe" 2^>nul') do (
    set "SETUP_EXE=%%F"
    goto :found
)

echo.
echo ERROR: Could not find promem_setup_*_x64.exe next to setup.bat.
echo.
echo Build it from this repo using:
echo   ./installer/build.sh
echo.
pause
exit /b 1

:found
echo Launching %SETUP_EXE% ...
start "" "%SRC_DIR%%SETUP_EXE%"
exit /b 0
