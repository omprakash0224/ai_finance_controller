"""
backend/agents/forecaster.py
=============================
Cash Forecaster ADK agent — Phase 2.

Responsibilities
----------------
- Query matched payments and pending settlements from the database
- Project 30-day daily cash inflows from pending T+1/T+2 settlements
- Account for matched (confirmed received) vs outstanding (expected) amounts
- Return a forecast_30d list with per-day projected inflow and cumulative balance

Forecast methodology:
  1. Today's known cash = sum of all matched (processed) settlement amounts
  2. Expected inflows = pending settlements grouped by settlement_date
  3. 30-day projection = day-by-day running total of expected + matched credits
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
from tools.razorpay_tools import list_pending_settlements, get_settlement_summary

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash-lite"
APP_NAME = "forecaster"

SYSTEM_PROMPT = """You are a cash flow forecasting agent for a merchant using Razorpay.

You have access to a Neon PostgreSQL database with:
  - settlements: settlement_id, settlement_date, total_amount, status
  - match_results: pay_id, status (matched/exception)
  - razorpay_payments: pay_id, net_amount, settlement_date, settlement_id

YOUR TASK:
1. Call get_settlement_summary() to understand current settlement status.
2. Call list_pending_settlements() to get all pending amounts by date.
3. Use sql_query() to get matched (processed) settlement totals.
4. Build a 30-day cash projection starting from today (2026-08-24):
   - For each of the next 30 days, show: date, projected_inflow, cumulative_balance
   - projected_inflow = sum of pending settlements due on that date
   - cumulative_balance = running total of all inflows
5. Include a summary with total_confirmed, total_pending, total_forecast.

Return a JSON object:
{
  "forecast_date": "2026-08-24",
  "total_confirmed_inr": <float>,
  "total_pending_inr": <float>,
  "total_forecast_30d_inr": <float>,
  "daily_forecast": [
    {
      "date": "YYYY-MM-DD",
      "projected_inflow": <float>,
      "cumulative_balance": <float>,
      "num_settlements": <int>,
      "source": "pending|confirmed|none"
    },
    ...
  ],
  "highlights": {
    "peak_inflow_date": "YYYY-MM-DD",
    "peak_inflow_amount": <float>,
    "days_with_inflow": <int>
  }
}
"""


def build_forecaster_agent() -> LlmAgent:
    """Instantiate the forecaster LlmAgent."""
    return LlmAgent(
        name="forecaster",
        model=MODEL_NAME,
        description=(
            "Projects 30-day merchant cash position from confirmed and "
            "pending Razorpay settlements."
        ),
        instruction=SYSTEM_PROMPT,
        tools=[sql_query, list_pending_settlements, get_settlement_summary],
    )


async def run_forecaster() -> AsyncGenerator[dict, None]:
    """
    Run the forecaster agent.

    Yields SSE event dicts with type 'step' or 'result'.
    Falls back to a computed forecast if the agent response cannot be parsed.
    """
    agent = build_forecaster_agent()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    await session_service.create_session(
        app_name=APP_NAME,
        user_id="pipeline",
        session_id="forecast_run",
    )

    prompt = (
        "Generate the 30-day cash flow forecast. "
        "Use the settlement data to project daily inflows. "
        "Return the complete forecast JSON."
    )

    user_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt)],
    )

    yield {"type": "step", "agent": "forecaster",
           "message": "Forecaster agent starting — projecting 30-day cash flow..."}

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id="pipeline",
            session_id="forecast_run",
            new_message=user_message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        yield {
                            "type": "step",
                            "agent": "forecaster",
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
        logger.exception("Forecaster error: %s", exc)
        yield {"type": "error", "agent": "forecaster", "message": str(exc)}
        forecast = _compute_forecast_from_db()
        yield {"type": "result", "agent": "forecaster", "data": forecast}
        return

    forecast = _parse_forecast(final_text)
    yield {"type": "result", "agent": "forecaster", "data": forecast}
    yield {
        "type": "step",
        "agent": "forecaster",
        "message": (
            f"✅ Forecast ready — "
            f"₹{forecast.get('total_pending_inr', '?')} pending, "
            f"₹{forecast.get('total_confirmed_inr', '?')} confirmed"
        ),
    }


def _parse_forecast(text: str) -> dict:
    """Extract JSON forecast from model response."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse forecast JSON — computing from DB")
    return _compute_forecast_from_db()


def _compute_forecast_from_db() -> dict:
    """Compute 30-day cash forecast directly from the database."""
    import datetime

    today = datetime.date(2026, 8, 24)
    end_date = today + datetime.timedelta(days=30)

    # Confirmed (processed) settlements
    confirmed_rows = _db.query(
        "SELECT COALESCE(SUM(total_amount), 0) AS total FROM settlements WHERE status = 'processed'"
    )
    total_confirmed = float(confirmed_rows[0]["total"] or 0)

    # Pending settlements by date
    pending_rows = _db.query(
        """
        SELECT settlement_date AS date,
               SUM(total_amount) AS amount,
               COUNT(*) AS num_settlements
        FROM settlements
        WHERE status = 'pending'
          AND settlement_date >= %s
          AND settlement_date <= %s
        GROUP BY settlement_date
        ORDER BY settlement_date
        """,
        (today.isoformat(), end_date.isoformat()),
    )

    pending_by_date = {
        str(r["date"]): {"amount": float(r["amount"] or 0), "count": int(r["num_settlements"])}
        for r in pending_rows
    }

    total_pending = sum(v["amount"] for v in pending_by_date.values())

    # Build 30-day daily forecast
    daily = []
    cumulative = 0.0
    peak_date = str(today)
    peak_amount = 0.0
    days_with_inflow = 0

    for i in range(30):
        d = today + datetime.timedelta(days=i)
        d_str = d.isoformat()
        day_data = pending_by_date.get(d_str, {"amount": 0.0, "count": 0})
        inflow = day_data["amount"]
        cumulative = round(cumulative + inflow, 2)

        if inflow > 0:
            days_with_inflow += 1
            if inflow > peak_amount:
                peak_amount = inflow
                peak_date = d_str

        daily.append({
            "date": d_str,
            "projected_inflow": round(inflow, 2),
            "cumulative_balance": cumulative,
            "num_settlements": day_data["count"],
            "source": "pending" if inflow > 0 else "none",
        })

    return {
        "forecast_date": str(today),
        "total_confirmed_inr": round(total_confirmed, 2),
        "total_pending_inr": round(total_pending, 2),
        "total_forecast_30d_inr": round(total_pending, 2),
        "daily_forecast": daily,
        "highlights": {
            "peak_inflow_date": peak_date,
            "peak_inflow_amount": round(peak_amount, 2),
            "days_with_inflow": days_with_inflow,
        },
    }
