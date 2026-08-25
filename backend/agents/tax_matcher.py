"""
backend/agents/tax_matcher.py
==============================
Tax Matcher ADK agent — Phase 2.

Responsibilities
----------------
- Fetch all matched payments from match_results
- Tag each payment to the correct GST code based on transaction type and method
- Return a tax_summary dict with per-code totals

GST Code Logic (simplified for Indian domestic payments):
  - UPI / NetBanking / Wallet    → IGST @ 18% on fee
  - Card (domestic)              → CGST @ 9% + SGST @ 9% on fee
  - Refunded payments            → GST reverse entry
  - Amounts < 1000 INR           → potentially exempt (for micromerchants)

The tax amount is already present in the razorpay_payments table (tax field)
as it was computed by the generator.  This agent's job is to:
  1. Confirm/tag the GST code for each transaction
  2. Aggregate totals by GST code
  3. Flag ambiguous cases (e.g., cross-state transactions)
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from data import db as _db
from tools.db_tools import sql_query

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash-lite"
APP_NAME = "tax_matcher"

SYSTEM_PROMPT = """You are a GST tax-line matcher agent for Indian digital payments.

You have access to a Neon PostgreSQL database with:
  - razorpay_payments: pay_id, method, amount, fee, tax, status, net_amount
  - match_results: pay_id, status (matched / exception)

YOUR TASK:
1. Use sql_query() to fetch all matched payments joined with their tax details.
2. For each payment, determine the correct GST code:
   - method = 'upi' or 'netbanking' or 'wallet'  → "IGST@18%" (inter-state service)
   - method = 'card' (domestic)                   → "CGST@9%+SGST@9%" (intra-state)
   - amount < 1000 INR                            → check if "Exempt" applies
   - status = 'refunded'                          → "GST_REVERSAL"
3. Aggregate totals by GST code.
4. Flag any payments where the tax amount seems inconsistent (delta > 0.50 INR
   from expected 18% of fee).

Return a JSON object:
{
  "total_tax_inr": <float>,
  "by_gst_code": {
    "IGST@18%": {"count": <int>, "total_tax": <float>, "total_fee": <float>},
    "CGST@9%+SGST@9%": {"count": <int>, "total_tax": <float>, "total_fee": <float>},
    "Exempt": {"count": <int>, "total_tax": <float>, "total_fee": <float>},
    "GST_REVERSAL": {"count": <int>, "total_tax": <float>, "total_fee": <float>}
  },
  "ambiguous": [{"pay_id": ..., "reason": ...}],
  "total_tagged": <int>
}
"""


def build_tax_matcher_agent() -> LlmAgent:
    """Instantiate the tax matcher LlmAgent."""
    return LlmAgent(
        name="tax_matcher",
        model=MODEL_NAME,
        description=(
            "Tags each matched Razorpay payment to its correct GST code "
            "(IGST, CGST+SGST, Exempt, or GST Reversal) and aggregates totals."
        ),
        instruction=SYSTEM_PROMPT,
        tools=[sql_query],
    )


async def run_tax_matcher() -> AsyncGenerator[dict, None]:
    """
    Run the tax matcher agent.

    Yields SSE event dicts with type 'step' or 'result'.
    Falls back to a computed summary if the agent response cannot be parsed.
    """
    agent = build_tax_matcher_agent()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    await session_service.create_session(
        app_name=APP_NAME,
        user_id="pipeline",
        session_id="tax_run",
    )

    prompt = (
        "Run the full GST tax tagging process for all matched payments. "
        "Return the complete tax_summary JSON."
    )

    user_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt)],
    )

    yield {"type": "step", "agent": "tax_matcher",
           "message": "Tax Matcher agent starting — tagging GST codes..."}

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id="pipeline",
            session_id="tax_run",
            new_message=user_message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        yield {
                            "type": "step",
                            "agent": "tax_matcher",
                            "message": f"🛠  Calling {part.function_call.name}()",
                        }
                    elif hasattr(part, "text") and part.text:
                        final_text = part.text
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            final_text = part.text
    except Exception as exc:                                     # noqa: BLE001
        logger.exception("Tax matcher error: %s", exc)
        yield {"type": "error", "agent": "tax_matcher", "message": str(exc)}
        tax_summary = _compute_tax_summary_from_db()
        yield {"type": "result", "agent": "tax_matcher", "data": tax_summary}
        return

    tax_summary = _parse_tax_summary(final_text)
    yield {"type": "result", "agent": "tax_matcher", "data": tax_summary}
    yield {
        "type": "step",
        "agent": "tax_matcher",
        "message": (
            f"Tax tagging complete — "
            f"{tax_summary.get('total_tagged', '?')} payments tagged, "
            f"total GST: ₹{tax_summary.get('total_tax_inr', '?')}"
        ),
    }


def _parse_tax_summary(text: str) -> dict:
    """Extract JSON from model response, fall back to DB computation."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse tax_summary JSON — computing from DB")
    return _compute_tax_summary_from_db()


def _compute_tax_summary_from_db() -> dict:
    """Compute GST tax summary directly from the database."""
    rows = _db.query(
        """
        SELECT p.pay_id, p.method, p.amount, p.fee, p.tax, p.status
        FROM razorpay_payments p
        INNER JOIN match_results m ON m.pay_id = p.pay_id
        WHERE m.status = 'matched'
        """
    )

    by_code: dict[str, dict] = {}
    ambiguous = []
    total_tax = 0.0

    for row in rows:
        method = row["method"]
        amount = float(row["amount"] or 0)
        fee = float(row["fee"] or 0)
        tax = float(row["tax"] or 0)

        # Determine GST code
        if row["status"] == "refunded":
            code = "GST_REVERSAL"
        elif amount < 1000:
            code = "Exempt"
        elif method in ("upi", "netbanking", "wallet"):
            code = "IGST@18%"
        else:
            code = "CGST@9%+SGST@9%"

        # Flag inconsistencies
        expected_tax = round(fee * 0.18, 2)
        if abs(expected_tax - tax) > 0.5 and code not in ("Exempt", "GST_REVERSAL"):
            ambiguous.append({
                "pay_id": row["pay_id"],
                "reason": f"expected_tax={expected_tax:.2f}, actual={tax:.2f}",
            })

        if code not in by_code:
            by_code[code] = {"count": 0, "total_tax": 0.0, "total_fee": 0.0}
        by_code[code]["count"] += 1
        by_code[code]["total_tax"] = round(by_code[code]["total_tax"] + tax, 2)
        by_code[code]["total_fee"] = round(by_code[code]["total_fee"] + fee, 2)
        total_tax += tax

    return {
        "total_tax_inr": round(total_tax, 2),
        "by_gst_code": by_code,
        "ambiguous": ambiguous,
        "total_tagged": len(rows),
    }
