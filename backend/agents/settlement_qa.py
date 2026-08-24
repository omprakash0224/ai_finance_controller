"""
backend/agents/settlement_qa.py
================================
Settlement Q&A ADK agent — Phase 2.

Responsibilities
----------------
- Accept natural-language questions about settlement and reconciliation data
- Generate and execute SQL queries against the Neon PostgreSQL database
- Return structured answers with supporting data

Supported question categories:
  - "How much is pending settlement?"
  - "Which payments were not reconciled?"
  - "What is the match rate?"
  - "Show me all T+2 settlements"
  - "What is the total GST collected?"
  - "Which exceptions need manual review?"
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from tools.db_tools import sql_query

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash-lite"
APP_NAME = "settlement_qa"

SYSTEM_PROMPT = """You are a settlement Q&A agent with access to a Neon PostgreSQL database
containing reconciled Razorpay payment data.

Database schema:
  razorpay_payments(pay_id, order_id, captured_at, amount, currency, method,
                    status, settlement_id, settlement_date, settlement_utr,
                    fee, tax, net_amount, error_type)
  bank_statements(txn_id, value_date, amount, description, bank_ref, currency, settlement_id)
  ledger_entries(entry_id, date, amount, narration, account_code, internal_ref)
  settlements(settlement_id, settlement_date, total_amount, num_payments, status)
  match_results(pay_id, entry_id, txn_id, match_type, confidence, delta, status,
                ground_truth_error_type)
  exceptions(exception_id, source, record_id, reason, agent_reasoning, suggested_action)

Rules:
- Use sql_query() to answer every question — never guess numbers
- Only use SELECT / WITH queries (no DML)
- Always return a JSON object:
  {
    "question": "<original question>",
    "sql": "<the SQL you executed>",
    "answer": "<concise human-readable answer>",
    "data": [<supporting rows>]
  }
- If a question is ambiguous, answer the most likely interpretation and note assumptions
- Format monetary amounts as INR with 2 decimal places in the answer text
"""


def build_qa_agent() -> LlmAgent:
    """Instantiate the settlement Q&A LlmAgent."""
    return LlmAgent(
        name="settlement_qa",
        model=MODEL_NAME,
        description=(
            "Answers natural-language questions about Razorpay settlements, "
            "reconciliation status, exceptions, and cash positions using SQL."
        ),
        instruction=SYSTEM_PROMPT,
        tools=[sql_query],
    )


async def answer_question(question: str) -> AsyncGenerator[dict, None]:
    """
    Run the Q&A agent for a single question.

    Yields:
        {"type": "step", "agent": "settlement_qa", "message": "..."}
        {"type": "result", "agent": "settlement_qa", "data": {...}}
        {"type": "error", "agent": "settlement_qa", "message": "..."}
    """
    agent = build_qa_agent()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    import uuid
    session_id = f"qa_{uuid.uuid4().hex[:8]}"
    await session_service.create_session(
        app_name=APP_NAME,
        user_id="user",
        session_id=session_id,
    )

    user_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=question)],
    )

    yield {"type": "step", "agent": "settlement_qa",
           "message": f"💬 Processing question: {question[:80]}..."}

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id="user",
            session_id=session_id,
            new_message=user_message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        yield {
                            "type": "step",
                            "agent": "settlement_qa",
                            "message": f"🛠  SQL query executing...",
                        }
                    elif hasattr(part, "text") and part.text:
                        final_text = part.text
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            final_text = part.text
    except Exception as exc:                                     # noqa: BLE001
        logger.exception("Q&A agent error: %s", exc)
        yield {"type": "error", "agent": "settlement_qa", "message": str(exc)}
        return

    result = _parse_qa_result(question, final_text)
    yield {"type": "result", "agent": "settlement_qa", "data": result}


def _parse_qa_result(question: str, text: str) -> dict[str, Any]:
    """Extract JSON QA result from model response."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(text[start:end])
            if "question" not in result:
                result["question"] = question
            return result
        except json.JSONDecodeError:
            pass

    # Fallback: wrap the raw text
    return {
        "question": question,
        "sql": None,
        "answer": text or "I was unable to process that question.",
        "data": [],
    }


# ---------------------------------------------------------------------------
# Synchronous helper for simple queries (used in tests)
# ---------------------------------------------------------------------------

async def run_qa(question: str) -> dict[str, Any]:
    """Run Q&A agent and return the final result dict."""
    result = {}
    async for event in answer_question(question):
        if event.get("type") == "result":
            result = event.get("data", {})
    return result
