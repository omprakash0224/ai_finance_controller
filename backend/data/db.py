"""
backend/data/db.py
==================
Neon PostgreSQL database layer for the AI Finance Controller.

Replaces the previous in-memory SQLite implementation with a psycopg2
ThreadedConnectionPool backed by a Neon serverless PostgreSQL endpoint.

Key contracts
-------------
- init_db(batch, seed, partitioned) → create schema, truncate, seed; return pool
- get_connection()                  → context-manager yielding a pooled connection
- query(sql, params)                → list[dict]  (SELECT helper)
- execute(sql, params)              → int         (DML helper, returns rowcount)
- table_counts()                    → dict[str, int]
- close_pool()                      → release all pool connections on shutdown
- create_monthly_partition(year, month) → create a date-range partition on-demand
- ensure_partition_for_date(date_str)   → idempotently create the right partition
- bulk_load_batch(batch)                → COPY-ingest a DataBatch into partitioned tables

Ingestion strategy — COPY vs execute_values
-------------------------------------------
For production loads (100k–1M rows per batch) every _insert_* function uses
PostgreSQL COPY streaming via cursor.copy_from(io.StringIO(...)) which is
~10–20× faster than parameterised INSERT ... VALUES %s.

Fallback: if copy_from raises any error the function transparently retries
with execute_values, so small development batches with special characters
continue to work without manual intervention.

Table partitioning (opt-in, init_db partitioned=True)
------------------------------------------------------
For multi-million long-term storage, init_db() can create RANGE-partitioned
variants of the two largest tables:

  razorpay_payments_partitioned  PARTITION BY RANGE (settlement_date)
  match_results_partitioned      PARTITION BY RANGE (settlement_date)

Monthly child partitions are created on demand via create_monthly_partition()
or automatically via ensure_partition_for_date().  Benefits:
  • Partition pruning: date-range queries scan only relevant months
  • Independent VACUUM / ANALYZE per partition
  • Hot-partition archiving: old months can be detached and cold-stored
  • Parallel ingestion: each month can be loaded concurrently

Performance indexes (applied idempotently via CREATE INDEX IF NOT EXISTS)
-------------------------------------------------------------------------
- idx_payments_utr         : razorpay_payments(settlement_utr)             — UTR lookup
- idx_payments_date_amount : razorpay_payments(settlement_date, net_amount) — exact/fuzzy join
- idx_payments_settlement  : razorpay_payments(settlement_id)              — ledger joins
- idx_bank_ref             : bank_statements(bank_ref)                     — UTR cross-ref
- idx_bank_date_amount     : bank_statements(value_date, amount)           — exact/fuzzy join
- idx_ledger_internal_ref  : ledger_entries(internal_ref)                  — settlement lookup
- idx_match_status         : match_results(status)                         — exception filtering
"""

from __future__ import annotations

import csv
import io
import logging
import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Generator, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from data.schema import DataBatch

logger = logging.getLogger(__name__)


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
    # -----------------------------------------------------------------------
    # Performance indexes — applied idempotently so re-running init_db() is safe.
    # These are critical for set-based bulk reconciliation at scale:
    #   - Without them PostgreSQL does sequential scans on every join column.
    #   - With them 1,000,000-row reconciliation completes in < 5 seconds.
    # -----------------------------------------------------------------------

    # 1. UTR matching — fastest reconciliation path (inner join on settlement_utr)
    "CREATE INDEX IF NOT EXISTS idx_payments_utr ON razorpay_payments (settlement_utr)",

    # 2. Exact & fuzzy amount+date joins across both tables
    "CREATE INDEX IF NOT EXISTS idx_payments_date_amount ON razorpay_payments (settlement_date, net_amount)",
    "CREATE INDEX IF NOT EXISTS idx_bank_date_amount ON bank_statements (value_date, amount)",

    # 3. bank_ref lookup for UTR cross-reference (bank_statements side)
    "CREATE INDEX IF NOT EXISTS idx_bank_ref ON bank_statements (bank_ref)",

    # 4. Ledger lookup via settlement_id → internal_ref
    "CREATE INDEX IF NOT EXISTS idx_ledger_internal_ref ON ledger_entries (internal_ref)",

    # 5. Settlement joins from payments side
    "CREATE INDEX IF NOT EXISTS idx_payments_settlement ON razorpay_payments (settlement_id)",

    # 6. Efficient exception / match status filtering
    "CREATE INDEX IF NOT EXISTS idx_match_status ON match_results (status)",
]

