"""
AI Finance Controller — FastAPI Backend
Phase 1: Synthetic data + schema + in-memory SQLite loaded on startup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from data import db as _db
from data.generator import get_batch
from data.schema import DataBatch


# ---------------------------------------------------------------------------
# Lifespan — load synthetic data into SQLite before first request
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Load the 60-row synthetic batch into in-memory SQLite on startup."""
    batch = get_batch()  # deterministic seed=42
    _db.init_db(batch)
    counts = _db.table_counts()
    print(
        "\n✅  In-memory SQLite loaded:\n"
        + "\n".join(f"   {t:30s}: {n:>3} rows" for t, n in counts.items())
        + "\n"
    )
    yield
    # No teardown needed — in-memory DB disappears with the process


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Finance Controller",
    description=(
        "Agentic finance-ops pipeline: Razorpay reconciliation, "
        "settlement Q&A, cash forecasting, and GST tax tagging."
    ),
    version="0.2.0",
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
    """Returns service liveness status and table row counts."""
    try:
        counts = _db.table_counts()
        return {"status": "ok", "service": "ai-finance-controller", "db": counts}
    except RuntimeError:
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
    """Return all Razorpay payment records."""
    return _db.query("SELECT * FROM razorpay_payments ORDER BY captured_at DESC")


@app.get("/api/data/bank", tags=["Data"])
async def get_bank_txns() -> list[dict]:
    """Return all bank statement credit rows."""
    return _db.query("SELECT * FROM bank_statements ORDER BY value_date DESC")


@app.get("/api/data/ledger", tags=["Data"])
async def get_ledger_entries() -> list[dict]:
    """Return all ledger entries."""
    return _db.query("SELECT * FROM ledger_entries ORDER BY date DESC")


@app.get("/api/data/settlements", tags=["Data"])
async def get_settlements() -> list[dict]:
    """Return all Razorpay settlement summaries."""
    return _db.query("SELECT * FROM settlements ORDER BY settlement_date DESC")


@app.get("/api/data/summary", tags=["Data"])
async def get_data_summary() -> dict[str, Any]:
    """
    Return high-level batch statistics for the dashboard stats bar.
    """
    counts = _db.table_counts()
    error_dist = _db.query(
        "SELECT error_type, COUNT(*) AS cnt FROM razorpay_payments GROUP BY error_type"
    )
    total_volume = _db.query("SELECT SUM(amount) AS total FROM razorpay_payments")[0]["total"]
    pending_settlement = _db.query(
        "SELECT SUM(total_amount) AS pending FROM settlements WHERE status='pending'"
    )[0]["pending"]
    return {
        "row_counts": counts,
        "error_distribution": {row["error_type"]: row["cnt"] for row in error_dist},
        "total_volume_inr": round(total_volume or 0, 2),
        "pending_settlement_inr": round(pending_settlement or 0, 2),
    }


# ---------------------------------------------------------------------------
# Phase 2 placeholder routes
# ---------------------------------------------------------------------------

@app.post("/api/run", tags=["Pipeline"])
async def run_pipeline() -> dict[str, str]:
    """Phase 2: Trigger orchestrator — streams agent steps via SSE."""
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.get("/api/report", tags=["Pipeline"])
async def get_report() -> dict[str, str]:
    """Phase 2: Return final JSON report after pipeline run."""
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.get("/api/accuracy", tags=["Pipeline"])
async def get_accuracy() -> dict[str, str]:
    """Phase 2: Return confusion-matrix-style accuracy breakdown."""
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.post("/api/qa", tags=["Pipeline"])
async def settlement_qa() -> dict[str, str]:
    """Phase 2: Natural-language Q&A over reconciled data."""
    return {"detail": "Not implemented yet — coming in Phase 2."}
