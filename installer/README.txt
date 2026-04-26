ProMem Agent - Windows installer
=================================

This zip contains the ProMem agent + a small Windows installer batch script.

REQUIREMENTS
------------
Python 3.10 or newer must be installed and on PATH.
Download:  https://www.python.org/downloads/
IMPORTANT: Check "Add Python to PATH" on the first install screen.

INSTALL
-------
1. Extract this zip somewhere (Downloads is fine).
2. Double-click  setup.bat
3. The installer will:
     - Verify Python 3.10+
     - Create a per-user install at  %LOCALAPPDATA%\ProMem
     - Set up a virtual environment with required packages
     - Register a Task Scheduler entry to run every 5 minutes
     - Open your browser once for Google login

The agent then runs silently in the background, uploading new tracker
segments to ProMem cloud every 5 minutes. Updates are automatic.

STATUS
------
Open a Command Prompt and run:
  "%LOCALAPPDATA%\ProMem\.venv\Scripts\python.exe" -m promem_agent status

UNINSTALL
---------
Double-click  %LOCALAPPDATA%\ProMem\uninstall.bat
(Refresh token in Credential Manager must be removed manually.)
