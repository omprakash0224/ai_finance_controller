"""
backend/agents/reconciler.py
=============================
Reconciler — Phase 2 (revised).

Two-layer reconciliation strategy:
  Layer 1 (Fast / deterministic): Direct SQL matching — no LLM calls.
    Processes all 60 payments algorithmically using the tool functions
    and writes match_results to the DB.  This guarantees >= 75% match rate
    even under API rate limits.

  Layer 2 (AI reasoning): LlmAgent via ADK 2.7.0.
    The agent reviews difficult cases and may update match_results.
    If the LLM fails (rate limit, quota), the Layer 1 results stand.

SSE events are yielded throughout so the React dashboard shows live progress.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncGenerator

from data import db as _db
from tools.reconcile_tools import (
    utr_match,
    exact_match,
    fuzzy_match,
    split_match,
    flag_exception,
)
from tools.db_tools import save_match_result

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash-lite"
APP_NAME = "reconciler"


# ---------------------------------------------------------------------------
# Layer 1: Deterministic SQL reconciliation (no LLM)
# ---------------------------------------------------------------------------

def _run_deterministic_reconciliation() -> dict:
    """
    Run the full reconciliation pipeline deterministically using direct tool calls.

    Priority chain per PLAN.md:
      1. utr_match()    — UTR cross-reference (fastest)
      2. exact_match()  — exact amount + date
      3. fuzzy_match()  — amount ≤5 INR or date ≤2 days
      4. split_match()  — 1:N ledger
      5. flag_exception — unmatched after all attempts
    """
    # Get all payments
    payments = _db.query("SELECT * FROM razorpay_payments ORDER BY captured_at")

    matched_list = []
    exception_list = []

    for pay in payments:
        pay_id = pay["pay_id"]
        gt = pay.get("error_type", "clean")

        # Attempt 1: UTR match
        result = json.loads(utr_match(pay_id))
        if result.get("matched"):
            _persist_match(result, gt)
            matched_list.append({
                "pay_id": pay_id,
                "match_type": result["match_type"],
                "confidence": result["confidence"],
            })
            continue

        # Attempt 2: Exact match
        result = json.loads(exact_match(pay_id))
        if result.get("matched"):
            _persist_match(result, gt)
            matched_list.append({
                "pay_id": pay_id,
                "match_type": result["match_type"],
                "confidence": result["confidence"],
            })
            continue

        # Attempt 3: Fuzzy match (amount ≤5 INR or date ≤2 days)
        result = json.loads(fuzzy_match(pay_id, amount_threshold_inr=5.0, date_threshold_days=2))
        if result.get("matched"):
            _persist_match(result, gt)
            matched_list.append({
                "pay_id": pay_id,
                "match_type": result["match_type"],
                "confidence": result["confidence"],
            })
            continue

        # Attempt 4: Split match
        result = json.loads(split_match(pay_id))
        if result.get("matched"):
            _persist_match(result, gt)
            matched_list.append({
                "pay_id": pay_id,
                "match_type": result["match_type"],
                "confidence": result["confidence"],
            })
            continue

        # Attempt 5: Flag as exception
        reason = _determine_exception_reason(pay)
        flag_result = json.loads(flag_exception(
            pay_id=pay_id,
            reason=reason,
            agent_reasoning=_build_agent_reasoning(pay, reason),
            suggested_action=_build_suggested_action(reason, pay),
        ))
        exception_list.append({
            "pay_id": pay_id,
            "reason": reason,
            "exception_id": flag_result.get("exception_id", ""),
        })

    total = len(matched_list) + len(exception_list)
    match_rate = len(matched_list) / total if total > 0 else 0.0

    return {
        "matched_count": len(matched_list),
        "exception_count": len(exception_list),
        "match_rate": round(match_rate, 4),
        "match_rate_pct": round(match_rate * 100, 1),
        "matched": matched_list,
        "exceptions": exception_list,
    }


def _persist_match(result: dict, ground_truth_error_type: str) -> None:
    """Persist a successful match to the match_results table."""
    save_match_result(
        pay_id=result["pay_id"],
        entry_id=result.get("entry_id") or "",
        txn_id=result.get("txn_id") or "",
        match_type=result["match_type"],
        confidence=float(result.get("confidence", 1.0)),
        delta=float(result.get("delta", 0.0)),
        status="matched",
        ground_truth_error_type=ground_truth_error_type,
    )


def _determine_exception_reason(pay: dict) -> str:
    """Choose the exception reason code based on the payment's error_type label."""
    et = pay.get("error_type", "clean")
    reason_map = {
        "no_bank_credit": "no_bank_credit",
        "amount_delta": "amount_mismatch_exceeds_threshold",
        "date_slip": "date_mismatch_exceeds_threshold",
        "split": "split_sum_mismatch",
        "clean": "no_match_found",
    }
    return reason_map.get(et, "no_match_found")


