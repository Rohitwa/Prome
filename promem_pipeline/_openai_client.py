"""Shared OpenAI credential resolution for the orchestrator.

Returns the (api_key, base_url) pair to use for raw httpx calls in
classify.py / synthesis.py. In proxy mode (OPENAI_USE_PROXY=true),
substitutes a fresh Supabase JWT + the Promem Cloudflare Worker URL
so cloud installs don't need per-user OpenAI keys.

Mode selection:
    OPENAI_USE_PROXY = "1" / "true" / "yes"   → proxy mode
    anything else (default)                   → direct mode (current behavior)

Optional:
    PROMEM_OPENAI_PROXY_URL = "https://..."   → custom Worker base URL
"""

from __future__ import annotations

import os
import time

DEFAULT_PROXY_URL = "https://promem-openai-proxy.yantrai.workers.dev/v1"
DEFAULT_DIRECT_URL = "https://api.openai.com/v1"
_TOKEN_REFRESH_SAFETY_SEC = 60

_cached_token: str | None = None
_cached_token_exp: float = 0.0


def _proxy_mode() -> bool:
    return os.environ.get("OPENAI_USE_PROXY", "").strip().lower() in ("1", "true", "yes")


def _proxy_jwt() -> str:
    """Return a fresh Supabase access_token, cached in process to avoid
    refreshing on every call. Refreshed when within ~60s of expiry."""
    global _cached_token, _cached_token_exp
    if time.time() > _cached_token_exp - _TOKEN_REFRESH_SAFETY_SEC:
        from promem_agent import oauth   # lazy — only required in proxy mode
        import jwt as pyjwt
        _cached_token = oauth.get_access_token()
        try:
            payload = pyjwt.decode(_cached_token, options={"verify_signature": False})
            _cached_token_exp = float(payload.get("exp", time.time() + 3600))
        except Exception:
            _cached_token_exp = time.time() + 3600
    return _cached_token   # type: ignore[return-value]


def credentials() -> tuple[str, str]:
    """Return (api_key, base_url). In proxy mode the api_key is a Supabase
    JWT and base_url points at the Cloudflare Worker. In direct mode the
    key comes from OPENAI_API_KEY (raises RuntimeError if unset) and
    base_url comes from OPENAI_BASE_URL (default api.openai.com/v1)."""
    if _proxy_mode():
        base = os.environ.get("PROMEM_OPENAI_PROXY_URL", DEFAULT_PROXY_URL).rstrip("/")
        return _proxy_jwt(), base

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    base = os.environ.get("OPENAI_BASE_URL", DEFAULT_DIRECT_URL).rstrip("/")
    return key, base
