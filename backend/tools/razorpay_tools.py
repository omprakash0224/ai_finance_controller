"""
backend/tools/razorpay_tools.py
================================
Razorpay-specific tool functions exposed to ADK agents.

These tools simulate the Razorpay Settlements / Payments API by querying
the synthetic data already loaded into Neon PostgreSQL, providing:
  - Settlement cycle information (T+0 / T+1 / T+2)
  - Full payment record resolution
  - Pending settlement listing

All functions return compact JSON strings so the ADK ReAct loop can reason
about the responses without further parsing.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from data import db as _db


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _jdump(obj: Any) -> str:
    """Serialise a dict / list to compact JSON, converting Decimal/date."""
    def _enc(o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return o

    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(i) for i in o]
        return _enc(o)

    return json.dumps(_walk(obj), separators=(",", ":"))


def _tier_from_delta(captured_at: str, settlement_date: str) -> str:
    """Infer T+0 / T+1 / T+2 tier from date strings."""
    from datetime import date
    try:
        cap = date.fromisoformat(str(captured_at))
        setl = date.fromisoformat(str(settlement_date))
        delta = (setl - cap).days
        if delta == 0:
            return "T+0"
        elif delta <= 1:
            return "T+1"
        elif delta <= 2:
            return "T+2"
        else:
            return f"T+{delta}"
    except Exception:                                            # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Tool: get_settlement_cycle
# ---------------------------------------------------------------------------

def get_settlement_cycle(pay_id: str) -> str:
    """
    Return settlement cycle information for a given Razorpay payment.

    Identifies whether the payment follows a T+0, T+1, or T+2 working-day
    settlement cycle based on the difference between captured_at and
    settlement_date.

    Args:
        pay_id: The Razorpay payment ID (prefix pay_).

    Returns:
        JSON string with:
          - pay_id, settlement_id, settlement_utr
          - captured_at, settlement_date
          - tier (T+0 / T+1 / T+2)
          - net_amount, status
        Or {'error': '...'} if the pay_id is not found.
    """
    try:
        rows = _db.query(
            """
            SELECT pay_id, settlement_id, settlement_utr,
                   captured_at, settlement_date, net_amount, status, error_type
            FROM razorpay_payments
            WHERE pay_id = %s
            """,
            (pay_id,),
        )
        if not rows:
            return _jdump({"error": f"Payment {pay_id} not found"})
        row = rows[0]
        tier = _tier_from_delta(row["captured_at"], row["settlement_date"])
        return _jdump({**dict(row), "tier": tier})
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: resolve_payment_id
# ---------------------------------------------------------------------------

def resolve_payment_id(pay_id: str) -> str:
    """
    Return the full payment record for a given Razorpay payment ID, including
    the corresponding bank statement and ledger entry rows if they exist.

    Args:
        pay_id: The Razorpay payment ID (prefix pay_).

    Returns:
        JSON string with:
          - payment: full razorpay_payments row
          - bank_txn: matching bank_statements row (null if not found)
          - ledger_entries: list of matching ledger_entries rows
        Or {'error': '...'} if the pay_id is not found.
    """
    try:
        pay_rows = _db.query(
            "SELECT * FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        if not pay_rows:
            return _jdump({"error": f"Payment {pay_id} not found"})
        pay = pay_rows[0]

        # Bank statement (via UTR)
        bank_rows = _db.query(
            "SELECT * FROM bank_statements WHERE bank_ref = %s LIMIT 1",
            (pay["settlement_utr"],),
        )

        # Ledger entries (via settlement_id)
        led_rows = _db.query(
            "SELECT * FROM ledger_entries WHERE internal_ref = %s ORDER BY entry_id",
            (pay["settlement_id"],),
        )

        return _jdump({
            "payment": pay,
            "bank_txn": bank_rows[0] if bank_rows else None,
            "ledger_entries": led_rows,
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: list_pending_settlements
# ---------------------------------------------------------------------------

def list_pending_settlements() -> str:
    """
    List all Razorpay settlement records with status = 'pending'.

    Pending settlements represent amounts that have not yet landed in the
    merchant's bank account and are relevant for cash flow forecasting.

    Returns:
        JSON string — list of settlement rows with:
          - settlement_id, settlement_date, total_amount, num_payments, status
          - tier: T+0 / T+1 / T+2 inferred from earliest payment capture date
        Returns '[]' if no pending settlements.
    """
    try:
        rows = _db.query(
            """
            SELECT
                s.settlement_id,
                s.settlement_date,
                s.total_amount,
                s.num_payments,
                s.status,
                MIN(p.captured_at) AS earliest_capture
            FROM settlements s
            LEFT JOIN razorpay_payments p ON p.settlement_id = s.settlement_id
            WHERE s.status = 'pending'
            GROUP BY s.settlement_id, s.settlement_date, s.total_amount,
                     s.num_payments, s.status
            ORDER BY s.settlement_date
            """
        )
        # Annotate tier
        for row in rows:
            row["tier"] = _tier_from_delta(
                row.get("earliest_capture", row["settlement_date"]),
                row["settlement_date"],
            )
        return _jdump(rows)
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: get_settlement_summary
# ---------------------------------------------------------------------------

def get_settlement_summary() -> str:
    """
    Return an aggregate summary of all settlements grouped by status and tier.

    Useful for the forecaster agent to understand how much cash is in-flight
    across T+0, T+1, and T+2 cycles.

    Returns:
        JSON string with:
          - total_settlements, total_amount_inr
          - by_status: {pending: {count, amount}, processed: {...}}
          - pending_by_date: list of {date, amount} for pending settlements
    """
    try:
        by_status = _db.query(
            """
            SELECT status,
                   COUNT(*)         AS settlement_count,
                   SUM(total_amount) AS total_amount
            FROM settlements
            GROUP BY status
            """
        )
        pending_by_date = _db.query(
            """
            SELECT settlement_date AS date,
                   SUM(total_amount) AS amount,
                   COUNT(*)          AS num_settlements
            FROM settlements
            WHERE status = 'pending'
            GROUP BY settlement_date
            ORDER BY settlement_date
            """
        )
        totals = _db.query(
            "SELECT COUNT(*) AS n, SUM(total_amount) AS total FROM settlements"
        )
        return _jdump({
            "total_settlements": totals[0]["n"] if totals else 0,
            "total_amount_inr": totals[0]["total"] if totals else 0,
            "by_status": {r["status"]: {"count": r["settlement_count"],
                                         "amount": r["total_amount"]}
                          for r in by_status},
            "pending_by_date": pending_by_date,
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})
