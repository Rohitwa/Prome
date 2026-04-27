ProMem - Windows installer
==========================

This installer installs both local components needed for ProMem:

  1. Tracker    Captures work activity and writes local tracker.db.
  2. Agent      Uploads new tracker segments/frames to ProMem Cloud.

The installer is per-user and does NOT require admin rights.
Install location:  %LOCALAPPDATA%\ProMem

REQUIREMENTS
------------
No Python install is required on the user's machine.
The setup EXE includes all runtime dependencies.

INSTALL
-------
1. Run:  promem_setup_<version>_x64.exe
2. The installer will:
     - Extract bundled tracker + agent binaries
     - Register two hidden Task Scheduler jobs:
         "ProMem Tracker" -> at logon (background)
         "ProMem Agent"   -> every 5 min (background upload)
     - Open browser once for Google login
     - Start tracker immediately
     - Open https://promem.fly.dev/wiki

No command prompt windows should pop up during normal background runs.
Updates are installer-based: users install a newer setup EXE to upgrade.

STATUS / SUPPORT COMMANDS
-------------------------
Open Command Prompt and run:

  cd /d "%LOCALAPPDATA%\ProMem"
  bin\promem_agent\promem_agent.exe status

If login must be repeated:

  bin\promem_agent\promem_agent.exe init

UNINSTALL
---------
Use Windows "Installed apps" and uninstall "ProMem",
or run:

  %LOCALAPPDATA%\ProMem\uninstall.exe

Note: remove refresh token manually from Windows Credential Manager:
Control Panel -> Credential Manager -> Windows Credentials -> 'ProMem'

BUILDING THE WINDOWS PAYLOAD
----------------------------
Run on a Windows x64 build machine:

  powershell -ExecutionPolicy Bypass -File installer\build_pyinstaller_windows.ps1

Then compile installer (makensis) from this repo:

  ./installer/build.sh

`PROMEM_TRACKER_SRC` can override the external tracker source path.
