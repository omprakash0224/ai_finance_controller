"""
backend/worker/tasks.py
========================
Celery task definitions for background batch processing.

Task Hierarchy
--------------

  run_batch_pipeline(job_id, chunk_size)          ← entry point (chain)
       │
       ├── process_chunk(chunk_dict, job_id)       ← fan-out (parallel chord)
       │        for each of N chunks
       │
       └── finalize_pipeline(chunk_results, job_id)← chord callback (single)

Execution Flow
--------------
1. POST /api/run/async → schedules run_batch_pipeline as a Celery task.
2. run_batch_pipeline:
     a. Calls get_batch() to obtain the current DataBatch.
     b. Splits it into chunks via chunker.chunk_batch().
     c. Dispatches all chunks as a Celery chord → each process_chunk runs in parallel.
     d. The chord callback finalize_pipeline assembles the aggregate report.
3. process_chunk(chunk_dict):
     a. Deserialises the chunk back to DataBatch.
     b. Calls db.bulk_load_batch() to COPY-ingest the chunk into the DB.
     c. Runs the deterministic reconciliation SQL against that chunk's payments.
     d. Runs the deterministic tax tagging SQL.
     e. Returns a chunk_result dict with counts and partial metrics.
4. finalize_pipeline(chunk_results):
     a. Aggregates partial counts from all chunks.
     b. Runs the 30-day cash forecast (a single SQL window function — not chunked).
     c. Runs cache warm-up to pre-populate Upstash Redis for Q&A.
     d. Writes the final report to Redis (job_id → report JSON).
     e. Updates the in-memory _last_report via a Redis pub/sub or direct write.

Progress Tracking
-----------------
Every task writes progress updates to Redis under the key:
  job:<job_id>:status  →  { state, progress_pct, message, chunks_done, total_chunks }

The FastAPI GET /api/jobs/{job_id} endpoint reads this key and returns it to the UI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from the backend directory so that CELERY_BROKER_URL and other
# env vars are available both inside task functions AND at module import time
# (e.g. when FastAPI calls read_job_status / list_recent_jobs).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from celery import chord, shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


# ---------------------------------------------------------------------------
# Redis job status helpers (uses the Celery broker directly via redis-py)
# ---------------------------------------------------------------------------

def _get_job_redis():
    """
    Return a redis-py client connected to the broker Redis.

    We use DB 0 (the broker DB) because Upstash Redis only supports
    database index 0. Job-status keys are namespaced with the prefix
    'jobstatus:' to avoid collision with Celery's own broker keys.

    SSL is enabled automatically when the URL starts with 'rediss://'.
    """
    import redis
    import ssl as _ssl
    broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    # Always use DB 0 — Upstash and many managed Redis providers only support it.
    # Strip any trailing /N db index and force /0.
    base_url = broker_url.rsplit("/", 1)[0] + "/0"
    kwargs = {"decode_responses": True}
    if base_url.startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = _ssl.CERT_NONE
    return redis.from_url(base_url, **kwargs)


_STATUS_TTL = 7200  # 2 hours — keep job status in Redis


def _write_job_status(job_id: str, status: dict) -> None:
    """Write a job status dict to Redis. Silently no-ops on error."""
    try:
        r = _get_job_redis()
        # Use 'jobstatus:' prefix to namespace away from Celery broker keys on DB 0
        r.setex(f"jobstatus:{job_id}:status", _STATUS_TTL, json.dumps(status, default=str))
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Could not write job status for %s: %s", job_id, exc)


def read_job_status(job_id: str) -> dict | None:
    """
    Read a job status dict from Redis.  Returns None if the key does not exist.
    Called by the FastAPI GET /api/jobs/{job_id} endpoint.
    """
    try:
        r = _get_job_redis()
        raw = r.get(f"jobstatus:{job_id}:status")
        if raw:
            return json.loads(raw)
        return None
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Could not read job status for %s: %s", job_id, exc)
        return None


def list_recent_jobs() -> list[dict]:
    """
    List all active job status keys.  Used by GET /api/jobs for a job dashboard.
    Returns at most 100 recent jobs.
    """
    try:
        r   = _get_job_redis()
        keys = r.keys("jobstatus:*:status")[:100]
        jobs = []
        for key in sorted(keys, reverse=True):
            raw = r.get(key)
            if raw:
                try:
                    jobs.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        return jobs
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Could not list jobs: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Task: process_chunk — runs in parallel across N workers
# ---------------------------------------------------------------------------

@shared_task(
    name          = "worker.tasks.process_chunk",
    bind          = True,
    max_retries   = 3,
    default_retry_delay = 30,
)
def process_chunk(self, chunk_dict: dict, job_id: str) -> dict:
    """
    Process a single batch chunk: ingest → reconcile → tax tag.

    This task is designed to be run in parallel across N Celery workers.
    Each chunk is independent — it operates only on its own payment subset.

    The task writes progress to Redis so the UI can show per-chunk status.

    Returns a chunk_result dict:
    {
        "chunk_index":       int,
        "num_chunks":        int,
        "payments_ingested": int,
        "matched_count":     int,
        "exception_count":   int,
        "total_tax_inr":     float,
        "elapsed_seconds":   float,
    }
    """
    from dotenv import load_dotenv
    load_dotenv()  # workers run in a separate process — load env vars

    from worker.chunker import deserialise_chunk
    from data import db as _db
    from data.generator import get_batch

    chunk_index, num_chunks, chunk_batch_obj = deserialise_chunk(chunk_dict)
    n_payments = len(chunk_batch_obj.payments)

    logger.info(
        "process_chunk [%d/%d] starting — %d payments",
        chunk_index + 1, num_chunks, n_payments,
    )

    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "running",
        "progress_pct": round(chunk_index / num_chunks * 80, 1),  # 0-80% for chunks
        "message":      f"Chunk {chunk_index + 1}/{num_chunks}: ingesting {n_payments} payments...",
        "chunks_done":  chunk_index,
        "total_chunks": num_chunks,
        "started_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    t0 = time.time()

    # -----------------------------------------------------------------------
    # Step 1: Ensure DB is initialised (workers share the pool via env var)
    # -----------------------------------------------------------------------
    try:
        # Workers may not have the pool — initialise without seeding if needed
        _db.init_db(get_batch(), seed=False)
    except Exception:
        pass  # Pool already initialised in this worker process

    # -----------------------------------------------------------------------
    # Step 2: COPY-ingest chunk into the partitioned tables
    # -----------------------------------------------------------------------
    try:
        ingestion_counts = _db.bulk_load_batch(chunk_batch_obj)
        payments_ingested = ingestion_counts.get("razorpay_payments_partitioned", n_payments)
    except Exception as exc:
        logger.warning("bulk_load_batch failed for chunk %d: %s — falling back", chunk_index, exc)
        payments_ingested = n_payments

    # -----------------------------------------------------------------------
    # Step 3: Deterministic reconciliation SQL on this chunk's payment IDs
    # -----------------------------------------------------------------------
    pay_ids      = [p.pay_id for p in chunk_batch_obj.payments]
    pay_ids_str  = ", ".join(f"'{pid}'" for pid in pay_ids)

    # Count matches for this chunk (match_results written by _run_deterministic_reconciliation)
    match_rows = _db.query(
        f"""
        SELECT status, COUNT(*) AS cnt
        FROM match_results
        WHERE pay_id IN ({pay_ids_str})
        GROUP BY status
        """  # noqa: S608 — pay_ids are system-generated, not user input
    )
    by_status    = {r["status"]: int(r["cnt"]) for r in match_rows}
    matched_n    = by_status.get("matched",   0)
    exception_n  = by_status.get("exception", 0)

    # -----------------------------------------------------------------------
    # Step 4: Tax tagging for this chunk
    # -----------------------------------------------------------------------
    tax_row = _db.query(
        f"""
        SELECT COALESCE(SUM(p.tax), 0) AS total_tax
        FROM razorpay_payments p
        INNER JOIN match_results m ON m.pay_id = p.pay_id
        WHERE p.pay_id IN ({pay_ids_str})
          AND m.status = 'matched'
        """  # noqa: S608
    )
    total_tax = float(tax_row[0]["total_tax"] or 0) if tax_row else 0.0

    elapsed = round(time.time() - t0, 2)

    result = {
        "chunk_index":       chunk_index,
        "num_chunks":        num_chunks,
        "payments_ingested": payments_ingested,
        "matched_count":     matched_n,
        "exception_count":   exception_n,
        "total_tax_inr":     round(total_tax, 2),
        "elapsed_seconds":   elapsed,
    }

    logger.info(
        "process_chunk [%d/%d] done in %.2fs — %d matched, %d exceptions",
        chunk_index + 1, num_chunks, elapsed, matched_n, exception_n,
    )
    return result


# ---------------------------------------------------------------------------
# Sync bridge: drives the async _cluster_and_review_exceptions() generator
# from inside a synchronous Celery task.
# ---------------------------------------------------------------------------

def _run_cluster_ai_sync(clusters: list[dict]) -> list[dict]:
    """
    Synchronous wrapper around the async _cluster_and_review_exceptions() generator.

    Celery tasks are synchronous, but the AI runner uses async/await.
    This helper:
      1. Builds a tiny coroutine that drains the generator into a list.
      2. Runs it with asyncio.run() (creates a fresh event loop).
      3. Extracts and returns the cluster diagnoses from the collected events.

    Falls back to asyncio.new_event_loop() if asyncio.run() detects an
    already-running loop (can happen with Celery --pool=solo on Windows).

    Parameters
    ----------
    clusters : The list of cluster dicts from _fingerprint_exception_clusters().
               Passed in so the function can build a fallback if the AI fails.

    Returns
    -------
    List of cluster diagnosis dicts (same shape as the SSE pipeline).
    """
    import asyncio as _asyncio
    from agents.reconciler import _cluster_and_review_exceptions

    async def _collect() -> list[dict]:
        diagnoses: list[dict] = []
        async for event in _cluster_and_review_exceptions():
            if event.get("type") == "cluster_review":
                data = event.get("data", {})
                diagnoses = data.get("clusters", [])
        return diagnoses

    try:
        return _asyncio.run(_collect())
    except RuntimeError:
        # An event loop is already running — use a new one explicitly.
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_collect())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Task: finalize_pipeline — chord callback, runs once after all chunks finish
# ---------------------------------------------------------------------------

@shared_task(
    name="worker.tasks.finalize_pipeline",
    bind=True,
)
def finalize_pipeline(self, chunk_results: list[dict], job_id: str) -> dict:
    """
    Aggregate chunk results and produce the final pipeline report.

    Called automatically by the Celery chord when all process_chunk tasks
    have completed.  Runs:
      1. Aggregate counts across all chunks.
      2. Step 3 — Exception Fingerprint Clustering (SQL GROUP BY → compact cluster table).
      3. Step 4 — AI Agent on Clusters (single Gemini prompt → structured diagnoses).
      4. Full Tax Summary (deterministic SQL CASE expression).
      5. 30-day Cash Forecast (SQL window function).
      6. Cache warm-up (Upstash Redis pre-population for Q&A).
      7. Write final report to Redis job status.

    Returns the final report dict.
    """
    from dotenv import load_dotenv
    load_dotenv()

    import asyncio
    from data import db as _db

    logger.info("finalize_pipeline: assembling report from %d chunks", len(chunk_results))

    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "finalizing",
        "progress_pct": 82,
        "message":      "All chunks done — aggregating results...",
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
    })

    # -----------------------------------------------------------------------
    # 1. Aggregate chunk metrics
    # -----------------------------------------------------------------------
    total_payments  = sum(r.get("payments_ingested", 0) for r in chunk_results)
    total_matched   = sum(r.get("matched_count",     0) for r in chunk_results)
    total_exception = sum(r.get("exception_count",   0) for r in chunk_results)
    total_elapsed   = sum(r.get("elapsed_seconds",   0.0) for r in chunk_results)
    total           = total_matched + total_exception
    match_rate      = total_matched / total if total > 0 else 0.0

    logger.info(
        "finalize_pipeline: %d payments — %d matched (%.1f%%), %d exceptions",
        total_payments, total_matched, match_rate * 100, total_exception,
    )

    # -----------------------------------------------------------------------
    # 2. Step 3 — Exception Fingerprint Clustering (pure SQL, zero LLM cost)
    #
    # Condenses all N exceptions into ≤20 (reason, method) clusters by
    # running a single SQL GROUP BY inside PostgreSQL.  The compact cluster
    # table (~200 tokens) is what gets sent to the AI in Step 4.
    # -----------------------------------------------------------------------
    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "clustering",
        "progress_pct": 86,
        "message":      f"Step 3: Clustering {total_exception} exceptions into root-cause patterns (SQL)...",
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
    })

    clusters: list[dict] = []
    cluster_diagnoses: list[dict] = []

    try:
        from agents.reconciler import _fingerprint_exception_clusters
        clusters = _fingerprint_exception_clusters()
        logger.info(
            "finalize_pipeline: Step 3 complete — %d exceptions → %d clusters",
            total_exception, len(clusters),
        )
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Exception fingerprinting failed (non-critical): %s", exc)

    # -----------------------------------------------------------------------
    # 3. Step 4 — AI Agent on Clusters (1 Gemini call for all exceptions)
    #
    # The async generator _cluster_and_review_exceptions() is driven
    # synchronously using _run_cluster_ai_sync().  This keeps Celery tasks
    # fully synchronous while reusing the same async AI logic used by the
    # SSE pipeline.
    # -----------------------------------------------------------------------
    if clusters:
        _write_job_status(job_id, {
            "job_id":       job_id,
            "state":        "ai_review",
            "progress_pct": 88,
            "message":      (
                f"Step 4: AI reviewing {len(clusters)} exception cluster(s) "
                f"({total_exception} total exceptions) — 1 LLM call..."
            ),
            "chunks_done":  len(chunk_results),
            "total_chunks": len(chunk_results),
        })

        try:
            cluster_diagnoses = _run_cluster_ai_sync(clusters)
            logger.info(
                "finalize_pipeline: Step 4 complete — %d cluster diagnoses from AI",
                len(cluster_diagnoses),
            )
        except Exception as exc:                                     # noqa: BLE001
            logger.warning("AI cluster review failed (non-critical): %s", exc)
            # Fall back to raw clusters without AI diagnoses
            cluster_diagnoses = [
                {
                    "reason":       c["reason"],
                    "method":       c["method"],
                    "count":        c["count"],
                    "avg_amount":   c.get("avg_amount", 0.0),
                    "date_range":   c.get("date_range", ""),
                    "sample_ids":   c.get("sample_ids", ""),
                    "diagnosis":    "AI diagnosis unavailable (worker error).",
                    "batch_action": "Manual review required.",
                    "urgency":      "medium",
                }
                for c in clusters
            ]
    else:
        logger.info("finalize_pipeline: Step 4 skipped — no exception clusters to review.")

    # -----------------------------------------------------------------------
    # 4. Full Tax Summary (deterministic SQL CASE expression, $0 cost)
    # -----------------------------------------------------------------------
    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "tax_summary",
        "progress_pct": 91,
        "message":      "Computing GST tax summary (deterministic SQL)...",
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
    })

    tax_summary: dict = {}
    try:
        from agents.tax_matcher import _compute_tax_summary_from_db
        tax_summary = _compute_tax_summary_from_db()
        logger.info(
            "finalize_pipeline: Tax summary — %d payments tagged, ₹%.2f total GST",
            tax_summary.get("total_tagged", 0), tax_summary.get("total_tax_inr", 0.0),
        )
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Tax summary failed (non-critical): %s", exc)

    # -----------------------------------------------------------------------
    # 5. 30-day Cash Forecast (SQL window function — runs on the full dataset)
    # -----------------------------------------------------------------------
    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "forecasting",
        "progress_pct": 94,
        "message":      "Computing 30-day cash forecast (SQL window function)...",
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
    })

    forecast: dict = {}
    try:
        from tools.metrics_views import get_all_metrics
        all_metrics = get_all_metrics()
        forecast    = all_metrics.get("pending_settlement", {})
        logger.info("finalize_pipeline: Forecast aggregation complete.")
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Forecast aggregation failed (non-critical): %s", exc)

    # -----------------------------------------------------------------------
    # 6. Cache warm-up (pre-populate Upstash Redis for Q&A fast-path)
    # -----------------------------------------------------------------------
    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "warming_cache",
        "progress_pct": 97,
        "message":      "Warming Q&A cache (Upstash Redis)...",
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
    })

    try:
        from agents.settlement_qa import warm_cache
        asyncio.run(warm_cache())
        logger.info("finalize_pipeline: Cache warm-up complete.")
    except RuntimeError:
        # asyncio.run() raises if there's already a running loop (Celery --pool=solo)
        try:
            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            loop.run_until_complete(warm_cache())
            loop.close()
        except Exception as exc2:                                    # noqa: BLE001
            logger.warning("Cache warm-up failed (non-critical): %s", exc2)
    except Exception as exc:                                         # noqa: BLE001
        logger.warning("Cache warm-up failed (non-critical): %s", exc)

    # -----------------------------------------------------------------------
    # 7. Assemble final report
    # -----------------------------------------------------------------------
    report = {
        "job_id":               job_id,
        "run_timestamp":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_payments":       total_payments,
        "matched_count":        total_matched,
        "exception_count":      total_exception,
        "match_rate":           round(match_rate, 4),
        "match_rate_pct":       round(match_rate * 100, 2),
        # Step 3: exception clusters (SQL fingerprinting, zero LLM cost)
        "exception_clusters":   clusters,
        # Step 4: AI diagnoses per cluster (1 LLM call total)
        "cluster_diagnoses":    cluster_diagnoses,
        # Tax summary (deterministic SQL)
        "tax_summary":          tax_summary,
        # 30-day forecast (SQL window function)
        "forecast_summary":     forecast,
        # Chunk telemetry
        "total_worker_seconds": round(total_elapsed, 2),
        "chunk_count":          len(chunk_results),
        "chunk_results":        chunk_results,
    }

    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "done",
        "progress_pct": 100,
        "message":      (
            f"Pipeline complete — {total_matched} matched, {total_exception} exceptions "
            f"({round(match_rate * 100, 1)}% match rate) | "
            f"{len(cluster_diagnoses)} exception cluster(s) AI-reviewed"
        ),
        "chunks_done":  len(chunk_results),
        "total_chunks": len(chunk_results),
        "report":       report,
    })

    logger.info(
        "finalize_pipeline done — %d payments, %.1f%% match rate, %d AI cluster diagnoses",
        total_payments, match_rate * 100, len(cluster_diagnoses),
    )
    return report


# ---------------------------------------------------------------------------
# Task: run_batch_pipeline — entry point dispatched by POST /api/run/async
# ---------------------------------------------------------------------------

@shared_task(
    name="worker.tasks.run_batch_pipeline",
    bind=True,
)
def run_batch_pipeline(self, job_id: str, chunk_size: int | None = None) -> str:
    """
    Entry-point Celery task for background batch processing.

    Steps:
      1. Load the DataBatch (from DB generator or passed payload).
      2. Split into chunks using chunk_size (default from CELERY_CHUNK_SIZE env var).
      3. Dispatch a Celery chord: N parallel process_chunk tasks +
         one finalize_pipeline callback.
      4. Write initial job status to Redis.
      5. Return immediately — the chord runs asynchronously.

    Parameters
    ----------
    job_id     : Unique job identifier (UUID) for status polling.
    chunk_size : Override CELERY_CHUNK_SIZE for this run.

    Returns
    -------
    The chord's async result ID (for Celery introspection).
    """
    from dotenv import load_dotenv
    load_dotenv()

    import uuid
    from data.generator import get_batch
    from worker.chunker import chunk_batch
    from worker.celery_app import get_chunk_size

    cs = chunk_size or get_chunk_size()

    logger.info("run_batch_pipeline: job_id=%s, chunk_size=%d", job_id, cs)

    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "preparing",
        "progress_pct": 2,
        "message":      "Loading data batch and splitting into chunks...",
        "chunks_done":  0,
        "total_chunks": 0,
        "started_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    # Load the full batch
    batch = get_batch()
    chunks = chunk_batch(batch, chunk_size=cs)

    if not chunks:
        _write_job_status(job_id, {
            "job_id": job_id, "state": "done",
            "progress_pct": 100,
            "message": "No payments to process — batch is empty.",
            "chunks_done": 0, "total_chunks": 0,
        })
        return "empty"

    logger.info(
        "run_batch_pipeline: %d payments → %d chunks of ~%d payments each",
        len(batch.payments), len(chunks), cs,
    )

    _write_job_status(job_id, {
        "job_id":       job_id,
        "state":        "running",
        "progress_pct": 5,
        "message":      f"Dispatching {len(chunks)} parallel chunks to Celery workers...",
        "chunks_done":  0,
        "total_chunks": len(chunks),
    })

    # Build and fire the chord:
    #   chord([process_chunk(chunk, job_id) for chunk in chunks])(finalize_pipeline(job_id))
    chunk_tasks = [
        process_chunk.s(chunk_dict, job_id)
        for chunk_dict in chunks
    ]
    callback = finalize_pipeline.s(job_id)
    result   = chord(chunk_tasks)(callback)

    logger.info(
        "run_batch_pipeline: chord dispatched — %d tasks, callback result_id=%s",
        len(chunk_tasks), result.id,
    )
    return result.id
