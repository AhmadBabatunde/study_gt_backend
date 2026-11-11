"""
PostgreSQL connection pool using psycopg2.
Handles connection lifecycle for the FastAPI application.
"""

import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from config import DATABASE_URL

# Threaded connection pool (min 2, max 10 connections)
_pool = None


def init_pool():
    """Initialize the connection pool. Called on app startup."""
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DATABASE_URL
        )
    return _pool


def close_pool():
    """Close all connections in the pool. Called on app shutdown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    """Context manager that provides a database connection from the pool."""
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor(cursor_factory=None):
    """Context manager providing a cursor with auto-commit/rollback."""
    with get_conn() as conn:
        factory = cursor_factory or psycopg2.extras.RealDictCursor
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
        finally:
            cur.close()


def fetch_one(query, params=None):
    """Execute query and return a single row as dict."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def fetch_all(query, params=None):
    """Execute query and return all rows as list of dicts."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def execute(query, params=None):
    """Execute a query (INSERT/UPDATE/DELETE) and return rowcount."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount


def execute_returning(query, params=None):
    """Execute a query with RETURNING clause and return the row."""
    with get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()
