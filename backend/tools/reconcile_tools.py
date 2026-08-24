"""
backend/tools/reconcile_tools.py
=================================
Core reconciliation tool functions registered with the Reconciler ADK agent.

Each function is a plain Python callable that the ADK FunctionTool wrapper
exposes to the LLM.  They perform the actual DB lookups / writes and return
compact JSON strings so the model can reason about the results.

Match priority (per PLAN.md):
  1. utr_match       — fastest, most reliable
  2. exact_match     — amount + date exact
  3. fuzzy_match     — amount delta ≤ 5 INR or date delta ≤ 2 days
  4. split_match     — 1 settlement → N ledger entries
  5. flag_exception  — no match after 3 attempts
"""

from __future__ import annotations

import json
import uuid
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


def _to_decimal(val: Any) -> Decimal:
    return Decimal(str(val)) if val is not None else Decimal("0")


# ---------------------------------------------------------------------------
# Tool: utr_match
# ---------------------------------------------------------------------------

def utr_match(pay_id: str) -> str:
    """
    Attempt to match a Razorpay payment to a bank statement entry via UTR.

    This is the fastest and most reliable reconciliation path.  The
    settlement_utr from razorpay_payments is looked up in the bank_ref
    column of bank_statements.

    Args:
        pay_id: The Razorpay payment ID (prefix pay_) to reconcile.

    Returns:
        JSON string with keys:
          - matched (bool)
          - pay_id, txn_id, entry_id, match_type, confidence, delta, status
        Or {'error': '...'} if an exception occurred.
    """
    try:
        # Fetch the payment
        pay_rows = _db.query(
            "SELECT * FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        if not pay_rows:
            return _jdump({"matched": False, "reason": f"pay_id {pay_id} not found"})
        pay = pay_rows[0]
        utr = pay["settlement_utr"]

        # Look for bank statement row with matching UTR
        bank_rows = _db.query(
            "SELECT * FROM bank_statements WHERE bank_ref = %s", (utr,)
        )
        if not bank_rows:
            return _jdump({"matched": False, "pay_id": pay_id, "utr": utr,
                           "reason": "UTR not found in bank_statements"})
        bank = bank_rows[0]

        # Find corresponding ledger entry (via settlement_id)
        led_rows = _db.query(
            "SELECT * FROM ledger_entries WHERE internal_ref = %s LIMIT 1",
            (pay["settlement_id"],),
        )
        entry_id = led_rows[0]["entry_id"] if led_rows else None

        # Compute amount delta for reference
        pay_net = _to_decimal(pay["net_amount"])
        bank_amt = _to_decimal(bank["amount"])
        delta = abs(pay_net - bank_amt)

        return _jdump({
            "matched": True,
            "pay_id": pay_id,
            "txn_id": bank["txn_id"],
            "entry_id": entry_id,
            "match_type": "utr_match",
            "confidence": 0.98,
            "delta": float(delta),
            "status": "matched",
            "utr": utr,
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: exact_match
# ---------------------------------------------------------------------------

def exact_match(pay_id: str) -> str:
    """
    Attempt an exact reconciliation match on net_amount and settlement_date.

    Looks for a bank_statement row where amount equals net_amount AND
    value_date equals settlement_date for the given payment.

    Args:
        pay_id: The Razorpay payment ID to reconcile.

    Returns:
        JSON string with matched status and match details, or not-matched
        indicator with reason.
    """
    try:
        pay_rows = _db.query(
            "SELECT * FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        if not pay_rows:
            return _jdump({"matched": False, "reason": f"pay_id {pay_id} not found"})
        pay = pay_rows[0]

        bank_rows = _db.query(
            """
            SELECT * FROM bank_statements
            WHERE amount = %s
              AND value_date = %s
              AND settlement_id = %s
            LIMIT 1
            """,
            (float(pay["net_amount"]), pay["settlement_date"], pay["settlement_id"]),
        )

        if not bank_rows:
            return _jdump({"matched": False, "pay_id": pay_id,
                           "reason": "No exact match on amount+date+settlement_id"})
        bank = bank_rows[0]

        led_rows = _db.query(
            "SELECT * FROM ledger_entries WHERE internal_ref = %s LIMIT 1",
            (pay["settlement_id"],),
        )
        entry_id = led_rows[0]["entry_id"] if led_rows else None

        return _jdump({
            "matched": True,
            "pay_id": pay_id,
            "txn_id": bank["txn_id"],
            "entry_id": entry_id,
            "match_type": "exact",
            "confidence": 1.0,
            "delta": 0.0,
            "status": "matched",
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: fuzzy_match
# ---------------------------------------------------------------------------

def fuzzy_match(pay_id: str, amount_threshold_inr: float = 5.0,
                date_threshold_days: int = 2) -> str:
    """
    Attempt a fuzzy reconciliation match using relaxed amount and date criteria.

    First tries to match on amount within ±amount_threshold_inr INR.
    Then tries on date within ±date_threshold_days calendar days.

    Args:
        pay_id: The Razorpay payment ID to reconcile.
        amount_threshold_inr: Maximum acceptable INR difference (default 5.0).
        date_threshold_days: Maximum acceptable day difference (default 2).

    Returns:
        JSON string with match details including the specific delta that
        triggered the match, or not-matched indicator with explanation.
    """
    try:
        pay_rows = _db.query(
            "SELECT * FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        if not pay_rows:
            return _jdump({"matched": False, "reason": f"pay_id {pay_id} not found"})
        pay = pay_rows[0]
        pay_net = _to_decimal(pay["net_amount"])
        settle_id = pay["settlement_id"]

        # --- Amount fuzzy match ---
        bank_rows = _db.query(
            """
            SELECT *, ABS(amount - %s) AS amt_delta
            FROM bank_statements
            WHERE settlement_id = %s
              AND ABS(amount - %s) <= %s
            ORDER BY amt_delta
            LIMIT 1
            """,
            (float(pay_net), settle_id, float(pay_net), amount_threshold_inr),
        )
        if bank_rows:
            bank = bank_rows[0]
            delta = float(abs(_to_decimal(bank["amount"]) - pay_net))
            led_rows = _db.query(
                "SELECT * FROM ledger_entries WHERE internal_ref = %s LIMIT 1",
                (settle_id,),
            )
            entry_id = led_rows[0]["entry_id"] if led_rows else None
            return _jdump({
                "matched": True,
                "pay_id": pay_id,
                "txn_id": bank["txn_id"],
                "entry_id": entry_id,
                "match_type": "fuzzy_amount",
                "confidence": round(max(0.7, 1.0 - delta / 10.0), 4),
                "delta": delta,
                "status": "matched",
            })

        # --- Date fuzzy match ---
        bank_rows = _db.query(
            """
            SELECT *,
                   ABS(EXTRACT(epoch FROM (value_date::date - %s::date)) / 86400) AS day_delta
            FROM bank_statements
            WHERE settlement_id = %s
              AND ABS(EXTRACT(epoch FROM (value_date::date - %s::date)) / 86400) <= %s
            ORDER BY day_delta
            LIMIT 1
            """,
            (
                pay["settlement_date"],
                settle_id,
                pay["settlement_date"],
                date_threshold_days,
            ),
        )
        if bank_rows:
            bank = bank_rows[0]
            day_delta = float(bank.get("day_delta", 0))
            led_rows = _db.query(
                "SELECT * FROM ledger_entries WHERE internal_ref = %s LIMIT 1",
                (settle_id,),
            )
            entry_id = led_rows[0]["entry_id"] if led_rows else None
            return _jdump({
                "matched": True,
                "pay_id": pay_id,
                "txn_id": bank["txn_id"],
                "entry_id": entry_id,
                "match_type": "fuzzy_date",
                "confidence": round(max(0.65, 1.0 - day_delta / 5.0), 4),
                "delta": day_delta,
                "status": "matched",
            })

        return _jdump({
            "matched": False,
            "pay_id": pay_id,
            "reason": (
                f"No fuzzy match found within {amount_threshold_inr} INR "
                f"or {date_threshold_days} days for settlement_id={settle_id}"
            ),
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: split_match
# ---------------------------------------------------------------------------

def split_match(pay_id: str) -> str:
    """
    Detect and match a split payment — one settlement_id mapped to N ledger entries.

    Checks if the ledger entries for this payment's settlement_id sum to the
    net_amount of the payment (indicating a 1:N split).

    Args:
        pay_id: The Razorpay payment ID to reconcile.

    Returns:
        JSON string with match details including all entry_ids involved in
        the split, or not-matched indicator.
    """
    try:
        pay_rows = _db.query(
            "SELECT * FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        if not pay_rows:
            return _jdump({"matched": False, "reason": f"pay_id {pay_id} not found"})
        pay = pay_rows[0]
        settle_id = pay["settlement_id"]
        pay_net = _to_decimal(pay["net_amount"])

        # Fetch all ledger entries for this settlement
        led_rows = _db.query(
            "SELECT * FROM ledger_entries WHERE internal_ref = %s ORDER BY entry_id",
            (settle_id,),
        )
        if len(led_rows) < 2:
            return _jdump({
                "matched": False,
                "pay_id": pay_id,
                "reason": f"Only {len(led_rows)} ledger entry for settlement — not a split",
            })

        entry_sum = sum(_to_decimal(e["amount"]) for e in led_rows)
        delta = abs(entry_sum - pay_net)

        if delta <= Decimal("0.05"):          # allow rounding pennies
            entry_ids = [e["entry_id"] for e in led_rows]
            # Also find a bank txn
            bank_rows = _db.query(
                "SELECT * FROM bank_statements WHERE settlement_id = %s LIMIT 1",
                (settle_id,),
            )
            txn_id = bank_rows[0]["txn_id"] if bank_rows else None
            return _jdump({
                "matched": True,
                "pay_id": pay_id,
                "txn_id": txn_id,
                "entry_id": entry_ids[0],     # primary entry
                "entry_ids": entry_ids,        # all split parts
                "num_splits": len(entry_ids),
                "match_type": "multi_split",
                "confidence": 0.95,
                "delta": float(delta),
                "status": "matched",
            })

        return _jdump({
            "matched": False,
            "pay_id": pay_id,
            "reason": (
                f"Ledger sum {float(entry_sum):.2f} ≠ net_amount {float(pay_net):.2f} "
                f"(delta={float(delta):.2f}) — not a clean split"
            ),
        })
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: flag_exception
# ---------------------------------------------------------------------------

def flag_exception(
    pay_id: str,
    reason: str,
    agent_reasoning: str,
    suggested_action: str,
) -> str:
    """
    Record an unresolvable payment as an exception in the exceptions table
    and write an 'exception' match_result row.

    Args:
        pay_id: The Razorpay payment ID that could not be matched.
        reason: Short reason code, e.g. 'no_bank_credit', 'amount_mismatch'.
        agent_reasoning: Full text explanation of why the agent could not match
                         this payment (will be shown in the UI exception panel).
        suggested_action: Actionable recommendation for a human reviewer,
                          e.g. 'Contact bank to trace UTR HDFCN260812XXXXX'.

    Returns:
        JSON string {'saved': true, 'exception_id': 'exc_...'} or error.
    """
    try:
        exception_id = f"exc_{uuid.uuid4().hex[:8]}"

        # Write to exceptions table
        _db.execute(
            """
            INSERT INTO exceptions (exception_id, source, record_id, reason,
                                    agent_reasoning, suggested_action)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (exception_id) DO NOTHING
            """,
            (exception_id, "reconciler", pay_id, reason, agent_reasoning, suggested_action),
        )

        # Fetch ground_truth_error_type for accuracy scoring
        pay_rows = _db.query(
            "SELECT error_type FROM razorpay_payments WHERE pay_id = %s", (pay_id,)
        )
        gt = pay_rows[0]["error_type"] if pay_rows else "clean"

        # Write match_result row
        _db.execute(
            """
            INSERT INTO match_results
                (pay_id, entry_id, txn_id, match_type, confidence, delta, status,
                 ground_truth_error_type)
            VALUES (%s, NULL, NULL, 'unmatched', 0.0, NULL, 'exception', %s)
            ON CONFLICT (pay_id) DO UPDATE SET
                match_type              = 'unmatched',
                confidence              = 0.0,
                delta                   = NULL,
                status                  = 'exception',
                ground_truth_error_type = EXCLUDED.ground_truth_error_type
            """,
            (pay_id, gt),
        )

        return _jdump({"saved": True, "exception_id": exception_id})
    except Exception as exc:                                     # noqa: BLE001
        return _jdump({"error": str(exc)})