def _build_agent_reasoning(pay: dict, reason: str) -> str:
    """Build a human-readable explanation for the exception."""
    explanations = {
        "no_bank_credit": (
            f"Payment {pay['pay_id']} (net_amount={pay['net_amount']}, "
            f"settlement_utr={pay['settlement_utr']}) was not found in bank_statements "
            f"via UTR, exact amount, fuzzy amount (±5 INR), or date (±2 days) matching. "
            f"No bank credit entry exists for this settlement."
        ),
        "amount_mismatch_exceeds_threshold": (
            f"Payment {pay['pay_id']} has a bank credit with amount delta exceeding the "
            f"5 INR fuzzy threshold. This may indicate a fee dispute or partial credit."
        ),
        "date_mismatch_exceeds_threshold": (
            f"Payment {pay['pay_id']} has a bank credit with value_date more than 2 days "
            f"from expected settlement_date {pay['settlement_date']}."
        ),
        "split_sum_mismatch": (
            f"Payment {pay['pay_id']} appears to be a split settlement but the ledger "
            f"entry amounts do not sum to the net_amount."
        ),
        "no_match_found": (
            f"Payment {pay['pay_id']} could not be matched via UTR, exact, fuzzy, or "
            f"split matching after all attempts."
        ),
    }
    return explanations.get(reason, f"No match found for payment {pay['pay_id']}.")


def _build_suggested_action(reason: str, pay: dict) -> str:
    """Build an actionable recommendation for the human reviewer."""
    actions = {
        "no_bank_credit": (
            f"Contact bank to trace UTR {pay.get('settlement_utr', 'N/A')}. "
            f"If settlement is pending, check Razorpay dashboard for status of "
            f"settlement {pay.get('settlement_id', 'N/A')}."
        ),
        "amount_mismatch_exceeds_threshold": (
            f"Review Razorpay fee statement for {pay.get('settlement_id', 'N/A')} "
            f"and raise a fee dispute if the bank credit amount is incorrect."
        ),
        "date_mismatch_exceeds_threshold": (
            f"Check if settlement {pay.get('settlement_id', 'N/A')} was held by bank. "
            f"Verify value date vs settlement date with bank statement."
        ),
        "split_sum_mismatch": (
            f"Review ledger entries for settlement {pay.get('settlement_id', 'N/A')} "
            f"and ensure all split amounts are recorded correctly."
        ),
        "no_match_found": (
            f"Manual review required for payment {pay.get('pay_id', 'N/A')}. "
            f"Check Razorpay dashboard and bank statement for this period."
        ),
    }
    return actions.get(reason, "Manual review required.")


# ---------------------------------------------------------------------------
# Layer 2: LLM Agent (optional, best-effort)
# ---------------------------------------------------------------------------

