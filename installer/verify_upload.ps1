# verify_upload.ps1 — ProMem upload health check + auto-remediation.
#
# What it does, in order:
#   1. Confirms install layout (venv, source, runner bats).
#   2. Confirms scheduled tasks (or HKCU\Run fallback) are registered.
#   3. Confirms tracker.db exists; if not, starts the tracker task and waits.
#   4. Counts captured rows so we know there's something to upload.
#   5. Confirms OAuth is alive (refresh token in keyring, status reports
#      "logged in as <email>").
#   6. Forces an agent run and parses the result.
#       - SUCCESS  (inserted > 0)  : new rows uploaded, all good.
#       - SUCCESS  (inserted = 0)  : already up to date with cloud, also good.
#       - 401 auth expired         : re-runs `promem_agent init` and retries.
#       - Network error            : surfaces firewall / VPN suggestion.
#       - Other failure            : prints log path and exits non-zero.
#
# Exit codes:
#   0  = upload is working (whether or not new rows were sent this run)
#   1  = unrecoverable (re-run setup.bat or fix network)
#   2  = action needed by user (waited but nothing captured yet, or
#        OAuth re-auth required)
#
# Usage:  powershell -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\ProMem\verify_upload.ps1"

$ErrorActionPreference = "Continue"

$Install = "$env:LOCALAPPDATA\ProMem"
$Py      = Join-Path $Install ".venv\Scripts\python.exe"
$Db      = Join-Path $Install "tracker.db"
$State   = Join-Path $Install "agent_state.json"
$Log     = Join-Path $Install "agent.log"

function Section($msg) { Write-Host "`n── $msg ──" -ForegroundColor Cyan }
function Ok($msg)      { Write-Host "  [+] $msg"  -ForegroundColor Green }
function Warn($msg)    { Write-Host "  [!] $msg"  -ForegroundColor Yellow }
function Bad($msg)     { Write-Host "  [-] $msg"  -ForegroundColor Red }
function Die($msg, $code) { Bad $msg; exit $code }

# ─── 1. Install integrity ──────────────────────────────────────────────────
Section "1. Install integrity"
if (-not (Test-Path $Install)) { Die "ProMem not installed at $Install. Run setup.bat first." 1 }
if (-not (Test-Path $Py))      { Die "Python venv missing at $Py. Re-run setup.bat to create it." 1 }
Ok "Install dir : $Install"
Ok "Python venv : $Py"

# ─── 2. Scheduled tasks / Run keys ─────────────────────────────────────────
Section "2. Startup mechanisms"
$trackerTaskOk = $false
$agentTaskOk   = $false

schtasks /Query /TN "ProMem Tracker" /FO LIST 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { $trackerTaskOk = $true; Ok "ProMem Tracker  : schtasks registered" }

schtasks /Query /TN "ProMem Agent" /FO LIST 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { $agentTaskOk = $true; Ok "ProMem Agent    : schtasks registered" }

# Fallback HKCU\Run check (for restricted machines where schtasks failed at install).
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValues = (Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue) | Select-Object -Property "ProMem Tracker","ProMem Agent" -ErrorAction SilentlyContinue
if (-not $trackerTaskOk -and $runValues."ProMem Tracker") { $trackerTaskOk = $true; Ok "ProMem Tracker  : HKCU\Run fallback registered" }
if (-not $agentTaskOk   -and $runValues."ProMem Agent")   { $agentTaskOk   = $true; Ok "ProMem Agent    : HKCU\Run fallback registered" }

if (-not $trackerTaskOk) { Bad "ProMem Tracker  : NOT registered (no schtask, no HKCU\Run)" }
if (-not $agentTaskOk)   { Bad "ProMem Agent    : NOT registered (no schtask, no HKCU\Run)" }
if (-not $trackerTaskOk -and -not $agentTaskOk) {
    Die "Neither tracker nor agent is registered. Re-run setup.bat (right-click → Run as administrator if needed)." 1
}

