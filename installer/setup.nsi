; ProMem agent installer (NSIS).
;
; Build on Mac:  brew install makensis  ;  ../installer/build.sh
; Or directly:   makensis -DAGENT_VERSION=0.1.0 setup.nsi
;
; Per-user install: lives in %LOCALAPPDATA%\ProMem, no admin rights, no UAC.
; Detects Python 3.10+ (prompts user to install manually if missing - clearer
; than silently auto-installing). Creates a per-install venv, pip-installs
; agent deps, writes runner.bat, registers Task Scheduler entry running every
; 5 minutes, then triggers OAuth login (browser opens once).

!ifndef AGENT_VERSION
  !define AGENT_VERSION "0.0.0-dev"
!endif

!define APPNAME       "ProMem Agent"
!define APPNAME_SHORT "ProMem"
!define TASK_NAME     "ProMem Agent"
!define COMPANY       "Rohit Singh"

Name        "${APPNAME} ${AGENT_VERSION}"
OutFile     "promem_setup_${AGENT_VERSION}.exe"
InstallDir  "$LOCALAPPDATA\${APPNAME_SHORT}"
ShowInstDetails   show
ShowUninstDetails show
RequestExecutionLevel user      ; per-user - never prompt for admin

!include "MUI2.nsh"
!include "LogicLib.nsh"

; -- UI -------------------------------------------------------------------
!define MUI_ABORTWARNING
!define MUI_ICON   "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE   "../LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "ProMem Agent ${AGENT_VERSION} installed.$\r$\n$\r$\nThe agent runs every 5 minutes via Task Scheduler.$\r$\n$\r$\nCheck status at any time:$\r$\n  $INSTDIR\.venv\Scripts\python.exe -m promem_agent status$\r$\n$\r$\nIf the browser login did not complete during install, re-run:$\r$\n  $INSTDIR\.venv\Scripts\python.exe -m promem_agent init"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; -- Install section ------------------------------------------------------
Section "Install" SecInstall
  SetOutPath "$INSTDIR"

  ; --- Step 1: Detect Python ---------------------------------------------
  DetailPrint "Checking for Python on PATH..."
  nsExec::ExecToStack 'cmd /c python --version 2>&1'
  Pop $0  ; exit code
  Pop $1  ; output
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Python is not installed or not on PATH.$\r$\n$\r$\nPlease install Python 3.10 or newer from:$\r$\n  https://www.python.org/downloads/$\r$\n$\r$\nIMPORTANT: Check 'Add Python to PATH' on the first install screen.$\r$\n$\r$\nThen re-run this installer."
    ExecShell "open" "https://www.python.org/downloads/"
    Abort
  ${EndIf}
  DetailPrint "Found: $1"

  ; --- Step 2: Verify Python >= 3.10 -------------------------------------
  DetailPrint "Checking Python version is 3.10 or newer..."
  nsExec::ExecToStack 'cmd /c python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Python 3.10 or newer is required (you have: $1).$\r$\n$\r$\nUpgrade from:$\r$\n  https://www.python.org/downloads/"
    ExecShell "open" "https://www.python.org/downloads/"
    Abort
  ${EndIf}

  ; --- Step 3: Extract baked agent.zip ----------------------------------
  DetailPrint "Extracting agent files..."
  File "agent.zip"
  ; tar.exe ships with Windows 10 1803+; extracts to $INSTDIR.
  nsExec::ExecToStack 'cmd /c tar -xf "$INSTDIR\agent.zip" -C "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Failed to extract agent.zip.$\r$\n$\r$\ntar.exe is required (built into Windows 10 1803+). If you are on an older Windows, please update."
    Abort
  ${EndIf}
  Delete "$INSTDIR\agent.zip"

  ; --- Step 4: Create per-install venv ----------------------------------
  DetailPrint "Creating virtual environment at $INSTDIR\.venv ..."
  nsExec::ExecToStack 'cmd /c python -m venv "$INSTDIR\.venv"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Failed to create Python venv.$\r$\n$\r$\nThe Python venv module may be missing. Try installing Python from python.org (the official installer includes it)."
    Abort
  ${EndIf}

  ; --- Step 5: Install agent dependencies into venv ---------------------
  DetailPrint "Installing Python dependencies (keyring, httpx, pyjwt)..."
  nsExec::ExecToStack 'cmd /c "$INSTDIR\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check -r "$INSTDIR\requirements-agent.txt"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP|MB_OK "Failed to install Python dependencies. Check your internet connection and re-run the installer.$\r$\n$\r$\nIf the problem persists, install manually:$\r$\n  $INSTDIR\.venv\Scripts\pip install -r $INSTDIR\requirements-agent.txt"
    Abort
  ${EndIf}

  ; --- Step 6: Write runner.bat -----------------------------------------
  DetailPrint "Writing runner.bat..."
  FileOpen $0 "$INSTDIR\runner.bat" w
  FileWrite $0 "@echo off$\r$\n"
  FileWrite $0 'REM ProMem runner - invoked by Task Scheduler every 5 min.$\r$\n'
  FileWrite $0 '"$INSTDIR\.venv\Scripts\python.exe" -m promem_agent run$\r$\n'
  FileWrite $0 "exit /b %errorlevel%$\r$\n"
  FileClose $0

  ; --- Step 7: Register Task Scheduler entry ----------------------------
  DetailPrint "Registering '${TASK_NAME}' scheduled task (every 5 min)..."
  nsExec::ExecToStack 'cmd /c schtasks /Create /TN "${TASK_NAME}" /TR "\"$INSTDIR\runner.bat\"" /SC MINUTE /MO 5 /F /RL LIMITED'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION|MB_OK "Failed to register the scheduled task.$\r$\n$\r$\nYou can register it manually:$\r$\n  schtasks /Create /TN ${TASK_NAME} /TR $\"$INSTDIR\runner.bat$\" /SC MINUTE /MO 5 /F"
  ${EndIf}

  ; --- Step 8: Trigger one-time OAuth login (opens browser) -------------
  DetailPrint "Opening browser for one-time login (Google / Supabase)..."
  ExecWait '"$INSTDIR\.venv\Scripts\python.exe" -m promem_agent init' $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION|MB_OK "OAuth login did not complete (exit=$0).$\r$\n$\r$\nYou can re-run it later:$\r$\n  $INSTDIR\.venv\Scripts\python.exe -m promem_agent init"
  ${EndIf}

  ; --- Uninstaller + Add/Remove Programs entry --------------------------
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "DisplayName"     "${APPNAME}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "DisplayVersion"  "${AGENT_VERSION}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "Publisher"       "${COMPANY}"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}" "NoRepair" 1
SectionEnd

