"""
AI Finance Controller — FastAPI Backend
Phase 0: Skeleton with /health endpoint and CORS for local dev.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="AI Finance Controller",
    description="Agentic finance-ops pipeline: reconciliation, settlement Q&A, cash forecasting, tax tagging.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# CORS — allow Vite dev server (port 5173) and any localhost origin
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
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Meta"])
async def health():
    """Returns service liveness status."""
    return {"status": "ok", "service": "ai-finance-controller"}


# ---------------------------------------------------------------------------
# Placeholder routes — implemented in later phases
# ---------------------------------------------------------------------------
@app.post("/api/run", tags=["Pipeline"])
async def run_pipeline():
    """
    Phase 2: Trigger orchestrator, stream agent steps via SSE.
    Not yet implemented.
    """
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.get("/api/report", tags=["Pipeline"])
async def get_report():
    """
    Phase 2: Return final JSON report after pipeline run.
    Not yet implemented.
    """
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.get("/api/data", tags=["Data"])
async def get_data():
    """
    Phase 1: Return raw synthetic batch as JSON for UI preview.
    Not yet implemented.
    """
    return {"detail": "Not implemented yet — coming in Phase 1."}


@app.get("/api/accuracy", tags=["Pipeline"])
async def get_accuracy():
    """
    Phase 2: Return confusion-matrix-style accuracy breakdown.
    Not yet implemented.
    """
    return {"detail": "Not implemented yet — coming in Phase 2."}


@app.post("/api/qa", tags=["Pipeline"])
async def settlement_qa():
    """
    Phase 2: Natural-language Q&A over reconciled data.
    Not yet implemented.
    """
    return {"detail": "Not implemented yet — coming in Phase 2."}
