"""
backend/data/db.py
==================
Neon PostgreSQL database layer for the AI Finance Controller.

Replaces the previous in-memory SQLite implementation with a psycopg2
ThreadedConnectionPool backed by a Neon serverless PostgreSQL endpoint.

Key contracts (unchanged from SQLite version)
---------------------------------------------
- init_db(batch)   → create schema, truncate, seed from batch; return pool
- get_connection() → context-manager that yields a pooled psycopg2 connection
- query(sql, params) → list[dict]  (SELECT helper)
- execute(sql, params) → int       (DML helper, returns rowcount)
- table_counts()   → dict[str, int]
- close_pool()     → release all pool connections on shutdown

SQL dialect differences vs SQLite
-----------------------------------
- Placeholders : ?  →  %s
- Numeric type  : REAL → NUMERIC(15,2)
- PRAGMA removed
- Table listing : sqlite_master → information_schema.tables
- Date storage  : still ISO-8601 TEXT (consistent with Pydantic models)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Generator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from data.schema import DataBatch


# ---------------------------------------------------------------------------
# DDL — PostgreSQL dialect
# ---------------------------------------------------------------------------

_DDL_STATEMENTS = [
    # Razorpay payments (simulates Razorpay Payments + Settlements API)
    """
    CREATE TABLE IF NOT EXISTS razorpay_payments (
        pay_id          TEXT PRIMARY KEY,
        order_id        TEXT        NOT NULL,
        captured_at     TEXT        NOT NULL,        -- ISO-8601 date
        amount          NUMERIC(15,2) NOT NULL,
        currency        TEXT        NOT NULL DEFAULT 'INR',
        method          TEXT        NOT NULL,
        status          TEXT        NOT NULL,
        settlement_id   TEXT        NOT NULL,
        settlement_date TEXT        NOT NULL,        -- ISO-8601 date
        settlement_utr  TEXT        NOT NULL,
        fee             NUMERIC(15,2) NOT NULL,
        tax             NUMERIC(15,2) NOT NULL,
        net_amount      NUMERIC(15,2) NOT NULL,
        error_type      TEXT        NOT NULL DEFAULT 'clean'
    )
    """,
    # Bank statement credits
    """
    CREATE TABLE IF NOT EXISTS bank_statements (
        txn_id          TEXT PRIMARY KEY,
        value_date      TEXT        NOT NULL,        -- ISO-8601 date
        amount          NUMERIC(15,2) NOT NULL,
        description     TEXT        NOT NULL,
        bank_ref        TEXT        NOT NULL,        -- = settlement_utr
        currency        TEXT        NOT NULL DEFAULT 'INR',
        settlement_id   TEXT                         -- FK → settlements
    )
    """,
    # Internal accounting ledger
    """
    CREATE TABLE IF NOT EXISTS ledger_entries (
        entry_id        TEXT PRIMARY KEY,
        date            TEXT        NOT NULL,        -- ISO-8601 date
        amount          NUMERIC(15,2) NOT NULL,
        narration       TEXT        NOT NULL,
        account_code    TEXT        NOT NULL,
        internal_ref    TEXT        NOT NULL         -- settlement_id or pay_id
    )
    """,
    # Razorpay settlement summary
    """
    CREATE TABLE IF NOT EXISTS settlements (
        settlement_id   TEXT PRIMARY KEY,
        settlement_date TEXT        NOT NULL,
        total_amount    NUMERIC(15,2) NOT NULL,
        num_payments    INTEGER     NOT NULL,
        status          TEXT        NOT NULL DEFAULT 'pending'
    )
    """,
    # Match results (written by Reconciler agent — Phase 2)
    """
    CREATE TABLE IF NOT EXISTS match_results (
        pay_id                  TEXT PRIMARY KEY,
        entry_id                TEXT,
        txn_id                  TEXT,
        match_type              TEXT        NOT NULL DEFAULT 'unmatched',
        confidence              NUMERIC(5,4) NOT NULL DEFAULT 0.0,
        delta                   NUMERIC(15,2),
        status                  TEXT        NOT NULL DEFAULT 'exception',
        ground_truth_error_type TEXT        NOT NULL DEFAULT 'clean'
    )
    """,
    # Exception records (Phase 2)
    """
    CREATE TABLE IF NOT EXISTS exceptions (
        exception_id     TEXT PRIMARY KEY,
        source           TEXT NOT NULL,
        record_id        TEXT NOT NULL,
        reason           TEXT NOT NULL,
        agent_reasoning  TEXT NOT NULL,
        suggested_action TEXT NOT NULL
    )
    """,
]

_TRUNCATE_ORDER = [
    "match_results",
    "exceptions",
    "bank_statements",
    "ledger_entries",
    "settlements",
    "razorpay_payments",
]


# ---------------------------------------------------------------------------
# Module-level connection pool (None until init_db() is called)
# ---------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    if _pool is None:
        raise RuntimeError(
            "Database not initialised. Call init_db() before accessing the pool."
        )
    return _pool


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Yield a pooled psycopg2 connection, returning it to the pool on exit.

    Usage::

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def _build_dsn() -> str:
    """
    Return the PostgreSQL DSN.

    Priority:
      1. DATABASE_URL env var  (Neon dashboard provides this)
      2. Raises EnvironmentError if not set
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise EnvironmentError(
            "DATABASE_URL environment variable is not set. "
            "Add it to backend/.env (see .env.example)."
        )
    return url


