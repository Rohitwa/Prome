ProMem - Windows installer
==========================

This zip installs both pieces of the ProMem productivity pipeline:

  1. productivity-tracker  Captures screenshots of your work, runs OpenAI
                           Vision on them, writes summaries to a local
                           tracker.db. Runs continuously after logon.

  2. promem_agent          Uploads new tracker.db rows to the ProMem cloud
                           every 5 minutes via the cloud /api/upload-segments
                           endpoint. Cloud orchestrator turns them into
                           wiki pages at https://promem.fly.dev/wiki.

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
     - Install productivity-tracker (no chromadb / PMIS extras - lean mode)
     - Register two Task Scheduler entries:
         "ProMem Agent"   -> every 5 min (uploads to cloud)
         "ProMem Tracker" -> at logon (long-lived capture loop)
     - Open your browser once for Google login
     - Start the tracker right away
     - Open https://promem.fly.dev/wiki in your default browser

After install, the tracker runs continuously in the background, and your
wiki at promem.fly.dev/wiki populates as data accumulates (data appears
within ~30 minutes; full wiki cards rebuild twice daily).

OPENAI KEY
----------
You do NOT need to provide your own OpenAI API key. The tracker routes
all OpenAI calls through the ProMem Cloudflare Worker
(promem-openai-proxy.yantrai.workers.dev), which authenticates each call
with your Supabase JWT. The Worker holds the centralized OpenAI key.

STATUS
------
Open a Command Prompt and run:
  cd /d "%LOCALAPPDATA%\ProMem"
  .venv\Scripts\python.exe -m promem_agent status

Expected output:
  auth:       ok logged in as <your-email>
  tracker.db: ok  (path)
  state:      last_uploaded_ts=<recent-timestamp>

UNINSTALL
---------
Double-click  %LOCALAPPDATA%\ProMem\uninstall.bat
(Refresh token in Credential Manager must be removed manually:
 Control Panel -> Credential Manager -> Windows Credentials -> 'ProMem')