; -- Uninstall section ---------------------------------------------------
Section "Uninstall"
  DetailPrint "Removing '${TASK_NAME}' scheduled task..."
  nsExec::ExecToStack 'cmd /c schtasks /Delete /TN "${TASK_NAME}" /F'
  Pop $0  ; ignore exit code - task may already be missing

  DetailPrint "Removing program files..."
  RMDir /r "$INSTDIR\promem_agent"
  RMDir /r "$INSTDIR\.venv"
  Delete   "$INSTDIR\runner.bat"
  Delete   "$INSTDIR\requirements-agent.txt"
  Delete   "$INSTDIR\uninstall.exe"

  ; State files (logs, sync state) are user data - ask before deleting.
  MessageBox MB_YESNO "Also remove agent state files (sync log, last-uploaded marker, staged updates)?$\r$\n$\r$\nNote: your refresh token in Windows Credential Manager must be removed manually via Control Panel -> Credential Manager -> Windows Credentials -> 'ProMem'." IDNO done_data
    Delete   "$INSTDIR\agent.log*"
    Delete   "$INSTDIR\agent_state.json"
    Delete   "$INSTDIR\.pending_update.json"
    Delete   "$INSTDIR\.last_update_check"
    RMDir /r "$INSTDIR\staged"
  done_data:

  RMDir "$INSTDIR"  ; only removes if empty
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}"
SectionEnd