def init_db(batch: DataBatch, *, seed: bool = True) -> psycopg2.pool.ThreadedConnectionPool:
    """
    Initialise the Neon PostgreSQL connection pool.

    Steps
    -----
    1. Build DSN from DATABASE_URL env var.
    2. Create a ThreadedConnectionPool (min=1, max=10).
    3. Run DDL (CREATE TABLE IF NOT EXISTS) — idempotent.
    4. If *seed* is True (default): TRUNCATE all tables and load *batch*.

    Returns the pool (also stored as module-level singleton).

    Calling init_db() again replaces the pool and reseeds — useful in tests.

    Parameters
    ----------
    batch:
        The DataBatch produced by the generator.
    seed:
        Set to False to skip truncate+reload (e.g. when DATABASE_URL points
        to a database that already has live data).
    """
    global _pool

    dsn = _build_dsn()

    new_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=dsn,
    )

    # Create schema — acquire a connection directly from the new pool
    conn = new_pool.getconn()
    try:
        with conn.cursor() as cur:
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        new_pool.putconn(conn)

    _pool = new_pool

    if seed:
        _seed_batch(batch)

    return _pool


def _seed_batch(batch: DataBatch) -> None:
    """Truncate all tables and insert the synthetic batch."""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            # Truncate in dependency order
            cur.execute(
                f"TRUNCATE TABLE {', '.join(_TRUNCATE_ORDER)} RESTART IDENTITY CASCADE"
            )
            _insert_payments(cur, batch)
            _insert_bank_txns(cur, batch)
            _insert_ledger_entries(cur, batch)
            _insert_settlements(cur, batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Batch inserters
# ---------------------------------------------------------------------------

def _d(value: Decimal) -> float:
    """Convert Decimal → float for psycopg2 NUMERIC binding."""
    return float(value)


def _insert_payments(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    rows = [
        (
            p.pay_id,
            p.order_id,
            p.captured_at.isoformat(),
            _d(p.amount),
            p.currency,
            p.method.value,
            p.status.value,
            p.settlement_id,
            p.settlement_date.isoformat(),
            p.settlement_utr,
            _d(p.fee),
            _d(p.tax),
            _d(p.net_amount),
            p.error_type.value,
        )
        for p in batch.payments
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO razorpay_payments
        (pay_id, order_id, captured_at, amount, currency, method, status,
         settlement_id, settlement_date, settlement_utr, fee, tax, net_amount, error_type)
        VALUES %s
        ON CONFLICT (pay_id) DO NOTHING
        """,
        rows,
    )


def _insert_bank_txns(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    rows = [
        (
            t.txn_id,
            t.value_date.isoformat(),
            _d(t.amount),
            t.description,
            t.bank_ref,
            t.currency,
            t.settlement_id,
        )
        for t in batch.bank_txns
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO bank_statements
        (txn_id, value_date, amount, description, bank_ref, currency, settlement_id)
        VALUES %s
        ON CONFLICT (txn_id) DO NOTHING
        """,
        rows,
    )


def _insert_ledger_entries(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    rows = [
        (
            e.entry_id,
            e.date.isoformat(),
            _d(e.amount),
            e.narration,
            e.account_code,
            e.internal_ref,
        )
        for e in batch.ledger_entries
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO ledger_entries
        (entry_id, date, amount, narration, account_code, internal_ref)
        VALUES %s
        ON CONFLICT (entry_id) DO NOTHING
        """,
        rows,
    )


def _insert_settlements(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    rows = [
        (
            s.settlement_id,
            s.settlement_date.isoformat(),
            _d(s.total_amount),
            s.num_payments,
            s.status.value,
        )
        for s in batch.settlements
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO settlements
        (settlement_id, settlement_date, total_amount, num_payments, status)
        VALUES %s
        ON CONFLICT (settlement_id) DO NOTHING
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def close_pool() -> None:
    """
    Close all connections in the pool.
    Call this from the FastAPI lifespan shutdown hook.
    """
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


# ---------------------------------------------------------------------------
# Public query helpers (called by tool layer in Phase 2 and API routes)
# ---------------------------------------------------------------------------

def query(sql: str, params: tuple = ()) -> list[dict]:
    """
    Execute a SELECT and return rows as plain dicts.

    Uses RealDictCursor so column names are preserved exactly.
    Placeholder character: %s  (psycopg2 standard).
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or None)
            return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    """
    Execute a DML statement (INSERT / UPDATE / DELETE).
    Returns the number of rows affected (cursor.rowcount).
    Placeholder character: %s.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            return cur.rowcount


def table_counts() -> dict[str, int]:
    """Return row counts for every main table — used by /health and tests."""
    tables = [
        "razorpay_payments",
        "bank_statements",
        "ledger_entries",
        "settlements",
        "match_results",
        "exceptions",
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")  # noqa: S608
                counts[t] = cur.fetchone()[0]
    return counts
