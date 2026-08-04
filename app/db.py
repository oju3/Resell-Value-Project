"""Database access for the API: a client-side connection pool and the
FastAPI dependency that hands out connections.

Every query in this application goes through get_conn() below, so the
authorization note in that function's docstring applies to all of them.

Why a pool, when scripts/ does not use one
------------------------------------------
The scripts (apify_backfill.py, refresh_comps.py) open one connection, do
their work, and exit. A web server handles many requests over a long life,
and opening a fresh connection per request means a TCP + TLS handshake to
Supabase every time -- tens of milliseconds of pure overhead on every call.
ThreadedConnectionPool opens a few connections up front and lends them out.

DATABASE_URL already points at Supabase's *transaction* pooler (port 6543;
port 5432 hangs from this network). That is server-side pooling and it does
not remove the client-side handshake cost, which is what this pool addresses.
Transaction-mode pooling also means server-side session state does not
survive between transactions -- one of the reasons this project uses plain
psycopg2 with explicit SQL rather than a session-stateful ORM.

Never prints or logs DATABASE_URL, including in error paths -- a DSN
contains credentials.
"""
import logging
from typing import Optional

import psycopg2
from psycopg2 import pool as pg_pool

from app.config import DATABASE_URL, DB_POOL_MAX_CONN, DB_POOL_MIN_CONN

logger = logging.getLogger(__name__)

# Module-level so the pool is shared across all requests. Created in
# init_pool() at application startup rather than at import, so importing this
# module (for tests, tooling) does not by itself open database connections.
_pool: Optional[pg_pool.ThreadedConnectionPool] = None


def init_pool():
    """Opens the pool. Called once from the app lifespan handler at startup."""
    global _pool
    if _pool is not None:
        return
    _pool = pg_pool.ThreadedConnectionPool(
        DB_POOL_MIN_CONN, DB_POOL_MAX_CONN, dsn=DATABASE_URL
    )


def close_pool():
    """Closes every pooled connection. Called from the lifespan handler at
    shutdown so the process does not leave connections open on Supabase."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def get_conn():
    """FastAPI dependency: yields a pooled connection, always returns it.

    AUTHORIZATION -- READ BEFORE WRITING PORTFOLIO QUERIES
    ------------------------------------------------------
    Row Level Security is NOT what protects user data on this code path.
    Every query written against this connection must filter by user id in
    application code:

        WHERE user_id = %s   -- the id from app.auth.get_current_user_id

    Why: the RLS policies on owned_sneakers in schema.sql are written as
    `auth.uid() = user_id`. auth.uid() reads the JWT claims out of Postgres
    session settings that *Supabase's PostgREST layer* populates on each
    request. This API connects straight to Postgres with psycopg2 using
    DATABASE_URL, so nothing sets those claims -- auth.uid() is NULL here,
    and the connection role bypasses RLS besides. A portfolio query written
    without an explicit user_id filter will silently return or modify every
    user's rows. It will not error, and RLS will not stop it.

    This is a deliberate design decision, not an oversight: authorization is
    enforced in application code, and RLS is kept as defence-in-depth for any
    access that arrives through PostgREST or the Supabase client instead.
    The alternative -- issuing SET LOCAL request.jwt.claims per transaction so
    auth.uid() resolves -- was rejected because per-transaction session state
    interacts badly with transaction-mode pgbouncer and is easy to get subtly
    wrong. Do not assume a future refactor has changed this; check here first.
    """
    if _pool is None:
        raise RuntimeError("Connection pool is not initialised")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        # Returned in a finally so a failing endpoint cannot leak a
        # connection out of the pool. rollback() first: psycopg2 opens a
        # transaction implicitly, and returning a connection with an open
        # transaction would hand the next request dirty state.
        try:
            conn.rollback()
        except psycopg2.Error:
            logger.warning("Rollback failed while returning connection to pool")
        _pool.putconn(conn)


def check_database() -> bool:
    """Runs a real query and reports whether Postgres answered.

    Used by /health. Takes its own connection rather than using the get_conn
    dependency: if the pool itself is the thing that is broken, a dependency
    would raise before the endpoint body ran and produce a 500, which is
    exactly the failure a health check is supposed to report cleanly.

    Logs the failure server-side and returns a bool -- the caller must not
    surface the exception text, which can contain connection details.
    """
    if _pool is None:
        logger.error("Health check failed: connection pool is not initialised")
        return False
    conn = None
    try:
        conn = _pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.rollback()
        return True
    except Exception:
        logger.exception("Health check database query failed")
        return False
    finally:
        if conn is not None:
            _pool.putconn(conn)
