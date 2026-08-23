"""
AI Finance Controller — FastAPI Backend
Phase 1 (Neon): Synthetic data + schema loaded into Neon PostgreSQL on startup.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
        "\n✅  Neon PostgreSQL ready:\n"
        + "\n".join(f"   {t:30s}: {n:>3} rows" for t, n in counts.items())
        + "\n"
    )
    yield
    # Shutdown — return all pooled connections
    _db.close_pool()
    print("🔌  Neon connection pool closed.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Finance Controller",
    description=(
        "Agentic finance-ops pipeline: Razorpay reconciliation, "
        "settlement Q&A, cash forecasting, and GST tax tagging."
    ),
    version="0.3.0",
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
        return {
            "status": "ok",
            "service": "ai-finance-controller",
            "db_backend": "neon_postgresql",
            "db": counts,
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