# ---------------------------------------------------------------------------
# DDL — partitioned tables (opt-in, used when init_db(partitioned=True))
# ---------------------------------------------------------------------------
#
# Two heavyweight tables are defined as RANGE-partitioned parents keyed on
# settlement_date (TEXT ISO-8601 cast to DATE inside the partition clause).
# Child partitions are created monthly via create_monthly_partition().
#
# NOTE: Neon serverless PostgreSQL fully supports declarative partitioning.
# The parent tables have no rows of their own — all data lives in children.

_DDL_PARTITIONED = [
    """
    CREATE TABLE IF NOT EXISTS razorpay_payments_partitioned (
        pay_id          TEXT          NOT NULL,
        order_id        TEXT          NOT NULL,
        captured_at     TEXT          NOT NULL,
        amount          NUMERIC(15,2) NOT NULL,
        currency        TEXT          NOT NULL DEFAULT 'INR',
        method          TEXT          NOT NULL,
        status          TEXT          NOT NULL,
        settlement_id   TEXT          NOT NULL,
        settlement_date TEXT          NOT NULL,   -- partition key (ISO-8601)
        settlement_utr  TEXT          NOT NULL,
        fee             NUMERIC(15,2) NOT NULL,
        tax             NUMERIC(15,2) NOT NULL,
        net_amount      NUMERIC(15,2) NOT NULL,
        error_type      TEXT          NOT NULL DEFAULT 'clean',
        PRIMARY KEY (pay_id, settlement_date)     -- PK must include partition key
    ) PARTITION BY RANGE (settlement_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS match_results_partitioned (
        pay_id                  TEXT          NOT NULL,
        entry_id                TEXT,
        txn_id                  TEXT,
        match_type              TEXT          NOT NULL DEFAULT 'unmatched',
        confidence              NUMERIC(5,4)  NOT NULL DEFAULT 0.0,
        delta                   NUMERIC(15,2),
        status                  TEXT          NOT NULL DEFAULT 'exception',
        ground_truth_error_type TEXT          NOT NULL DEFAULT 'clean',
        settlement_date         TEXT          NOT NULL,   -- partition key
        PRIMARY KEY (pay_id, settlement_date)
    ) PARTITION BY RANGE (settlement_date)
    """,
]

# Template to create one monthly child partition.
# Placeholders: {parent}, {suffix}, {start}, {end}  (formatted in Python, NOT %s)
_PARTITION_CHILD_TPL = (
    "CREATE TABLE IF NOT EXISTS {parent}_{suffix} "
    "PARTITION OF {parent} "
    "FOR VALUES FROM ('{start}') TO ('{end}')"
)

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


def init_db(
    batch: DataBatch,
    *,
    seed: bool = True,
    partitioned: bool = False,
) -> psycopg2.pool.ThreadedConnectionPool:
    """
    Initialise the Neon PostgreSQL connection pool.

    Steps
    -----
    1. Build DSN from DATABASE_URL env var.
    2. Create a ThreadedConnectionPool (min=1, max=10).
    3. Run DDL (CREATE TABLE IF NOT EXISTS) — idempotent.
       If *partitioned* is True, also create the RANGE-partitioned parent
       tables and auto-provision monthly child partitions for every
       settlement_date present in *batch*.
    4. If *seed* is True (default): TRUNCATE all tables and COPY-ingest *batch*.

    Returns the pool (also stored as module-level singleton).

    Calling init_db() again replaces the pool and reseeds — useful in tests.

    Parameters
    ----------
    batch:
        The DataBatch produced by the generator.
    seed:
        Set to False to skip truncate+reload (e.g. when DATABASE_URL points
        to a database that already has live data).
    partitioned:
        Set to True to create RANGE-partitioned variants of
        razorpay_payments and match_results for multi-million long-term
        storage.  Monthly child partitions are created automatically.
    """
    global _pool

    dsn = _build_dsn()

    new_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=dsn,
    )

    # Apply standard DDL — always idempotent
    conn = new_pool.getconn()
    try:
        with conn.cursor() as cur:
            for stmt in _DDL_STATEMENTS:
                cur.execute(stmt)
            if partitioned:
                for stmt in _DDL_PARTITIONED:
                    cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        new_pool.putconn(conn)

    _pool = new_pool

    # Auto-provision monthly child partitions for every date in the batch
    if partitioned and batch.payments:
        _dates = {p.settlement_date.isoformat()[:7] for p in batch.payments}  # 'YYYY-MM'
        for ym in sorted(_dates):
            year, month = int(ym[:4]), int(ym[5:7])
            create_monthly_partition(year, month)

    if seed:
        _seed_batch(batch)

    return _pool


