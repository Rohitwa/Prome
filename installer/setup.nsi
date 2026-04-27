; ProMem Windows installer (NSIS).
;
; Builds a per-user installer that ships prebuilt binaries:
;   - bin\promem_agent\promem_agent.exe
;   - bin\promem_tracker\promem_tracker.exe
;
; No system Python required on end-user machines.
;
; Build:
;   makensis -DAGENT_VERSION=0.1.0 setup.nsi

!ifndef AGENT_VERSION
  !define AGENT_VERSION "0.0.0-dev"
!endif

!define APPNAME       "ProMem"
!define APPNAME_SHORT "ProMem"
!define TASK_AGENT    "ProMem Agent"
!define TASK_TRACKER  "ProMem Tracker"
!define COMPANY       "Rohit Singh"

Name        "${APPNAME} ${AGENT_VERSION}"
OutFile     "promem_setup_${AGENT_VERSION}_x64.exe"
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
!define MUI_FINISHPAGE_TEXT "ProMem ${AGENT_VERSION} installed.$\r$\n$\r$\nTracker runs in the background at logon.$\r$\nAgent runs every 5 minutes via Task Scheduler.$\r$\n$\r$\nCheck status at any time:$\r$\n  $INSTDIR\bin\promem_agent\promem_agent.exe status$\r$\n$\r$\nIf browser login did not complete during install, re-run:$\r$\n  $INSTDIR\bin\promem_agent\promem_agent.exe init"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; -- Install section ------------------------------------------------------
Section "Install" SecInstall
  SetOutPath "$INSTDIR"

  ; --- Step 1: Extract bundled binaries ----------------------------------
  DetailPrint "Extracting bundled runtime files..."
  File /r "payload\*"

  IfFileExists "$INSTDIR\bin\promem_agent\promem_agent.exe" +2 0
    Goto missing_payload
  IfFileExists "$INSTDIR\bin\promem_tracker\promem_tracker.exe" +2 0
    Goto missing_payload
  Goto payload_ok

  missing_payload:
    MessageBox MB_ICONSTOP|MB_OK "Installer payload is incomplete.$\r$\n$\r$\nMissing binary under:$\r$\n  $INSTDIR\bin$\r$\n$\r$\nRebuild payload with installer/build_pyinstaller_windows.ps1 and recompile setup.nsi."
    Abort

  payload_ok:

  ; --- Step 2: Write hidden launchers (no cmd popups) --------------------
  DetailPrint "Writing hidden task launchers..."

  ; Agent task launcher (every 5 min, waits for completion).
  FileOpen $0 "$INSTDIR\run_agent_hidden.vbs" w
  FileWrite $0 "Set shell = CreateObject($\"WScript.Shell$\")$\r$\n"
  FileWrite $0 "shell.CurrentDirectory = $\"$INSTDIR$\"$\r$\n"
  FileWrite $0 "shell.Environment($\"PROCESS$\")($\"PROMEM_TRACKER_DB$\") = $\"$INSTDIR\tracker.db$\"$\r$\n"
  FileWrite $0 "shell.Environment($\"PROCESS$\")($\"PROMEM_AGENT_DISABLE_AUTO_UPDATE$\") = $\"true$\"$\r$\n"
  FileWrite $0 "shell.Run Chr(34) & $\"$INSTDIR\bin\promem_agent\promem_agent.exe$\" & Chr(34) & $\" run$\", 0, True$\r$\n"
  FileClose $0

  ; Tracker launcher (at logon, long-lived background process).
  FileOpen $0 "$INSTDIR\run_tracker_hidden.vbs" w
  FileWrite $0 "Set shell = CreateObject($\"WScript.Shell$\")$\r$\n"
  FileWrite $0 "shell.CurrentDirectory = $\"$INSTDIR$\"$\r$\n"
  FileWrite $0 "shell.Environment($\"PROCESS$\")($\"OPENAI_USE_PROXY$\") = $\"true$\"$\r$\n"
  FileWrite $0 "shell.Environment($\"PROCESS$\")($\"PROMEM_TRACKER_DB$\") = $\"$INSTDIR\tracker.db$\"$\r$\n"
  FileWrite $0 "shell.Run Chr(34) & $\"$INSTDIR\bin\promem_tracker\promem_tracker.exe$\" & Chr(34), 0, False$\r$\n"
  FileClose $0

  ; --- Step 3: Register scheduled tasks ----------------------------------
  DetailPrint "Registering '${TASK_AGENT}' scheduled task (every 5 min)..."
  nsExec::ExecToStack 'cmd /c schtasks /Create /TN "${TASK_AGENT}" /TR "\"$WINDIR\System32\wscript.exe\" //B \"$INSTDIR\run_agent_hidden.vbs\"" /SC MINUTE /MO 5 /F /RL LIMITED'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION|MB_OK "Failed to register ${TASK_AGENT}.$\r$\n$\r$\nYou can register it manually from Command Prompt."
  ${EndIf}

  DetailPrint "Registering '${TASK_TRACKER}' scheduled task (at logon)..."
  nsExec::ExecToStack 'cmd /c schtasks /Create /TN "${TASK_TRACKER}" /TR "\"$WINDIR\System32\wscript.exe\" //B \"$INSTDIR\run_tracker_hidden.vbs\"" /SC ONLOGON /F /RL LIMITED'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION|MB_OK "Failed to register ${TASK_TRACKER}.$\r$\n$\r$\nYou can register it manually from Command Prompt."
  ${EndIf}

  ; --- Step 4: Trigger one-time OAuth login -------------------------------
  DetailPrint "Opening browser for one-time login (Google / Supabase)..."
  ExecWait '"$INSTDIR\bin\promem_agent\promem_agent.exe" init' $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION|MB_OK "OAuth login did not complete (exit=$0).$\r$\n$\r$\nYou can re-run later:$\r$\n  $INSTDIR\bin\promem_agent\promem_agent.exe init"
  ${EndIf}

  ; --- Step 5: Start tracker silently + open wiki -------------------------
  DetailPrint "Starting tracker in background..."
  Exec '"$WINDIR\System32\wscript.exe" //B "$INSTDIR\run_tracker_hidden.vbs"'
  ExecShell "open" "https://promem.fly.dev/wiki"

  ; --- Uninstaller + Add/Remove Programs entry ---------------------------
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
  DetailPrint "Removing scheduled tasks..."
  nsExec::ExecToStack 'cmd /c schtasks /Delete /TN "${TASK_AGENT}" /F'
  Pop $0
  nsExec::ExecToStack 'cmd /c schtasks /Delete /TN "${TASK_TRACKER}" /F'
  Pop $0

  DetailPrint "Stopping tracker process (best effort)..."
  nsExec::ExecToStack 'cmd /c taskkill /F /IM promem_tracker.exe >nul 2>&1'
  Pop $0

  DetailPrint "Removing installed binaries + launchers..."
  RMDir /r "$INSTDIR\bin\promem_agent"
  RMDir /r "$INSTDIR\bin\promem_tracker"
  RMDir /r "$INSTDIR\bin"
  Delete   "$INSTDIR\run_agent_hidden.vbs"
  Delete   "$INSTDIR\run_tracker_hidden.vbs"
  Delete   "$INSTDIR\uninstall.exe"

  ; User data (logs/state/db) — ask before deleting.
  MessageBox MB_YESNO "Also remove local data files (tracker.db, state files, logs)?$\r$\n$\r$\nNote: refresh token in Credential Manager must still be removed manually via Control Panel -> Credential Manager -> Windows Credentials -> 'ProMem'." IDNO done_data
    Delete   "$INSTDIR\agent.log*"
    Delete   "$INSTDIR\agent_state.json"
    Delete   "$INSTDIR\.pending_update.json"
    Delete   "$INSTDIR\.last_update_check"
    RMDir /r "$INSTDIR\staged"
    Delete   "$INSTDIR\tracker.db"
    Delete   "$INSTDIR\tracker.db-shm"
    Delete   "$INSTDIR\tracker.db-wal"
    RMDir /r "$INSTDIR\logs"
  done_data:

  RMDir "$INSTDIR"  ; only removes if empty
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME_SHORT}"
SectionEnd
