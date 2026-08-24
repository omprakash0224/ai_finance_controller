"""
AI Finance Controller — FastAPI Backend
Phase 2: Agent Core — Reconciler, TaxMatcher, Forecaster, Settlement Q&A.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Load .env before any module that reads env vars (e.g. data.db)
load_dotenv()

from data import db as _db
from data.generator import get_batch
from data.schema import DataBatch


# ---------------------------------------------------------------------------
# Lifespan — connect to Neon, seed synthetic data, close pool on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    On startup  : connect to Neon PostgreSQL, create schema, seed 60-row batch.
    On shutdown : return all pool connections.
    """
    seed_flag = os.getenv("SEED_DB", "true").lower() != "false"
    batch = get_batch()  # deterministic seed=42
    _db.init_db(batch, seed=seed_flag)
    counts = _db.table_counts()
    print(
        "\n[OK]  Neon PostgreSQL ready:\n"
        + "\n".join(f"   {t:30s}: {n:>3} rows" for t, n in counts.items())
        + "\n"
    )
    yield
    # Shutdown -- return all pooled connections
    _db.close_pool()
    print("[--] Neon connection pool closed.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Finance Controller",
    description=(
        "Agentic finance-ops pipeline: Razorpay reconciliation, "
        "settlement Q&A, cash forecasting, and GST tax tagging."
    ),
    version="0.4.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow Vite dev server (5173) and any localhost origin
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Meta"])
async def health() -> dict[str, Any]:
    """Returns service liveness status and Neon table row counts."""
    try:
        counts = _db.table_counts()
        from agents.orchestrator import is_run_in_progress
        return {
            "status": "ok",
            "service": "ai-finance-controller",
            "version": "0.4.0",
            "db_backend": "neon_postgresql",
            "db": counts,
            "pipeline_running": is_run_in_progress(),
        }
    except (RuntimeError, Exception):
        return {"status": "starting", "service": "ai-finance-controller"}


# ---------------------------------------------------------------------------
# Phase 1 — Data endpoints
# ---------------------------------------------------------------------------

@app.get("/api/data", tags=["Data"], response_model=DataBatch)
async def get_data() -> DataBatch:
    """
    Return the full raw synthetic batch as JSON.
    Includes all payments, bank transactions, ledger entries, and settlements.
    """
    return get_batch()


@app.get("/api/data/payments", tags=["Data"])
async def get_payments() -> list[dict]:
    """Return all Razorpay payment records ordered by capture date (newest first)."""
    return _db.query("SELECT * FROM razorpay_payments ORDER BY captured_at DESC")


@app.get("/api/data/bank", tags=["Data"])
async def get_bank_txns() -> list[dict]:
    """Return all bank statement credit rows ordered by value date (newest first)."""
    return _db.query("SELECT * FROM bank_statements ORDER BY value_date DESC")


@app.get("/api/data/ledger", tags=["Data"])
async def get_ledger_entries() -> list[dict]:
    """Return all ledger entries ordered by accounting date (newest first)."""
    return _db.query("SELECT * FROM ledger_entries ORDER BY date DESC")


@app.get("/api/data/settlements", tags=["Data"])
async def get_settlements() -> list[dict]:
    """Return all Razorpay settlement summaries ordered by settlement date."""
    return _db.query("SELECT * FROM settlements ORDER BY settlement_date DESC")


@app.get("/api/data/summary", tags=["Data"])
async def get_data_summary() -> dict[str, Any]:
    """Return high-level batch statistics for the dashboard stats bar."""
    counts = _db.table_counts()
    error_dist = _db.query(
        "SELECT error_type, COUNT(*) AS cnt FROM razorpay_payments GROUP BY error_type"
    )
    total_row = _db.query("SELECT SUM(amount) AS total FROM razorpay_payments")
    pending_row = _db.query(
        "SELECT SUM(total_amount) AS pending FROM settlements WHERE status = %s",
        ("pending",),
    )
    total_volume = total_row[0]["total"] if total_row else 0
    pending_settlement = pending_row[0]["pending"] if pending_row else 0
    return {
        "row_counts": counts,
        "error_distribution": {row["error_type"]: row["cnt"] for row in error_dist},
        "total_volume_inr": round(float(total_volume or 0), 2),
        "pending_settlement_inr": round(float(pending_settlement or 0), 2),
    }


# ---------------------------------------------------------------------------
# Phase 2 — Pipeline endpoints
# ---------------------------------------------------------------------------

async def _sse_generator(pipeline_gen: AsyncGenerator[dict, None]) -> AsyncGenerator[str, None]:
    """
    Convert pipeline event dicts to SSE-formatted text/event-stream strings.

    SSE format:
        data: <json>\n\n
    """
    try:
        async for event in pipeline_gen:
            yield f"data: {json.dumps(event)}\n\n"
        # Signal stream end
        yield "data: {\"type\": \"stream_end\"}\n\n"
    except asyncio.CancelledError:
        pass
    except Exception as exc:                                     # noqa: BLE001
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"


@app.post("/api/run", tags=["Pipeline"])
async def run_pipeline():
    """
    Trigger the AI finance orchestrator pipeline.

    Streams Server-Sent Events (text/event-stream) showing:
    - Reconciler agent steps and tool calls
    - Tax Matcher tagging progress
    - Forecaster projection steps
    - Final consolidated report

    The pipeline runs: Reconciler → TaxMatcher → Forecaster
    Results are stored and available via GET /api/report afterwards.
    """
    from agents.orchestrator import run_pipeline as _run_pipeline, is_run_in_progress

    if is_run_in_progress():
        raise HTTPException(
            status_code=409,
            detail="A pipeline run is already in progress. Check /api/report when done.",
        )

    return StreamingResponse(
        _sse_generator(_run_pipeline()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


@app.get("/api/report", tags=["Pipeline"])
async def get_report() -> dict[str, Any]:
    """
    Return the final JSON report from the most recent pipeline run.

    Includes:
    - match_rate, matched_count, exception_count
    - matched[] — list of matched pay_ids with match_type and confidence
    - exceptions[] — list of exceptions with reason and suggested_action
    - tax_summary — GST code breakdown
    - forecast — 30-day cash projection
    - accuracy — confusion-matrix-style breakdown vs ground truth
    """
    from agents.orchestrator import get_last_report

    report = get_last_report()
    if not report:
        # Return a pre-run summary from the database if available
        match_rows = _db.query("SELECT COUNT(*) AS n FROM match_results WHERE status = 'matched'")
        exc_rows = _db.query("SELECT COUNT(*) AS n FROM match_results WHERE status = 'exception'")
        matched_n = match_rows[0]["n"] if match_rows else 0
        exc_n = exc_rows[0]["n"] if exc_rows else 0

        if matched_n + exc_n == 0:
            return {
                "status": "not_run",
                "message": "No pipeline run found. POST /api/run to start the pipeline.",
            }

        total = matched_n + exc_n
        return {
            "status": "partial",
            "message": "Pipeline was run previously (data in DB). POST /api/run to refresh.",
            "matched_count": matched_n,
            "exception_count": exc_n,
            "match_rate_pct": round(matched_n / total * 100, 1) if total > 0 else 0,
        }

    return report


@app.get("/api/accuracy", tags=["Pipeline"])
async def get_accuracy() -> dict[str, Any]:
    """
    Return confusion-matrix-style accuracy breakdown.

    Compares agent match decisions against ground-truth error_type labels
    baked into the synthetic data generator.

    Columns:
    - true_positives: correctly matched / correctly excepted
    - false_positives: wrong decision vs ground truth
    - accuracy_pct: overall accuracy percentage
    - by_error_type: breakdown per generator error type
    """
    from agents.orchestrator import _compute_accuracy

    rows = _db.query("SELECT * FROM match_results")
    if not rows:
        return {
            "status": "not_run",
            "message": "No match results found. POST /api/run to start the pipeline.",
        }

    accuracy = _compute_accuracy({})
    return accuracy


@app.get("/api/match-results", tags=["Pipeline"])
async def get_match_results() -> list[dict]:
    """Return all match results from the most recent reconciliation run."""
    return _db.query(
        """
        SELECT
            mr.pay_id,
            mr.entry_id,
            mr.txn_id,
            mr.match_type,
            mr.confidence,
            mr.delta,
            mr.status,
            mr.ground_truth_error_type,
            p.settlement_id,
            p.settlement_utr,
            p.net_amount,
            p.settlement_date,
            p.method,
            p.amount
        FROM match_results mr
        JOIN razorpay_payments p ON p.pay_id = mr.pay_id
        ORDER BY mr.status, mr.match_type, mr.pay_id
        """
    )


@app.get("/api/exceptions", tags=["Pipeline"])
async def get_exceptions() -> list[dict]:
    """Return all exception records from the most recent reconciliation run."""
    return _db.query(
        """
        SELECT e.*, p.net_amount, p.settlement_id, p.settlement_utr, p.method
        FROM exceptions e
        JOIN razorpay_payments p ON p.pay_id = e.record_id
        ORDER BY e.exception_id
        """
    )


# ---------------------------------------------------------------------------
# Phase 2 — Q&A endpoint
# ---------------------------------------------------------------------------

class QARequest(BaseModel):
    question: str


@app.post("/api/qa", tags=["Pipeline"])
async def settlement_qa(body: QARequest) -> dict[str, Any]:
    """
    Natural-language Q&A over reconciled settlement data.

    The agent translates the question to SQL, executes it against Neon
    PostgreSQL, and returns a structured answer with supporting data rows.

    Example questions:
    - "How much is pending settlement?"
    - "Which payments were not reconciled?"
    - "What is the total GST collected on UPI transactions?"
    - "Show me all T+2 settlements"
    """
    from agents.settlement_qa import answer_question

    result = {}
    async for event in answer_question(body.question):
        if event.get("type") == "result":
            result = event.get("data", {})
        elif event.get("type") == "error":
            raise HTTPException(status_code=500, detail=event.get("message", "Q&A error"))
    return result
