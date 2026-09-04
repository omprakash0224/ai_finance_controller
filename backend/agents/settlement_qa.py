"""
backend/agents/settlement_qa.py
================================
Settlement Q&A agent — Phase 2 (cache-optimised rewrite).

Performance Architecture
------------------------
Three-tier response strategy to achieve sub-second latency on 1M+ records
while keeping AI token cost near zero for repeated questions:

  Tier 1 — Upstash Redis Cache (< 5 ms)
    Every Q&A result is stored in Upstash Redis with a configurable TTL.
    On cache hit the answer is returned immediately — no SQL, no AI cost.
    Cache keys are SHA-256 hashes of the normalised question text.

  Tier 2 — Smart SQL Fast-Path (< 80 ms, $0 AI cost)
    Questions matching known aggregate patterns (match rate, GST, pending
    settlement, exceptions, daily volume, method breakdown) are answered
    directly from pre-aggregated SQL queries in tools/metrics_views.py.
    The result bypasses Gemini entirely, then is stored in Redis.

  Tier 3 — Gemini LLM Agent (< 3 s, standard AI cost)
    Novel or complex questions that don't match any fast-path pattern are
    forwarded to the LlmAgent with an updated system prompt that instructs
    it to query the pre-aggregated views first.  The response is stored
    in Redis for subsequent identical questions.

Cache Invalidation
------------------
  - After every successful pipeline run, all metric keys are flushed and
    recomputed (warm-up) so the cache always reflects the latest data.
  - POST /api/qa/cache/flush manually flushes all Q&A and metrics keys.
  - Individual answer keys expire automatically via TTL.

Environment Variables
---------------------
  UPSTASH_REDIS_URL           — Upstash REST endpoint URL
  UPSTASH_REDIS_TOKEN         — Upstash REST auth token
  QA_CACHE_TTL_SECONDS        — Answer cache TTL (default 300 = 5 min)
  METRICS_CACHE_TTL_SECONDS   — Metrics cache TTL (default 60 = 1 min)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from tools.db_tools import sql_query
from tools.cache import (
    get_cached,
    set_cached,
    qa_cache_key,
    metrics_cache_key,
    cached_metrics,
    is_cache_available,
    _qa_ttl,
    _metrics_ttl,
)
from tools.metrics_views import (
    get_match_rate,
    get_pending_settlement,
    get_gst_summary,
    get_exception_summary,
    get_daily_volume,
    get_method_breakdown,
    get_all_metrics,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash-lite"
APP_NAME   = "settlement_qa"

# ---------------------------------------------------------------------------
# Updated system prompt — instructs the agent to prefer pre-aggregated views
# ---------------------------------------------------------------------------

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

Performance rules (IMPORTANT for 1M+ row databases):
- For AGGREGATE questions (totals, counts, averages, rates), ALWAYS use
  GROUP BY with specific WHERE clauses. Never SELECT * on large tables.
- For match rate / GST totals / pending amounts, use:
    SELECT COUNT(*), SUM(...) FROM match_results / razorpay_payments with GROUP BY
  These are indexed and will complete in < 80 ms.
- For specific payment lookups, always filter with pay_id or settlement_id WHERE clause.
- Only use SELECT / WITH queries (no DML).

Always return a JSON object:
  {
    "question": "<original question>",
    "sql": "<the SQL you executed>",
    "answer": "<concise human-readable answer>",
    "data": [<supporting rows — max 50 rows>]
  }
- If a question is ambiguous, answer the most likely interpretation and note assumptions.
- Format monetary amounts as INR with 2 decimal places.
- Limit data arrays to 50 rows maximum.
"""


# ---------------------------------------------------------------------------
# Tier 2: Smart fast-path router — keyword pattern matching
# ---------------------------------------------------------------------------
#
# Each entry maps a compiled regex pattern to a (metric_name, compute_fn) pair.
# If the normalised question matches, the metric is fetched from cache or
# computed from SQL — never from Gemini.
#
# Patterns are checked in order; the first match wins.

_FAST_PATH_ROUTES: list[tuple[re.Pattern, str, Any]] = [
    # Match rate / reconciliation status
    (re.compile(r"match.?rate|reconcil|how many.*(match|reconcil)|unmatched", re.I),
     "match_rate", get_match_rate),

    # Pending settlement amount
    (re.compile(r"pending.*(settlement|amount|total)|how much.*(pending|owed|due)", re.I),
     "pending_settlement", get_pending_settlement),

    # GST / tax
    (re.compile(r"\bgst\b|tax.*(collect|total|amount)|igst|cgst|sgst", re.I),
     "gst_summary", get_gst_summary),

    # Exceptions / errors
    (re.compile(r"exception|not reconcil|manual.?review|error.?type|which.*fail", re.I),
     "exception_summary", get_exception_summary),

    # Daily / weekly volume trends
    (re.compile(r"daily.*(volume|amount|trend)|per.?day|volume.*(trend|over)", re.I),
     "daily_volume", get_daily_volume),

    # Payment method breakdown
    (re.compile(r"\b(upi|card|netbanking|wallet)\b.*breakdown|method.*(split|break|volume)|"
                r"breakdown.*method", re.I),
     "method_breakdown", get_method_breakdown),
]


