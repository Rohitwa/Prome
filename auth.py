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

import jwt
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
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )
    return user_id


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    promem_session: Optional[str] = Cookie(default=None),
) -> str:
    """FastAPI dependency: verify JWT, return user_id (UUID as string).
    Raises 401 if no valid token in either Authorization header or cookie."""
    token = None
    if creds is not None:
        token = creds.credentials
    elif promem_session:
        token = promem_session
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — provide Authorization: Bearer or promem_session cookie",
        )
    user_id = _verify(token)
    _ensure_user_seeded(user_id)
    return user_id
