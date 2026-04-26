@echo off
REM ProMem agent runner — invoked by Windows Task Scheduler every 5 min.
REM
REM The agent itself handles update apply/check inside `run`, so this stays
REM a thin wrapper. Path is the per-install venv created by the installer.
"%LOCALAPPDATA%\ProMem\.venv\Scripts\python.exe" -m promem_agent run
exit /b %errorlevel%