def _fast_path_answer(question: str) -> tuple[str | None, dict | None]:
    """
    Check if a question matches a fast-path pattern.

    Returns (metric_name, result_dict) if matched, (None, None) otherwise.
    The result is fetched from Upstash Redis if cached, computed from SQL
    if not, and then stored in Redis for future requests.
    """
    for pattern, metric_name, compute_fn in _FAST_PATH_ROUTES:
        if pattern.search(question):
            logger.debug("Fast-path match: '%s' → metric '%s'", question[:60], metric_name)
            result = cached_metrics(metric_name, compute_fn, _metrics_ttl())
            return metric_name, result

    return None, None


def _wrap_fast_path_as_qa(question: str, metric_name: str, data: dict) -> dict:
    """
    Wrap a pre-aggregated metrics dict into the standard Q&A response shape.
    """
    cache_status = data.pop("_cache", "miss")

    # Build a concise human-readable answer from the metric
    answer_builders = {
        "match_rate": lambda d: (
            f"Match rate is {d['match_rate_pct']}% "
            f"({d['matched_count']} matched, {d['exception_count']} exceptions "
            f"out of {d['total_processed']} total payments)."
        ),
        "pending_settlement": lambda d: (
            f"Total pending settlement: \u20b9{d['total_pending_inr']:,.2f} "
            f"across {d['pending_settlement_count']} settlement(s)."
        ),
        "gst_summary": lambda d: (
            f"Total GST collected: \u20b9{d['total_tax_inr']:,.2f} "
            f"from {d['total_tagged']} matched payments. "
            f"Breakdown: " +
            ", ".join(f"{k}: \u20b9{v['total_tax']:,.2f}" for k, v in d.get("by_gst_code", {}).items())
        ),
        "exception_summary": lambda d: (
            f"Total exceptions: {d['total_exceptions']}. "
            f"Top pattern: {d['top_patterns'][0]['reason']} "
            f"({d['top_patterns'][0]['count']} occurrences)."
            if d.get("top_patterns") else f"Total exceptions: {d['total_exceptions']}."
        ),
        "daily_volume": lambda d: (
            f"Total gross volume (last 30 days): \u20b9{d['total_gross_inr']:,.2f} "
            f"across {len(d['days'])} settlement dates."
        ),
        "method_breakdown": lambda d: (
            "Payment method breakdown: " +
            ", ".join(
                f"{m['method']}: {m['txn_count']} txns (\u20b9{m['gross_volume_inr']:,.2f})"
                for m in d.get("by_method", [])
            )
        ),
    }

    answer_fn  = answer_builders.get(metric_name)
    answer_txt = answer_fn(data) if answer_fn else f"Pre-computed metric: {metric_name}"

    # Supporting data rows (limit to 50)
    data_rows = (
        data.get("by_date") or
        data.get("by_method") or
        data.get("top_patterns") or
        data.get("by_gst_code") or
        []
    )
    if isinstance(data_rows, dict):
        data_rows = [{"code": k, **v} for k, v in data_rows.items()]
    data_rows = list(data_rows)[:50]

    return {
        "question":      question,
        "sql":           f"-- Pre-aggregated metric: {metric_name} (no LLM)",
        "answer":        answer_txt,
        "data":          data_rows,
        "_source":       f"fast_path:{metric_name}",
        "_cache":        cache_status,
    }


# ---------------------------------------------------------------------------
# Build LlmAgent
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Core answer_question generator — three-tier cache + fast-path + LLM
# ---------------------------------------------------------------------------

