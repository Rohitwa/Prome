@echo off
REM ProMem agent runner (legacy/manual fallback).
REM Primary installer path uses hidden VBS launchers via Task Scheduler.
set PROMEM_TRACKER_DB=%LOCALAPPDATA%\ProMem\tracker.db
set PROMEM_AGENT_DISABLE_AUTO_UPDATE=true
set PROMEM_AGENT_NONINTERACTIVE=true
"%LOCALAPPDATA%\ProMem\bin\promem_agent\promem_agent.exe" run
exit /b %errorlevel%
