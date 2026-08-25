"""
backend/agents/orchestrator.py
===============================
Pipeline Orchestrator — Phase 2.

Coordinates the three specialist agents in sequence:
  1. Reconciler    — match all payments, write match_results + exceptions
  2. TaxMatcher   — tag matched payments to GST codes
  3. Forecaster   — project 30-day cash from settled + pending amounts

Emits SSE events throughout so the React dashboard can show live progress.
The final report is stored in-memory and returned by GET /api/report.

SSE event schema:
  { "type": "step" | "result" | "error" | "done",
    "agent": "orchestrator" | "reconciler" | "tax_matcher" | "forecaster",
    "message": "...",      # human-readable (for step/error events)
    "data": {...}          # payload (for result/done events)
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator

from agents.reconciler import run_reconciler
from agents.tax_matcher import run_tax_matcher
from agents.forecaster import run_forecaster

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory report store (reset on each pipeline run)
# ---------------------------------------------------------------------------

_last_report: dict[str, Any] = {}
_run_in_progress: bool = False


def get_last_report() -> dict[str, Any]:
    """Return the report from the most recent pipeline run."""
    return _last_report


def is_run_in_progress() -> bool:
    """Return True if a pipeline run is currently executing."""
    return _run_in_progress


# ---------------------------------------------------------------------------
# Accuracy computation
# ---------------------------------------------------------------------------

def _compute_accuracy(reconciler_summary: dict) -> dict:
    """
    Compute a confusion-matrix-style accuracy breakdown.

    Ground truth error types (from generator):
      clean          → should be matched  (TP if matched, FN if exception)
      amount_delta   → should be fuzzy_matched (TP if matched, FN if exception)
      date_slip      → should be fuzzy_matched
      split          → should be split_matched
      no_bank_credit → should be exception (TP if exception, FP if matched)

    Columns:
      TP  = correctly handled (matched when should match, exception when should except)
      FP  = incorrectly handled (matched when should except, or vice versa)
      unresolved = not processed
    """
    from data import db as _db

    rows = _db.query(
        """
        SELECT pay_id, match_type, status, ground_truth_error_type
        FROM match_results
        ORDER BY pay_id
        """
    )

    tp = 0
    fp = 0
    unresolved = 0

    confusion: dict[str, dict] = {}

    for row in rows:
        gt = row["ground_truth_error_type"]
        status = row["status"]
        match_type = row["match_type"]

        if gt not in confusion:
            confusion[gt] = {"tp": 0, "fp": 0, "total": 0}
        confusion[gt]["total"] += 1

        if status == "exception":
            if gt == "no_bank_credit":
                # Correctly flagged as exception
                tp += 1
                confusion[gt]["tp"] += 1
            else:
                # Should have been matched
                fp += 1
                confusion[gt]["fp"] += 1
        elif status == "matched":
            if gt == "no_bank_credit":
                # Should have been an exception — false positive match
                fp += 1
                confusion[gt]["fp"] += 1
            else:
                # Correctly matched
                tp += 1
                confusion[gt]["tp"] += 1
        else:
            unresolved += 1

    total = len(rows)
    accuracy = tp / total if total > 0 else 0.0
    fp_rate = fp / total if total > 0 else 0.0

    return {
        "total_processed": total,
        "true_positives": tp,
        "false_positives": fp,
        "unresolved": unresolved,
        "accuracy": round(accuracy, 4),
        "accuracy_pct": round(accuracy * 100, 1),
        "false_positive_rate": round(fp_rate, 4),
        "false_positive_rate_pct": round(fp_rate * 100, 1),
        "by_error_type": confusion,
    }


# ---------------------------------------------------------------------------
# Main orchestrator runner
# ---------------------------------------------------------------------------

async def run_pipeline() -> AsyncGenerator[dict, None]:
    """
    Run the full finance-ops pipeline and stream SSE events.

    Sequence:
      1. Emit pipeline start event
      2. Run Reconciler (streaming)
      3. Run TaxMatcher (streaming)
      4. Run Forecaster (streaming)
      5. Compute accuracy report
      6. Assemble and store final report
      7. Emit done event with full report

    Yields SSE event dicts throughout.
    """
    global _last_report, _run_in_progress

    if _run_in_progress:
        yield {
            "type": "error",
            "agent": "orchestrator",
            "message": "A pipeline run is already in progress. Please wait.",
        }
        return

    _run_in_progress = True
    start_time = time.time()

    yield {
        "type": "step",
        "agent": "orchestrator",
        "message": "AI Finance Pipeline starting — Reconciler → TaxMatcher → Forecaster",
    }

    reconciler_result: dict = {}
    tax_result: dict = {}
    forecast_result: dict = {}

    try:
        # ----------------------------------------------------------------
        # Stage 1 — Reconciler
        # ----------------------------------------------------------------
        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 1/3: Running Reconciler agent..."}

        async for event in run_reconciler():
            yield event
            if event.get("type") == "result" and event.get("agent") == "reconciler":
                reconciler_result = event.get("data", {})

        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 1 complete. Starting Tax Matcher..."}

        # ----------------------------------------------------------------
        # Stage 2 — Tax Matcher
        # ----------------------------------------------------------------
        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 2/3: Running Tax Matcher agent..."}

        async for event in run_tax_matcher():
            yield event
            if event.get("type") == "result" and event.get("agent") == "tax_matcher":
                tax_result = event.get("data", {})

        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 2 complete. Starting Forecaster..."}

        # ----------------------------------------------------------------
        # Stage 3 — Forecaster
        # ----------------------------------------------------------------
        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 3/3: Running Forecaster agent..."}

        async for event in run_forecaster():
            yield event
            if event.get("type") == "result" and event.get("agent") == "forecaster":
                forecast_result = event.get("data", {})

        yield {"type": "step", "agent": "orchestrator",
               "message": "Stage 3 complete. Computing accuracy report..."}

        # ----------------------------------------------------------------
        # Accuracy report
        # ----------------------------------------------------------------
        accuracy = _compute_accuracy(reconciler_result)

        # ----------------------------------------------------------------
        # Assemble final report
        # ----------------------------------------------------------------
        elapsed = round(time.time() - start_time, 1)
        report = {
            "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": elapsed,
            "match_rate": reconciler_result.get("match_rate", 0.0),
            "match_rate_pct": reconciler_result.get(
                "match_rate_pct",
                round(reconciler_result.get("match_rate", 0.0) * 100, 1),
            ),
            "matched_count": reconciler_result.get("matched_count", 0),
            "exception_count": reconciler_result.get("exception_count", 0),
            "matched": reconciler_result.get("matched", []),
            "exceptions": reconciler_result.get("exceptions", []),
            "tax_summary": tax_result,
            "forecast": forecast_result,
            "accuracy": accuracy,
        }

        _last_report = report

        yield {
            "type": "step",
            "agent": "orchestrator",
            "message": (
                f"Pipeline complete in {elapsed}s — "
                f"Match rate: {report['match_rate_pct']}% | "
                f"Accuracy: {accuracy.get('accuracy_pct', '?')}%"
            ),
        }

        yield {"type": "done", "agent": "orchestrator", "data": report}

    except Exception as exc:                                     # noqa: BLE001
        logger.exception("Orchestrator pipeline error: %s", exc)
        yield {"type": "error", "agent": "orchestrator", "message": str(exc)}
    finally:
        _run_in_progress = False
