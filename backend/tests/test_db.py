"""
tests/test_db.py
================
Integration tests for backend/data/db.py (Neon PostgreSQL layer).

These tests require a live PostgreSQL database (Neon or local).
They are automatically SKIPPED if DATABASE_URL is not set in the environment,
so the CI pipeline stays green without a database connection.

Set DATABASE_URL before running:
    $env:DATABASE_URL = "postgresql://user:pass@host/dbname?sslmode=require"
    .venv\\Scripts\\pytest tests/test_db.py -v

Covers
------
- init_db() : pool created, schema exists, rows loaded
- Row counts : payments, bank_txns, ledger_entries, settlements
- Column presence : every schema column exists in every table
- Data integrity : UTR cross-ref, no duplicates, positive net_amount
- query()   : returns list[dict], correct count, parameterised filter
- execute() : INSERT and UPDATE DML helpers
- table_counts() : correct values, match_results/exceptions start at 0
- close_pool() : pool is closed and set to None
- get_connection() before init raises RuntimeError
- Second init_db() call replaces all data (no double-counts)
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.pool
import pytest

import data.db as db_module
from data.db import (
    close_pool,
    execute,
    get_connection,
    init_db,
    query,
    table_counts,
)
from data.generator import BatchGenerator, BATCH_SIZE
from data.schema import ErrorType

# ---------------------------------------------------------------------------
# Skip entire module if DATABASE_URL is not available
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping Neon integration tests",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_pool():
    """
    Ensure the module-level pool is closed and reset before and after each test
    so tests are fully isolated.
    """
    close_pool()
    db_module._pool = None
    yield
    close_pool()
    db_module._pool = None


@pytest.fixture
def batch():
    return BatchGenerator(seed=42).generate()


@pytest.fixture
def loaded_db(batch):
    """Initialise DB with the standard 60-row batch; return the pool."""
    return init_db(batch)


# ---------------------------------------------------------------------------
# get_connection() before init
# ---------------------------------------------------------------------------

def test_get_connection_before_init_raises():
    with pytest.raises(RuntimeError, match="not initialised"):
        with get_connection() as _:
            pass


# ---------------------------------------------------------------------------
# init_db()
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_returns_pool(self, batch):
        pool = init_db(batch)
        assert isinstance(pool, psycopg2.pool.ThreadedConnectionPool)

    def test_sets_module_pool(self, batch):
        pool = init_db(batch)
        assert db_module._pool is pool

    def test_all_tables_exist(self, batch):
        init_db(batch)
        tables = {
            row["table_name"]
            for row in query(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type   = 'BASE TABLE'
                """
            )
        }
        required = {
            "razorpay_payments",
            "bank_statements",
            "ledger_entries",
            "settlements",
            "match_results",
            "exceptions",
        }
        assert required.issubset(tables), f"Missing tables: {required - tables}"

    def test_payment_row_count(self, batch):
        init_db(batch)
        rows = query("SELECT COUNT(*) AS cnt FROM razorpay_payments")
        assert rows[0]["cnt"] == BATCH_SIZE

    def test_bank_txn_row_count(self, batch):
        init_db(batch)
        no_credit = sum(1 for p in batch.payments if p.error_type == ErrorType.no_bank_credit)
        expected = BATCH_SIZE - no_credit
        rows = query("SELECT COUNT(*) AS cnt FROM bank_statements")
        assert rows[0]["cnt"] == expected

    def test_ledger_entry_row_count(self, batch):
        init_db(batch)
        rows = query("SELECT COUNT(*) AS cnt FROM ledger_entries")
        assert rows[0]["cnt"] == len(batch.ledger_entries)

    def test_settlement_row_count(self, batch):
        init_db(batch)
        rows = query("SELECT COUNT(*) AS cnt FROM settlements")
        assert rows[0]["cnt"] == len(batch.settlements)

    def test_second_init_replaces_data(self, batch):
        """Calling init_db() twice must not double-count rows."""
        init_db(batch)
        init_db(batch)
        rows = query("SELECT COUNT(*) AS cnt FROM razorpay_payments")
        assert rows[0]["cnt"] == BATCH_SIZE  # not BATCH_SIZE * 2


