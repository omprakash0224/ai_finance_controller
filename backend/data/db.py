"""
backend/data/db.py
==================
In-memory SQLite database for the AI Finance Controller.

Responsibilities
----------------
1. Create all tables (DDL).
2. Load a DataBatch into SQLite on server startup.
3. Expose a connection accessor for agents and tool layer.

All data lives in a module-level connection so it is shared across requests
within a single server process.  For tests, call `init_db(batch)` with a
freshly generated batch to get an isolated connection.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Optional

from data.schema import DataBatch


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
-- Razorpay payments (simulates Razorpay Payments + Settlements API)
CREATE TABLE IF NOT EXISTS razorpay_payments (
    pay_id          TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    captured_at     TEXT NOT NULL,      -- ISO-8601 date
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'INR',
    method          TEXT NOT NULL,
    status          TEXT NOT NULL,
    settlement_id   TEXT NOT NULL,
    settlement_date TEXT NOT NULL,      -- ISO-8601 date
    settlement_utr  TEXT NOT NULL,
    fee             REAL NOT NULL,
    tax             REAL NOT NULL,
    net_amount      REAL NOT NULL,
    error_type      TEXT NOT NULL DEFAULT 'clean'
);

-- Bank statement credits
CREATE TABLE IF NOT EXISTS bank_statements (
    txn_id          TEXT PRIMARY KEY,
    value_date      TEXT NOT NULL,      -- ISO-8601 date
    amount          REAL NOT NULL,
    description     TEXT NOT NULL,
    bank_ref        TEXT NOT NULL,      -- = settlement_utr from razorpay_payments
    currency        TEXT NOT NULL DEFAULT 'INR',
    settlement_id   TEXT             -- FK → settlements
);

-- Internal accounting ledger
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id        TEXT PRIMARY KEY,
    date            TEXT NOT NULL,      -- ISO-8601 date
    amount          REAL NOT NULL,
    narration       TEXT NOT NULL,
    account_code    TEXT NOT NULL,
    internal_ref    TEXT NOT NULL       -- settlement_id or pay_id
);

-- Razorpay settlement summary (one row per settlement_id)
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id   TEXT PRIMARY KEY,
    settlement_date TEXT NOT NULL,
    total_amount    REAL NOT NULL,
    num_payments    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
);

-- Match results written by the Reconciler agent (Phase 2)
CREATE TABLE IF NOT EXISTS match_results (
    pay_id                  TEXT PRIMARY KEY,
    entry_id                TEXT,
    txn_id                  TEXT,
    match_type              TEXT NOT NULL DEFAULT 'unmatched',
    confidence              REAL NOT NULL DEFAULT 0.0,
    delta                   REAL,
    status                  TEXT NOT NULL DEFAULT 'exception',
    ground_truth_error_type TEXT NOT NULL DEFAULT 'clean'
);

-- Exception records (Phase 2)
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id     TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    record_id        TEXT NOT NULL,
    reason           TEXT NOT NULL,
    agent_reasoning  TEXT NOT NULL,
    suggested_action TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Module-level shared connection (None until init_db() is called)
# ---------------------------------------------------------------------------

_connection: Optional[sqlite3.Connection] = None


def get_connection() -> sqlite3.Connection:
    """
    Return the active in-memory SQLite connection.
    Raises RuntimeError if init_db() has not been called yet.
    """
    if _connection is None:
        raise RuntimeError(
            "Database not initialised. Call init_db() before accessing the connection."
        )
    return _connection


def _decimal_to_float(value: Decimal) -> float:
    return float(value)


def init_db(batch: DataBatch) -> sqlite3.Connection:
    """
    Create all tables and load *batch* into the in-memory SQLite database.
    Returns the connection (also stored as the module-level singleton).

    Calling init_db() a second time drops all data and reloads from *batch*.
    This is intentional — it allows tests to call it with isolated batches.
    """
    global _connection

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_DDL)

    _load_payments(conn, batch)
    _load_bank_txns(conn, batch)
    _load_ledger_entries(conn, batch)
    _load_settlements(conn, batch)

    conn.commit()
    _connection = conn
    return conn


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_payments(conn: sqlite3.Connection, batch: DataBatch) -> None:
    rows = [
        (
            p.pay_id,
            p.order_id,
            p.captured_at.isoformat(),
            _decimal_to_float(p.amount),
            p.currency,
            p.method.value,
            p.status.value,
            p.settlement_id,
            p.settlement_date.isoformat(),
            p.settlement_utr,
            _decimal_to_float(p.fee),
            _decimal_to_float(p.tax),
            _decimal_to_float(p.net_amount),
            p.error_type.value,
        )
        for p in batch.payments
    ]
    conn.executemany(
        """
        INSERT INTO razorpay_payments VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _load_bank_txns(conn: sqlite3.Connection, batch: DataBatch) -> None:
    rows = [
        (
            t.txn_id,
            t.value_date.isoformat(),
            _decimal_to_float(t.amount),
            t.description,
            t.bank_ref,
            t.currency,
            t.settlement_id,
        )
        for t in batch.bank_txns
    ]
    conn.executemany(
        """
        INSERT INTO bank_statements VALUES (?,?,?,?,?,?,?)
        """,
        rows,
    )


def _load_ledger_entries(conn: sqlite3.Connection, batch: DataBatch) -> None:
    rows = [
        (
            e.entry_id,
            e.date.isoformat(),
            _decimal_to_float(e.amount),
            e.narration,
            e.account_code,
            e.internal_ref,
        )
        for e in batch.ledger_entries
    ]
    conn.executemany(
        """
        INSERT INTO ledger_entries VALUES (?,?,?,?,?,?)
        """,
        rows,
    )


def _load_settlements(conn: sqlite3.Connection, batch: DataBatch) -> None:
    rows = [
        (
            s.settlement_id,
            s.settlement_date.isoformat(),
            _decimal_to_float(s.total_amount),
            s.num_payments,
            s.status.value,
        )
        for s in batch.settlements
    ]
    conn.executemany(
        """
        INSERT INTO settlements VALUES (?,?,?,?,?)
        """,
        rows,
    )


# ---------------------------------------------------------------------------
# Convenience query helpers (used by tool layer in Phase 2)
# ---------------------------------------------------------------------------

def query(sql: str, params: tuple = ()) -> list[dict]:
    """
    Execute a SELECT and return rows as plain dicts.
    Raises sqlite3.Error on bad SQL.
    """
    conn = get_connection()
    cur = conn.execute(sql, params)
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    """
    Execute a DML statement (INSERT / UPDATE / DELETE).
    Returns the number of rows affected.
    """
    conn = get_connection()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def table_counts() -> dict[str, int]:
    """Return row counts for every main table — useful for health checks."""
    tables = [
        "razorpay_payments",
        "bank_statements",
        "ledger_entries",
        "settlements",
        "match_results",
        "exceptions",
    ]
    conn = get_connection()
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in tables
    }
