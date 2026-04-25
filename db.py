"""Postgres connection helpers for ProMem.

Drop-in shape replacement for the old sqlite `_conn()` — psycopg v3 connections
expose `.execute().fetchall()` with the same `row['col']` access (via dict_row)
that the SQLite code uses, so query-site refactors stay mechanical.

Pool-backed: one ConnectionPool per process, lazy-init on first use.

Smoke test:
    PROMEM_DB_URL="postgresql://..." python3 db.py
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _db_url() -> str:
    url = os.environ.get("PROMEM_DB_URL", "").strip()
    if not url:
        raise RuntimeError(
            "PROMEM_DB_URL is not set. Get the connection string from "
            "Supabase Dashboard → Project Settings → Database → "
            "Connection string → URI, then export it:\n\n"
            '  export PROMEM_DB_URL="postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres"\n'
        )
    return url


_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_db_url(),
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Acquire a pooled connection. Use as `with conn() as c: ...`.
    Auto-commits on exit if no exception, rolls back if there is one.
    Connection is returned to the pool either way."""
    with get_pool().connection() as c:
        yield c


def close_pool() -> None:
    """Shut the pool down — call from FastAPI lifespan exit."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


if __name__ == "__main__":
    # Quick connectivity check.
    with conn() as c:
        row = c.execute("SELECT now() AS now, current_user AS user").fetchone()
        print(f"connected · now={row['now']} · user={row['user']}")
        tables = c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
        print(f"public tables ({len(tables)}): {', '.join(t['tablename'] for t in tables)}")