# ---------------------------------------------------------------------------
# Column presence
# ---------------------------------------------------------------------------

class TestColumnPresence:
    def _columns(self, table: str) -> set[str]:
        rows = query(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = %s
            """,
            (table,),
        )
        return {r["column_name"] for r in rows}

    def test_razorpay_payments_columns(self, loaded_db):
        cols = self._columns("razorpay_payments")
        required = {
            "pay_id", "order_id", "captured_at", "amount", "currency",
            "method", "status", "settlement_id", "settlement_date",
            "settlement_utr", "fee", "tax", "net_amount", "error_type",
        }
        assert required.issubset(cols)

    def test_bank_statements_columns(self, loaded_db):
        cols = self._columns("bank_statements")
        required = {"txn_id", "value_date", "amount", "description", "bank_ref", "currency", "settlement_id"}
        assert required.issubset(cols)

    def test_ledger_entries_columns(self, loaded_db):
        cols = self._columns("ledger_entries")
        required = {"entry_id", "date", "amount", "narration", "account_code", "internal_ref"}
        assert required.issubset(cols)

    def test_settlements_columns(self, loaded_db):
        cols = self._columns("settlements")
        required = {"settlement_id", "settlement_date", "total_amount", "num_payments", "status"}
        assert required.issubset(cols)

    def test_match_results_columns(self, loaded_db):
        cols = self._columns("match_results")
        required = {"pay_id", "entry_id", "txn_id", "match_type", "confidence", "delta", "status"}
        assert required.issubset(cols)

    def test_exceptions_columns(self, loaded_db):
        cols = self._columns("exceptions")
        required = {"exception_id", "source", "record_id", "reason", "agent_reasoning", "suggested_action"}
        assert required.issubset(cols)


# ---------------------------------------------------------------------------
# Data integrity
# ---------------------------------------------------------------------------

class TestDataIntegrity:
    def test_all_pay_ids_start_with_pay(self, loaded_db):
        rows = query("SELECT pay_id FROM razorpay_payments")
        for row in rows:
            assert row["pay_id"].startswith("pay_")

    def test_all_bank_refs_are_utrs(self, loaded_db):
        rows = query("SELECT bank_ref FROM bank_statements")
        for row in rows:
            assert len(row["bank_ref"]) >= 10

    def test_net_amount_positive(self, loaded_db):
        rows = query("SELECT net_amount FROM razorpay_payments")
        for row in rows:
            assert float(row["net_amount"]) > 0

    def test_settlement_ids_in_payments_exist_in_settlements(self, loaded_db):
        """Every settlement_id referenced in payments must have a settlements row."""
        pay_setl_ids = {
            row["settlement_id"]
            for row in query("SELECT settlement_id FROM razorpay_payments")
        }
        setl_ids = {
            row["settlement_id"]
            for row in query("SELECT settlement_id FROM settlements")
        }
        assert pay_setl_ids.issubset(setl_ids), (
            f"Orphaned settlement IDs: {pay_setl_ids - setl_ids}"
        )

    def test_bank_utrs_match_payment_utrs(self, loaded_db):
        """All bank_ref values must match a settlement_utr in razorpay_payments."""
        bank_refs = {row["bank_ref"] for row in query("SELECT bank_ref FROM bank_statements")}
        payment_utrs = {
            row["settlement_utr"] for row in query("SELECT settlement_utr FROM razorpay_payments")
        }
        assert bank_refs.issubset(payment_utrs), (
            f"Bank refs without matching UTR: {bank_refs - payment_utrs}"
        )

    def test_no_duplicate_pay_ids(self, loaded_db):
        rows = query(
            "SELECT pay_id, COUNT(*) AS cnt FROM razorpay_payments GROUP BY pay_id HAVING COUNT(*) > 1"
        )
        assert rows == [], f"Duplicate pay_ids: {rows}"

    def test_no_duplicate_txn_ids(self, loaded_db):
        rows = query(
            "SELECT txn_id, COUNT(*) AS cnt FROM bank_statements GROUP BY txn_id HAVING COUNT(*) > 1"
        )
        assert rows == [], f"Duplicate txn_ids: {rows}"

    def test_ledger_internal_refs_are_setl_ids(self, loaded_db):
        rows = query("SELECT internal_ref FROM ledger_entries")
        for row in rows:
            assert row["internal_ref"].startswith("setl_"), (
                f"internal_ref should start with setl_: {row['internal_ref']}"
            )


# ---------------------------------------------------------------------------
# db.query() helper
# ---------------------------------------------------------------------------

class TestQueryHelper:
    def test_returns_list_of_dicts(self, loaded_db):
        rows = query("SELECT * FROM razorpay_payments LIMIT 5")
        assert isinstance(rows, list)
        assert all(isinstance(r, dict) for r in rows)

    def test_correct_row_count(self, loaded_db):
        rows = query("SELECT * FROM razorpay_payments")
        assert len(rows) == BATCH_SIZE

    def test_filter_by_currency(self, loaded_db):
        rows = query("SELECT * FROM razorpay_payments WHERE currency = %s", ("INR",))
        assert len(rows) == BATCH_SIZE

    def test_filter_no_results(self, loaded_db):
        rows = query("SELECT * FROM razorpay_payments WHERE currency = %s", ("USD",))
        assert rows == []


# ---------------------------------------------------------------------------
# db.execute() helper
# ---------------------------------------------------------------------------

class TestExecuteHelper:
    def test_insert_match_result(self, loaded_db):
        rowcount = execute(
            """
            INSERT INTO match_results
            (pay_id, match_type, confidence, status, ground_truth_error_type)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("pay_testonly00000001", "exact", 1.0, "matched", "clean"),
        )
        assert rowcount == 1

    def test_update_match_result(self, loaded_db):
        execute(
            """
            INSERT INTO match_results
            (pay_id, match_type, confidence, status, ground_truth_error_type)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ("pay_testonly00000002", "unmatched", 0.0, "exception", "clean"),
        )
        rowcount = execute(
            "UPDATE match_results SET status = %s WHERE pay_id = %s",
            ("matched", "pay_testonly00000002"),
        )
        assert rowcount == 1
        rows = query(
            "SELECT status FROM match_results WHERE pay_id = %s",
            ("pay_testonly00000002",),
        )
        assert rows[0]["status"] == "matched"


# ---------------------------------------------------------------------------
# table_counts()
# ---------------------------------------------------------------------------

class TestTableCounts:
    def test_returns_dict(self, loaded_db):
        counts = table_counts()
        assert isinstance(counts, dict)

    def test_contains_all_tables(self, loaded_db):
        counts = table_counts()
        assert "razorpay_payments" in counts
        assert "bank_statements" in counts
        assert "ledger_entries" in counts
        assert "settlements" in counts
        assert "match_results" in counts
        assert "exceptions" in counts

    def test_payment_count_matches(self, loaded_db):
        counts = table_counts()
        assert counts["razorpay_payments"] == BATCH_SIZE

    def test_match_results_empty_initially(self, loaded_db):
        counts = table_counts()
        assert counts["match_results"] == 0

    def test_exceptions_empty_initially(self, loaded_db):
        counts = table_counts()
        assert counts["exceptions"] == 0


# ---------------------------------------------------------------------------
# close_pool()
# ---------------------------------------------------------------------------

class TestClosePool:
    def test_close_sets_pool_to_none(self, batch):
        init_db(batch)
        assert db_module._pool is not None
        close_pool()
        assert db_module._pool is None

    def test_close_idempotent(self, batch):
        """Calling close_pool() twice must not raise."""
        init_db(batch)
        close_pool()
        close_pool()  # second call is safe
