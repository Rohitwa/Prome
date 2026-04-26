"""Supabase JWT verification + FastAPI auth dependency.

Verifies HS256-signed JWTs issued by Supabase Auth. Returns the user UUID
(`sub` claim) for use in user_id-scoped queries.

Accepts the JWT either via:
  - Authorization: Bearer <token>           (API clients, the widget)
  - Cookie:        promem_session=<token>   (browser, set by login page in Phase 5)

On first activity per user, idempotently seeds the default SC registry
via the `seed_sc_registry_for_user(uuid)` Postgres function.
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

import jwt
from jwt import PyJWKClient
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import db


bearer_scheme = HTTPBearer(auto_error=False)


def _jwt_secret() -> str:
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "SUPABASE_JWT_SECRET is not set. Get it from Supabase Dashboard → "
            "Project Settings → API → JWT Settings → 'JWT Secret'."
        )
    return secret


_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    """Return a cached PyJWKClient pointed at this project's JWKS endpoint.
    Used for ES256/RS256 — Supabase's modern asymmetric signing scheme."""
    global _jwks_client
    if _jwks_client is None:
        supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        if not supabase_url:
            raise RuntimeError(
                "SUPABASE_URL is not set. Required for verifying ES256/RS256 "
                "JWTs against the project's JWKS endpoint."
            )
        _jwks_client = PyJWKClient(
            f"{supabase_url}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


_seeded_users: set[str] = set()


def _ensure_user_seeded(user_id: str) -> None:
    """Insert the 8 default super-contexts for a fresh user. Idempotent."""
    if user_id in _seeded_users:
        return
    try:
        with db.conn() as c:
            c.execute("SELECT seed_sc_registry_for_user(%s)", (user_id,))
        _seeded_users.add(user_id)
    except Exception:
        # Don't fail auth if seeding hiccups — let the request through and
        # we'll retry on the next request from this user.
        pass


def _verify(token: str) -> str:
    """Decode + validate a Supabase JWT, return user UUID (the `sub` claim).

    Auto-detects the algorithm from the token header:
      - ES256 / RS256: asymmetric — fetch public key from Supabase's JWKS
        endpoint (modern projects, default since late 2025).
      - HS256: symmetric — use SUPABASE_JWT_SECRET (legacy projects).
    """
    header = jwt.get_unverified_header(token)
    alg = (header.get("alg") or "").upper()

    if alg in ("ES256", "RS256"):
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token).key
        payload = jwt.decode(
            token, signing_key,
            algorithms=[alg],
            audience="authenticated",
        )
    elif alg == "HS256":
        payload = jwt.decode(
            token, _jwt_secret(),
            algorithms=["HS256"],
            audience="authenticated",
        )
    else:
        raise jwt.InvalidAlgorithmError(f"Unsupported JWT algorithm: {alg!r}")

    user_id = payload.get("sub")
    if not user_id:
        raise jwt.InvalidTokenError("Token missing 'sub' claim")
    return user_id


def _fail(request: Request, msg: str) -> None:
    """Raise the right error: 303 → /login for browsers, 401 for APIs."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        next_url = quote(str(request.url), safe="")
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={next_url}"},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)


def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    promem_session: Optional[str] = Cookie(default=None),
) -> str:
    """FastAPI dependency: verify JWT, return user_id (UUID as string).
    Browsers without auth get redirected to /login?next=<original-url>;
    API clients get 401."""
    token = creds.credentials if creds is not None else promem_session
    if not token:
        _fail(request, "Not authenticated — provide Authorization: Bearer or promem_session cookie")
    try:
        user_id = _verify(token)
    except jwt.PyJWTError as e:
        _fail(request, f"Invalid token: {e}")
    _ensure_user_seeded(user_id)
    return user_id
