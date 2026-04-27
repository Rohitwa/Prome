"""Phase 4b.1 — OAuth flow + cross-platform secret storage.

First run: opens system browser to hosted ProMem login, which performs
Google OAuth via Supabase and relays access + refresh tokens back to a
local callback listener on 127.0.0.1:53682.

Subsequent runs: reads the stored refresh_token, exchanges it for a fresh
access_token. No browser involved.

CLI:
    python3 -m promem_agent.oauth login    # full OAuth flow, browser opens
    python3 -m promem_agent.oauth refresh  # use stored refresh_token, print first 30 chars
    python3 -m promem_agent.oauth logout   # clear stored refresh_token
    python3 -m promem_agent.oauth whoami   # decode current access_token, print payload
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import httpx
import jwt
import keyring


KEYRING_SERVICE = "ProMem"
KEYRING_USER    = "refresh_token"
LOCAL_PORT      = 53682
LOCAL_REDIRECT  = f"http://127.0.0.1:{LOCAL_PORT}/callback"
BROWSER_TIMEOUT = 120  # seconds to wait for user to complete browser auth
DEFAULT_PROMEM_LOGIN_URL = "https://promem.fly.dev/login"

# Public values — same as those embedded in any Supabase frontend bundle
# (the anon key is designed for client-side use; RLS enforces data isolation
# regardless of who has the anon key). Baked in so the agent works on a fresh
# Windows install with no .env file shipped. Env vars still take precedence
# for dev / staging / testing against alt projects.
DEFAULT_SUPABASE_URL      = "https://oiuaahyuvbdxhobjpyuy.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9pdWFhaHl1dmJkeGhvYmpweXV5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzcxMzI3MTgsImV4cCI6MjA5MjcwODcxOH0."
    "vyQVbBgWwivQXXv0e1c5QqGzSZwSG-dhExZXRhP5P20"
)


class AuthError(Exception):
    """Auth flow failed (user closed browser, refresh token revoked, etc.)."""


# ── Runtime mode helpers ────────────────────────────────────────────────
def _noninteractive_mode() -> bool:
    """True when the caller must never open browser-based OAuth flows.

    Used by scheduled/background runs so auth failures are surfaced as
    explicit errors instead of spawning interactive login unexpectedly.
    """
    return os.environ.get("PROMEM_AGENT_NONINTERACTIVE", "").strip().lower() in ("1", "true", "yes")


# ── .env loader (mirrors db.py's loader so dev paths agree) ──────────────
def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if v.startswith('"') and '"' in v[1:]:
            v = v[1:].split('"', 1)[0]
        elif v.startswith("'") and "'" in v[1:]:
            v = v[1:].split("'", 1)[0]
        else:
            v = v.split(" #", 1)[0].rstrip()
        k = k.strip()
        if k:
            os.environ.setdefault(k, v)


_load_dotenv()


def _supabase_url() -> str:
    """Env var SUPABASE_URL overrides; otherwise fall back to baked default."""
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    return url if url else DEFAULT_SUPABASE_URL


def _supabase_anon_key() -> str:
    """Public anon key required as `apikey` header on /auth/v1/* requests."""
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    return key if key else DEFAULT_SUPABASE_ANON_KEY


def _promem_login_url() -> str:
    url = os.environ.get("PROMEM_LOGIN_URL", DEFAULT_PROMEM_LOGIN_URL).strip().rstrip("/")
    return url if url else DEFAULT_PROMEM_LOGIN_URL


# ── PKCE helpers (kept for compatibility/testing) ───────────────────────
def _make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


# ── Local one-shot HTTP listener for OAuth callback ──────────────────────
class _CallbackServer(http.server.HTTPServer):
    code: Optional[str] = None
    error: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer  # type hint refinement

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/agent-relay-info":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"callback": LOCAL_REDIRECT}).encode())
            return
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        access = params.get("access_token", [None])[0]
        refresh = params.get("refresh_token", [None])[0]
        code = params.get("code", [None])[0]
        err = params.get("error_description", params.get("error", [None]))[0]
        if err:
            self.server.error = err
        elif access and refresh:
            self.server.access_token = access
            self.server.refresh_token = refresh
        elif code:
            self.server.code = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        headline = f"Error: {err}" if err else "You are logged in."
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<title>ProMem — login complete</title>"
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "padding:3em;text-align:center;color:#333}h1{color:#0a7}</style>"
            f"<h1>{headline}</h1>"
            "<p>You can close this tab and return to the agent.</p>"
        )
        self.wfile.write(body.encode())

    def log_message(self, *args, **kwargs) -> None:
        pass  # silence default access logs


# ── Token exchange ───────────────────────────────────────────────────────
def _exchange_pkce(auth_code: str, code_verifier: str) -> dict:
    """POST /auth/v1/token?grant_type=pkce → access_token + refresh_token."""
    r = httpx.post(
        f"{_supabase_url()}/auth/v1/token",
        params={"grant_type": "pkce"},
        json={"auth_code": auth_code, "code_verifier": code_verifier},
        headers={"apikey": _supabase_anon_key()},
        timeout=15,
    )
    if r.status_code != 200:
        raise AuthError(f"PKCE token exchange failed: HTTP {r.status_code} — {r.text}")
    return r.json()


def _exchange_refresh(refresh_token: str) -> dict:
    """POST /auth/v1/token?grant_type=refresh_token → fresh tokens."""
    r = httpx.post(
        f"{_supabase_url()}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        json={"refresh_token": refresh_token},
        headers={"apikey": _supabase_anon_key()},
        timeout=15,
    )
    if r.status_code != 200:
        raise AuthError(f"Refresh failed: HTTP {r.status_code} — {r.text}")
    return r.json()


# ── Public API ───────────────────────────────────────────────────────────
def first_run_login() -> str:
    """Run OAuth through hosted ProMem login, then relay tokens locally.

    Browser opens https://promem.fly.dev/login.
    The hosted login page should exchange auth code for session tokens and
    relay to LOCAL_REDIRECT with access_token + refresh_token query params.
    """
    login_url = (
        f"{_promem_login_url()}#agent_callback="
        f"{urllib.parse.quote(LOCAL_REDIRECT, safe='')}"
    )

    server = _CallbackServer(("127.0.0.1", LOCAL_PORT), _CallbackHandler)
    server.timeout = 0.5
    print(f"Opening hosted login {_promem_login_url()} (listener on {LOCAL_REDIRECT})",
          file=sys.stderr)
    webbrowser.open(login_url)

    ticks = BROWSER_TIMEOUT * 2  # each handle_request waits up to 0.5s
    while (
        ticks > 0
        and server.error is None
        and server.access_token is None
        and server.refresh_token is None
        and server.code is None
    ):
        server.handle_request()
        ticks -= 1
    server.server_close()

    if server.error:
        raise AuthError(f"Supabase returned error: {server.error}")

    if server.code and not server.refresh_token:
        raise AuthError(
            "Hosted login returned code but did not relay tokens to local callback. "
            "Update https://promem.fly.dev/login token relay flow."
        )

    refresh_token = server.refresh_token
    access_token = server.access_token
    if not refresh_token:
        raise AuthError(f"Timed out waiting for browser callback after {BROWSER_TIMEOUT}s")

    # If callback omitted access token, derive it from refresh once.
    if not access_token:
        tokens = _exchange_refresh(refresh_token)
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token") or refresh_token
        if not access_token:
            raise AuthError(f"Refresh response missing access_token: {tokens}")

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, refresh_token)
    return access_token


def get_access_token() -> str:
    """Return a fresh access_token. Tries refresh first; falls back to OAuth
    if no refresh_token is stored OR if refresh fails (revoked/expired).

    In non-interactive mode (PROMEM_AGENT_NONINTERACTIVE=true), browser login
    fallback is disabled and AuthError is raised instead."""
    noninteractive = _noninteractive_mode()

    try:
        refresh_token = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as e:
        raise AuthError(f"Keyring read failed: {e}") from e

    if not refresh_token:
        if noninteractive:
            raise AuthError("No refresh_token in keyring (run `promem_agent init` interactively).")
        return first_run_login()
    try:
        tokens = _exchange_refresh(refresh_token)
    except AuthError as e:
        if noninteractive:
            raise AuthError(
                f"Refresh failed in non-interactive mode: {e}. "
                "Run `promem_agent init` interactively."
            ) from e
        return first_run_login()
    access_token = tokens.get("access_token")
    new_refresh = tokens.get("refresh_token")
    if not access_token:
        raise AuthError(f"Refresh response missing access_token: {tokens}")
    if new_refresh and new_refresh != refresh_token:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, new_refresh)
    return access_token


def logout() -> None:
    """Clear the stored refresh_token. Forces re-login on next get_access_token()."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass  # already gone, or backend hiccup — fine either way


def whoami(token: Optional[str] = None) -> dict:
    """Decode (without signature verification) the access_token JWT payload."""
    if token is None:
        token = get_access_token()
    return jwt.decode(token, options={"verify_signature": False})


# ── CLI ──────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "refresh"
    try:
        if cmd == "login":
            tok = first_run_login()
            payload = jwt.decode(tok, options={"verify_signature": False})
            print(f"Logged in as {payload.get('email', '?')} ({payload.get('sub', '?')})")
            return 0
        if cmd == "refresh":
            tok = get_access_token()
            print(f"access_token: {tok[:30]}... (length={len(tok)})")
            return 0
        if cmd == "logout":
            logout()
            print("Logged out (refresh_token cleared from keyring).")
            return 0
        if cmd == "whoami":
            payload = whoami()
            print(json.dumps(payload, indent=2, default=str))
            return 0
        print(f"Unknown command: {cmd}. Try login | refresh | logout | whoami", file=sys.stderr)
        return 2
    except AuthError as e:
        print(f"AuthError: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