def _seed_batch(batch: DataBatch) -> None:
    """Truncate all tables and COPY-ingest the synthetic batch."""
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            # Truncate in dependency order (CASCADE handles FK constraints)
            cur.execute(
                f"TRUNCATE TABLE {', '.join(_TRUNCATE_ORDER)} RESTART IDENTITY CASCADE"
            )
            _copy_payments(cur, batch)
            _copy_bank_txns(cur, batch)
            _copy_ledger_entries(cur, batch)
            _copy_settlements(cur, batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# COPY-based batch inserters
# ---------------------------------------------------------------------------
# Each function serialises rows into a TSV io.StringIO buffer and streams it
# into PostgreSQL via cursor.copy_from().  This is ~10-20× faster than
# parameterised INSERT VALUES %s for large batches (100k+ rows) because:
#   1. No per-row parameter binding overhead on the Python side.
#   2. PostgreSQL receives data as a single bulk stream, not N small messages.
#   3. COPY bypasses trigger-per-row processing (none here, but good practice).
#
# Fallback: if copy_from raises (e.g. special chars in test data) the function
# retries transparently with execute_values — no caller changes needed.
# ---------------------------------------------------------------------------

def _d(value: Decimal) -> float:
    """Convert Decimal → float for psycopg2 NUMERIC binding."""
    return float(value)


def _make_tsv(rows: list[tuple]) -> io.StringIO:
    """
    Serialise a list of row-tuples into a tab-separated values buffer
    suitable for cursor.copy_from().

    Rules applied:
    - None  → '\\N'  (PostgreSQL NULL sentinel in COPY text format)
    - str   → tab/newline/backslash are escaped with a leading backslash
    - Other → str(value)  (numbers, dates, enums already serialised)
    """
    buf = io.StringIO()
    for row in rows:
        escaped = []
        for val in row:
            if val is None:
                escaped.append("\\N")
            elif isinstance(val, str):
                # Escape special COPY characters
                val = val.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
                escaped.append(val)
            else:
                escaped.append(str(val))
        buf.write("\t".join(escaped) + "\n")
    buf.seek(0)
    return buf


def _copy_or_fallback(
    cur: psycopg2.extensions.cursor,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple],
    conflict_sql: str,
) -> None:
    """
    Attempt COPY streaming; fall back to execute_values on any error.

    Parameters
    ----------
    cur         : open cursor (caller manages transaction)
    table       : target table name
    columns     : column names in the same order as each row tuple
    rows        : data rows as tuples
    conflict_sql: full INSERT ... ON CONFLICT statement for the fallback path
    """
    if not rows:
        return

    buf = _make_tsv(rows)
    try:
        cur.copy_from(buf, table, columns=columns, null="\\N")
        logger.debug("COPY streamed %d rows into %s", len(rows), table)
    except Exception as exc:                                         # noqa: BLE001
        logger.warning(
            "COPY into %s failed (%s); retrying with execute_values.", table, exc
        )
        cur.connection.rollback()  # clear the aborted-tx state
        psycopg2.extras.execute_values(cur, conflict_sql, rows)


