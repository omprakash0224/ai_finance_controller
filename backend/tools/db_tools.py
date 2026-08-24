"""
backend/tools/db_tools.py
=========================
ADK-compatible tool functions for database access.

These functions are registered as FunctionTools on ADK agents and are called
during the ReAct loop to query or mutate the Neon PostgreSQL database.

All functions accept / return plain Python types (str, list, dict) so they
are directly serialisable by the ADK tool-calling layer.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from data import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    """Recursively convert Decimal / date types so they JSON-serialise."""
    if isinstance(obj, Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):          # date / datetime
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(i) for i in obj]
    return obj


def _rows_to_json(rows: list[dict]) -> str:
    """Convert list[dict] → compact JSON string for agent consumption."""
    return json.dumps(_json_safe(rows), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Tool: sql_query
# ---------------------------------------------------------------------------

def sql_query(sql: str) -> str:
    """
    Execute a read-only SQL SELECT query against the Neon PostgreSQL database
    and return the results as a JSON string.

    Args:
        sql: A valid PostgreSQL SELECT statement.  Only SELECT is permitted;
             any DML will raise an error.

    Returns:
        JSON string representing a list of row objects, e.g.
        '[{"pay_id":"pay_abc","amount":1000.00},...]'
        Returns '[]' if no rows matched.
        Returns a JSON error object if the query fails.
    """
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("WITH"):
        return json.dumps({"error": "Only SELECT / WITH queries are permitted."})
    try:
        rows = _db.query(sql)
        return _rows_to_json(rows)
    except Exception as exc:                                     # noqa: BLE001
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: get_unmatched_payments
# ---------------------------------------------------------------------------

def get_unmatched_payments() -> str:
    """
    Return all Razorpay payments that have not yet been assigned a match result.

    A payment is considered unmatched when its pay_id does NOT appear in the
    match_results table (or appears with status = 'exception').

    Returns:
        JSON string list of payment rows (pay_id, settlement_id, settlement_utr,
        net_amount, settlement_date, error_type).
    """
    sql = """
        SELECT
            p.pay_id,
            p.settlement_id,
            p.settlement_utr,
            p.net_amount,
            p.settlement_date,
            p.captured_at,
            p.error_type
        FROM razorpay_payments p
        WHERE p.pay_id NOT IN (
            SELECT pay_id FROM match_results WHERE status = 'matched'
        )
        ORDER BY p.captured_at
    """
    try:
        rows = _db.query(sql)
        return _rows_to_json(rows)
    except Exception as exc:                                     # noqa: BLE001
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: get_unmatched_entries
# ---------------------------------------------------------------------------

def get_unmatched_entries() -> str:
    """
    Return all ledger entries that have not yet been linked to a matched payment.

    A ledger entry is unmatched when its internal_ref (settlement_id) does NOT
    appear in the match_results table with status = 'matched'.

    Returns:
        JSON string list of ledger entry rows (entry_id, date, amount,
        narration, account_code, internal_ref).
    """
    sql = """
        SELECT
            e.entry_id,
            e.date,
            e.amount,
            e.narration,
            e.account_code,
            e.internal_ref
        FROM ledger_entries e
        WHERE e.internal_ref NOT IN (
            SELECT COALESCE(entry_id, '')
            FROM match_results
            WHERE status = 'matched'
        )
        ORDER BY e.date
    """
    try:
        rows = _db.query(sql)
        return _rows_to_json(rows)
    except Exception as exc:                                     # noqa: BLE001
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: get_all_payments
# ---------------------------------------------------------------------------

def get_all_payments() -> str:
    """
    Return all Razorpay payments with their settlement and bank statement data.

    Returns a JSON string with all payment records joined with available bank
    statement rows (LEFT JOIN so payments without bank credit are included).
    """
    sql = """
        SELECT
            p.pay_id,
            p.order_id,
            p.captured_at,
            p.amount,
            p.net_amount,
            p.settlement_id,
            p.settlement_date,
            p.settlement_utr,
            p.method,
            p.status,
            p.fee,
            p.tax,
            p.error_type,
            b.txn_id      AS bank_txn_id,
            b.value_date  AS bank_value_date,
            b.amount      AS bank_amount,
            b.bank_ref    AS bank_utr
        FROM razorpay_payments p
        LEFT JOIN bank_statements b ON b.bank_ref = p.settlement_utr
        ORDER BY p.captured_at
    """
    try:
        rows = _db.query(sql)
        return _rows_to_json(rows)
    except Exception as exc:                                     # noqa: BLE001
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: save_match_result
# ---------------------------------------------------------------------------

def save_match_result(
    pay_id: str,
    entry_id: str,
    txn_id: str,
    match_type: str,
    confidence: float,
    delta: float,
    status: str,
    ground_truth_error_type: str,
) -> str:
    """
    Persist a single match result to the match_results table.

    Args:
        pay_id: Razorpay payment ID being reconciled.
        entry_id: Matched ledger entry_id (empty string if unmatched).
        txn_id: Matched bank txn_id (empty string if unmatched).
        match_type: One of: exact, fuzzy_amount, fuzzy_date, utr_match,
                    multi_split, unmatched.
        confidence: Float 0.0–1.0 representing match confidence.
        delta: Amount or date delta that triggered the match (0.0 if exact).
        status: One of: matched, exception, escalated.
        ground_truth_error_type: Generator label for accuracy scoring.

    Returns:
        JSON string {'saved': true} or {'error': '...'}.
    """
    sql = """
        INSERT INTO match_results
            (pay_id, entry_id, txn_id, match_type, confidence, delta, status, ground_truth_error_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (pay_id) DO UPDATE SET
            entry_id                = EXCLUDED.entry_id,
            txn_id                  = EXCLUDED.txn_id,
            match_type              = EXCLUDED.match_type,
            confidence              = EXCLUDED.confidence,
            delta                   = EXCLUDED.delta,
            status                  = EXCLUDED.status,
            ground_truth_error_type = EXCLUDED.ground_truth_error_type
    """
    try:
        _db.execute(
            sql,
            (
                pay_id,
                entry_id or None,
                txn_id or None,
                match_type,
                confidence,
                delta if delta != 0.0 else None,
                status,
                ground_truth_error_type,
            ),
        )
        return json.dumps({"saved": True})
    except Exception as exc:                                     # noqa: BLE001
        return json.dumps({"error": str(exc)})