async def answer_question(question: str) -> AsyncGenerator[dict, None]:
    """
    Answer a settlement Q&A question using the three-tier strategy.

    Yields:
        {"type": "step",   "agent": "settlement_qa", "message": "...", "_source": "..."}
        {"type": "result", "agent": "settlement_qa", "data": {...}}
        {"type": "error",  "agent": "settlement_qa", "message": "..."}
    """
    # ------------------------------------------------------------------
    # Tier 1: Upstash Redis cache
    # ------------------------------------------------------------------
    cache_key = qa_cache_key(question)
    cached    = get_cached(cache_key)
    cache_on  = is_cache_available()

    if cached is not None:
        yield {
            "type":    "step",
            "agent":   "settlement_qa",
            "message": f"\u26a1 Cache HIT — returning stored answer ({cache_key[-8:]})",
            "_source": "cache",
        }
        yield {
            "type":    "result",
            "agent":   "settlement_qa",
            "data":    {**cached, "_cache": "hit"},
        }
        return

    yield {
        "type":    "step",
        "agent":   "settlement_qa",
        "message": (
            f"\ud83d\udcac Processing: {question[:80]}... "
            f"{'(cache miss)' if cache_on else '(cache disabled)'}"
        ),
        "_source": "router",
    }

    # ------------------------------------------------------------------
    # Tier 2: Smart SQL fast-path
    # ------------------------------------------------------------------
    metric_name, fast_result = _fast_path_answer(question)

    if fast_result is not None:
        result = _wrap_fast_path_as_qa(question, metric_name, fast_result)

        # Store the wrapped answer in Redis too (using QA TTL)
        answer_to_cache = {k: v for k, v in result.items() if not k.startswith("_")}
        set_cached(cache_key, answer_to_cache, _qa_ttl())

        yield {
            "type":    "step",
            "agent":   "settlement_qa",
            "message": (
                f"\u26a1 Fast-path answer from pre-aggregated SQL "
                f"(metric: {metric_name}, no AI cost)"
            ),
            "_source": f"fast_path:{metric_name}",
        }
        yield {"type": "result", "agent": "settlement_qa", "data": result}
        return

    # ------------------------------------------------------------------
    # Tier 3: Gemini LLM Agent
    # ------------------------------------------------------------------
    yield {
        "type":    "step",
        "agent":   "settlement_qa",
        "message": "\ud83e\udd16 Forwarding to Gemini SQL agent...",
        "_source": "llm",
    }

    agent           = build_qa_agent()
    session_service = InMemorySessionService()
    runner          = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

    session_id = f"qa_{uuid.uuid4().hex[:8]}"
    await session_service.create_session(
        app_name=APP_NAME, user_id="user", session_id=session_id
    )

    user_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=question)],
    )

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id="user", session_id=session_id, new_message=user_message
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        yield {
                            "type":    "step",
                            "agent":   "settlement_qa",
                            "message": "\ud83d\udee0  SQL query executing...",
                            "_source": "llm",
                        }
                    elif hasattr(part, "text") and part.text:
                        final_text = part.text
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            final_text = part.text
    except Exception as exc:                                         # noqa: BLE001
        logger.exception("Q&A agent error: %s", exc)
        yield {"type": "error", "agent": "settlement_qa", "message": str(exc)}
        return

    result = _parse_qa_result(question, final_text)

    # Cache the LLM result for future identical questions
    answer_to_cache = {k: v for k, v in result.items() if not k.startswith("_")}
    stored = set_cached(cache_key, answer_to_cache, _qa_ttl())
    result["_cache"]  = "miss"
    result["_source"] = "llm"
    result["_stored"] = stored

    yield {"type": "result", "agent": "settlement_qa", "data": result}


# ---------------------------------------------------------------------------
# Cache warm-up: call after pipeline runs to pre-populate Redis
# ---------------------------------------------------------------------------

async def warm_cache() -> dict[str, Any]:
    """
    Pre-compute and cache all pre-aggregated metrics in Upstash Redis.

    Should be called after every successful pipeline run so the first
    user Q&A request after reconciliation is instant.

    Returns a summary of what was cached.
    """
    if not is_cache_available():
        return {"status": "cache_disabled", "cached": 0}

    metrics   = get_all_metrics()
    cached_n  = 0
    ttl       = _metrics_ttl()

    for metric_name, data in metrics.items():
        key     = metrics_cache_key(metric_name)
        success = set_cached(key, data, ttl)
        if success:
            cached_n += 1

    logger.info("Cache warm-up complete: %d metrics cached (TTL %ds)", cached_n, ttl)
    return {
        "status":        "ok",
        "cached":        cached_n,
        "ttl_seconds":   ttl,
        "metric_names":  list(metrics.keys()),
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_qa_result(question: str, text: str) -> dict[str, Any]:
    """Extract JSON QA result from model response."""
    text  = text.strip()
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            result = json.loads(text[start:end])
            if "question" not in result:
                result["question"] = question
            # Enforce 50-row limit on data arrays
            if "data" in result and isinstance(result["data"], list):
                result["data"] = result["data"][:50]
            return result
        except json.JSONDecodeError:
            pass

    return {
        "question": question,
        "sql":      None,
        "answer":   text or "I was unable to process that question.",
        "data":     [],
    }


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------

async def run_qa(question: str) -> dict[str, Any]:
    """Run Q&A agent and return the final result dict."""
    result: dict[str, Any] = {}
    async for event in answer_question(question):
        if event.get("type") == "result":
            result = event.get("data", {})
    return result