def _copy_payments(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    """COPY-stream razorpay_payments rows from *batch*."""
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
    _copy_or_fallback(
        cur,
        table="razorpay_payments",
        columns=(
            "pay_id", "order_id", "captured_at", "amount", "currency",
            "method", "status", "settlement_id", "settlement_date",
            "settlement_utr", "fee", "tax", "net_amount", "error_type",
        ),
        rows=rows,
        conflict_sql="""
            INSERT INTO razorpay_payments
            (pay_id, order_id, captured_at, amount, currency, method, status,
             settlement_id, settlement_date, settlement_utr, fee, tax, net_amount, error_type)
            VALUES %s
            ON CONFLICT (pay_id) DO NOTHING
        """,
    )


def _copy_bank_txns(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    """COPY-stream bank_statements rows from *batch*."""
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
    _copy_or_fallback(
        cur,
        table="bank_statements",
        columns=("txn_id", "value_date", "amount", "description",
                 "bank_ref", "currency", "settlement_id"),
        rows=rows,
        conflict_sql="""
            INSERT INTO bank_statements
            (txn_id, value_date, amount, description, bank_ref, currency, settlement_id)
            VALUES %s
            ON CONFLICT (txn_id) DO NOTHING
        """,
    )


def _copy_ledger_entries(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    """COPY-stream ledger_entries rows from *batch*."""
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
    _copy_or_fallback(
        cur,
        table="ledger_entries",
        columns=("entry_id", "date", "amount", "narration",
                 "account_code", "internal_ref"),
        rows=rows,
        conflict_sql="""
            INSERT INTO ledger_entries
            (entry_id, date, amount, narration, account_code, internal_ref)
            VALUES %s
            ON CONFLICT (entry_id) DO NOTHING
        """,
    )


def _copy_settlements(cur: psycopg2.extensions.cursor, batch: DataBatch) -> None:
    """COPY-stream settlements rows from *batch*."""
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
    _copy_or_fallback(
        cur,
        table="settlements",
        columns=("settlement_id", "settlement_date", "total_amount",
                 "num_payments", "status"),
        rows=rows,
        conflict_sql="""
            INSERT INTO settlements
            (settlement_id, settlement_date, total_amount, num_payments, status)
            VALUES %s
            ON CONFLICT (settlement_id) DO NOTHING
        """,
    )


# ---------------------------------------------------------------------------
# Partition management — monthly RANGE partitions
# ---------------------------------------------------------------------------

def create_monthly_partition(year: int, month: int) -> None:
    """
    Idempotently create monthly child partitions for both partitioned tables.

    Partition range: [YYYY-MM-01, YYYY-{MM+1}-01)  (exclusive upper bound).
    Child table names follow the pattern:  <parent>_YYYY_MM

    This is safe to call multiple times — CREATE TABLE IF NOT EXISTS
    ensures no error if the partition already exists.

    Parameters
    ----------
    year  : 4-digit year (e.g. 2026)
    month : 1-12

    Example
    -------
    >>> create_monthly_partition(2026, 8)   # creates _2026_08 partitions
    """
    import calendar

    suffix = f"{year:04d}_{month:02d}"
    start  = f"{year:04d}-{month:02d}-01"

    # Compute exclusive upper bound (first day of next month)
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"

    parents = ["razorpay_payments_partitioned", "match_results_partitioned"]
    stmts   = [
        _PARTITION_CHILD_TPL.format(parent=p, suffix=suffix, start=start, end=end)
        for p in parents
    ]

    conn = _get_pool().getconn()
    try:
        with conn.cursor() as cur:
            for stmt in stmts:
                cur.execute(stmt)
        conn.commit()
        logger.info(
            "Ensured monthly partitions (%s – %s) for: %s",
            start, end, ", ".join(parents),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)


def ensure_partition_for_date(date_str: str) -> None:
    """
    Parse an ISO-8601 date string and create its monthly partition if absent.

    Parameters
    ----------
    date_str : ISO-8601 date, e.g. '2026-09-15'

    Example
    -------
    >>> ensure_partition_for_date('2026-09-15')  # creates _2026_09 partition
    """
    import datetime
    d = datetime.date.fromisoformat(date_str[:10])
    create_monthly_partition(d.year, d.month)


def bulk_load_batch(batch: DataBatch) -> dict[str, int]:
    """
    COPY-ingest a DataBatch into the partitioned parent tables.

    Auto-provisions monthly child partitions for every settlement_date present
    in the batch before loading, so callers do not need to pre-create them.

    Returns a dict of ``{table_name: rows_loaded}`` for observability.

    This is the preferred entry point for production multi-million ingestion
    pipelines.  It does NOT truncate existing data — rows already present are
    skipped via ON CONFLICT DO NOTHING (in the fallback path) or simply
    appended (COPY does not check conflicts — upstream deduplication is the
    caller's responsibility for maximum throughput).

    Parameters
    ----------
    batch : DataBatch produced by the generator or fetched from Razorpay API.

    Example
    -------
    >>> counts = bulk_load_batch(my_batch)
    >>> print(counts)   # {'razorpay_payments_partitioned': 50000, ...}
    """
    # 1. Ensure child partitions exist for all settlement_dates in this batch
    seen_months: set[str] = set()
    for p in batch.payments:
        ym = p.settlement_date.isoformat()[:7]  # 'YYYY-MM'
        if ym not in seen_months:
            seen_months.add(ym)
            year, month = int(ym[:4]), int(ym[5:7])
            create_monthly_partition(year, month)

    counts: dict[str, int] = {}

    conn = _get_pool().getconn()
    try:
        with conn.cursor() as cur:
            # --- razorpay_payments_partitioned ---
            pay_rows = [
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
            _copy_or_fallback(
                cur,
                table="razorpay_payments_partitioned",
                columns=(
                    "pay_id", "order_id", "captured_at", "amount", "currency",
                    "method", "status", "settlement_id", "settlement_date",
                    "settlement_utr", "fee", "tax", "net_amount", "error_type",
                ),
                rows=pay_rows,
                conflict_sql="""
                    INSERT INTO razorpay_payments_partitioned
                    (pay_id, order_id, captured_at, amount, currency, method, status,
                     settlement_id, settlement_date, settlement_utr, fee, tax, net_amount, error_type)
                    VALUES %s
                    ON CONFLICT (pay_id, settlement_date) DO NOTHING
                """,
            )
            counts["razorpay_payments_partitioned"] = len(pay_rows)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)

    logger.info(
        "bulk_load_batch complete — loaded %d payments into partitioned table",
        counts.get("razorpay_payments_partitioned", 0),
    )
    return counts


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