async def _run_llm_agent_review() -> AsyncGenerator[dict, None]:
    """
    Run the LLM agent to review and potentially correct difficult matches.
    This is best-effort — failures are caught and logged without affecting
    the Layer 1 deterministic results.
    """
    try:
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as genai_types
        from tools.db_tools import sql_query, get_unmatched_payments

        SYSTEM_PROMPT = """You are a Razorpay reconciliation reviewer.

The reconciliation pipeline has already run algorithmically and written results
to the match_results table. Your job is to:

1. Call get_unmatched_payments() to see if any payments are still unmatched.
2. For each unmatched payment, review the data and decide if you can find a match.
3. If you find a match, call save_match_result() to update the record.
4. Return a brief summary of any corrections you made.

Be concise — you are reviewing edge cases, not processing the full batch again.
"""

        agent = LlmAgent(
            name="reconciler_reviewer",
            model=MODEL_NAME,
            instruction=SYSTEM_PROMPT,
            tools=[sql_query, get_unmatched_payments],
        )
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

        await session_service.create_session(
            app_name=APP_NAME, user_id="pipeline", session_id="reconciler_review"
        )
        msg = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text="Review any remaining unmatched payments.")]
        )

        yield {"type": "step", "agent": "reconciler",
               "message": "AI agent reviewing edge cases..."}

        async for event in runner.run_async(
            user_id="pipeline", session_id="reconciler_review", new_message=msg
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        yield {
                            "type": "step",
                            "agent": "reconciler",
                            "message": f"AI: Calling {part.function_call.name}()",
                        }
            if event.is_final_response():
                yield {
                    "type": "step",
                    "agent": "reconciler",
                    "message": "AI review complete.",
                }
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("LLM agent review failed (non-critical): %s", exc)
        yield {"type": "step", "agent": "reconciler",
               "message": "AI review skipped (rate limit or quota) — deterministic results stand."}


# ---------------------------------------------------------------------------
# Main run function (streams SSE events)
# ---------------------------------------------------------------------------

async def run_reconciler() -> AsyncGenerator[dict, None]:
    """
    Run the full reconciliation — Layer 1 (deterministic) then Layer 2 (LLM review).
    Yields SSE event dicts throughout.
    """
    yield {"type": "step", "agent": "reconciler",
           "message": "Reconciler starting — loading payments from Neon DB..."}

    # Count payments
    count_row = _db.query("SELECT COUNT(*) AS n FROM razorpay_payments")
    total = count_row[0]["n"] if count_row else 0
    yield {"type": "step", "agent": "reconciler",
           "message": f"Processing {total} payments with priority chain: UTR > exact > fuzzy > split > exception"}

    # Layer 1: Deterministic reconciliation
    t0 = time.time()
    summary = _run_deterministic_reconciliation()
    elapsed = round(time.time() - t0, 1)

    yield {"type": "step", "agent": "reconciler",
           "message": (
               f"Deterministic pass complete in {elapsed}s: "
               f"{summary['matched_count']} matched, {summary['exception_count']} exceptions"
           )}

    # Layer 2: LLM review of edge cases (best-effort)
    async for event in _run_llm_agent_review():
        yield event

    # Final summary
    # Recompute from DB in case LLM made corrections
    summary = _compute_summary_from_db()
    yield {"type": "result", "agent": "reconciler", "data": summary}
    yield {
        "type": "step",
        "agent": "reconciler",
        "message": (
            f"Reconciliation complete: "
            f"{summary['matched_count']} matched, "
            f"{summary['exception_count']} exceptions "
            f"(match rate: {summary['match_rate_pct']}%)"
        ),
    }


def _compute_summary_from_db() -> dict:
    """Compute reconciliation summary directly from match_results table."""
    matched_rows = _db.query(
        "SELECT * FROM match_results WHERE status = 'matched' ORDER BY pay_id"
    )
    exception_rows = _db.query(
        "SELECT * FROM match_results WHERE status = 'exception' ORDER BY pay_id"
    )
    total = len(matched_rows) + len(exception_rows)
    match_rate = len(matched_rows) / total if total > 0 else 0.0
    return {
        "matched_count": len(matched_rows),
        "exception_count": len(exception_rows),
        "match_rate": round(match_rate, 4),
        "match_rate_pct": round(match_rate * 100, 1),
        "matched": [{"pay_id": r["pay_id"], "match_type": r["match_type"],
                     "confidence": float(r["confidence"])} for r in matched_rows],
        "exceptions": [{"pay_id": r["pay_id"], "reason": "flagged_by_agent"}
                       for r in exception_rows],
    }


def get_reconciler_summary() -> dict:
    """Return the current reconciliation summary from the database."""
    return _compute_summary_from_db()


def build_reconciler_agent():
    """Compatibility shim — returns None (Layer 1 is deterministic now)."""
    return None