# ─── 3. tracker.db existence ───────────────────────────────────────────────
Section "3. tracker.db"
if (-not (Test-Path $Db)) {
    Warn "tracker.db not found. Starting ProMem Tracker task and waiting 60s..."
    if ($trackerTaskOk) {
        schtasks /Run /TN "ProMem Tracker" 2>$null | Out-Null
    }
    Start-Sleep -Seconds 60
    if (-not (Test-Path $Db)) {
        Die "tracker.db still missing. Tracker isn't running. Re-run setup.bat to refresh runners." 1
    }
}
$dbInfo = Get-Item $Db
Ok "tracker.db    : $([math]::Round($dbInfo.Length / 1KB, 1)) KB, last write $($dbInfo.LastWriteTime)"

# ─── 4. Captured rows ──────────────────────────────────────────────────────
Section "4. Captured data"
$countOut = & $Py -c "import sqlite3; c=sqlite3.connect(r'$Db'); print(c.execute('SELECT COUNT(*) FROM context_1').fetchone()[0], c.execute('SELECT COUNT(*) FROM context_2').fetchone()[0])" 2>&1
if ($LASTEXITCODE -ne 0) { Die "Could not read tracker.db: $countOut" 1 }
$parts = ($countOut -split "\s+") | Where-Object { $_ -ne "" }
$nSegs = [int]$parts[0]
$nFrames = [int]$parts[1]
Write-Host "  context_1     : $nSegs segment(s)"
Write-Host "  context_2     : $nFrames frame(s)"
if ($nSegs -eq 0) {
    Warn "No segments captured yet. Wait 1–2 minutes after the tracker starts, then re-run this script."
    exit 2
}

# ─── 5. Auth state ─────────────────────────────────────────────────────────
Section "5. OAuth / Supabase auth"
$status = & $Py -m promem_agent status 2>&1 | Out-String
if ($status -match "logged in as (\S+)") {
    Ok "Logged in as $($Matches[1])"
} elseif ($status -match "no refresh_token") {
    Warn "No refresh token in Windows Credential Manager. Running `promem_agent init`..."
    & $Py -m promem_agent init
    if ($LASTEXITCODE -ne 0) { Die "OAuth flow failed. Run manually: $Py -m promem_agent init" 2 }
    Ok "OAuth completed."
} else {
    Warn "Auth state unclear; status output:"
    Write-Host $status
}

# ─── 6. Force a verbose run ────────────────────────────────────────────────
Section "6. Force agent run (live upload to Supabase)"
$runOut = & $Py -m promem_agent --verbose run 2>&1 | Out-String
$runExit = $LASTEXITCODE
Write-Host $runOut

# Parse for outcome.
if ($runExit -eq 0) {
    if ($runOut -match "received=(\d+).*?inserted=(\d+)") {
        $received = [int]$Matches[1]
        $inserted = [int]$Matches[2]
        if ($inserted -gt 0) {
            Ok "Uploaded $inserted new segment(s) to Supabase (received=$received)."
        } elseif ($received -gt 0) {
            Ok "$received segment(s) re-sent (already in cloud — server dedup'd via ON CONFLICT)."
        } else {
            Ok "Caught up — no new rows to push since last run."
        }
    } else {
        Ok "Agent run completed (exit 0)."
    }
    exit 0
}

# Failure paths.
if ($runOut -match "401\b" -or $runOut -match "re-running OAuth login flow") {
    Warn "Auth expired (HTTP 401). Re-running OAuth..."
    & $Py -m promem_agent init
    if ($LASTEXITCODE -ne 0) { Die "Re-auth failed. Check Supabase service status." 1 }
    Section "Retry after re-auth"
    & $Py -m promem_agent --verbose run
    if ($LASTEXITCODE -eq 0) { Ok "Upload resumed after re-auth."; exit 0 }
    Die "Upload still failing after re-auth. See $Log" 1
}
if ($runOut -match "Network error|Connection|Timeout|getaddrinfo|cannot resolve|resolve host") {
    Bad "Network error reaching https://promem.fly.dev"
    Write-Host "  Check: corporate firewall, VPN, proxy settings, internet connection." -ForegroundColor Yellow
    exit 1
}
Die "Upload failed for an unrecognized reason. See log: notepad $Log" 1
