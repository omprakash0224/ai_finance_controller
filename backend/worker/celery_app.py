"""
backend/worker/celery_app.py
=============================
Celery application factory for the AI Finance Controller.

Architecture
------------
Celery is used exclusively for BACKGROUND BATCH PROCESSING of large payment
datasets.  The synchronous pipeline (POST /api/run) continues to work for
small batches and real-time use.

For batches of 100k–1,000,000 payments, the background pipeline:
  1. FastAPI receives POST /api/run/async and immediately returns a job_id.
  2. Celery dispatches a `run_batch_pipeline` task to the Redis queue.
  3. The task splits the batch into N_CHUNK chunks.
  4. A Celery chord fans out N_CHUNK `process_chunk` tasks in parallel.
  5. A callback task (`finalize_pipeline`) assembles the final report when
     all chunks are done.
  6. Progress is written to Redis (job_id → status JSON) so the frontend
     can poll GET /api/jobs/{job_id} for live updates.

Broker & Backend
----------------
Both the task queue (broker) and result backend use the same Redis instance.

For Upstash:
  - Go to https://console.upstash.com → your Redis database
  - Copy the "Redis CLI" connection string: rediss://:<token>@<host>:6379
  - Set CELERY_BROKER_URL=rediss://:<token>@<host>:6379/0
  - Set CELERY_RESULT_BACKEND=rediss://:<token>@<host>:6379/1

Or use a local Redis for development:
  - Set CELERY_BROKER_URL=redis://localhost:6379/0
  - Set CELERY_RESULT_BACKEND=redis://localhost:6379/1

Running the worker
------------------
  cd backend
  celery -A worker.celery_app worker --loglevel=info --concurrency=4

Environment Variables
---------------------
  CELERY_BROKER_URL       — Redis connection string for task queue
  CELERY_RESULT_BACKEND   — Redis connection string for results store
  CELERY_CHUNK_SIZE       — Payments per chunk (default 5000)
  CELERY_MAX_CONCURRENCY  — Max parallel chunk tasks (default 8)
"""

from __future__ import annotations

import os

from celery import Celery

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_BROKER  = "redis://localhost:6379/0"
_DEFAULT_BACKEND = "redis://localhost:6379/1"


def _broker_url() -> str:
    return os.getenv("CELERY_BROKER_URL", _DEFAULT_BROKER)


def _backend_url() -> str:
    return os.getenv("CELERY_RESULT_BACKEND", _DEFAULT_BACKEND)


def get_chunk_size() -> int:
    """Number of payments to include in each parallel chunk."""
    return int(os.getenv("CELERY_CHUNK_SIZE", "5000"))


def get_max_concurrency() -> int:
    """Maximum number of parallel chunk-processing Celery tasks."""
    return int(os.getenv("CELERY_MAX_CONCURRENCY", "8"))


# ---------------------------------------------------------------------------
# Celery application — configured lazily so FastAPI can import this module
# without a running broker (broker connection happens only when tasks run)
# ---------------------------------------------------------------------------

celery_app = Celery(
    "ai_finance_controller",
    broker=_broker_url(),
    backend=_backend_url(),
    include=[
        "worker.tasks",       # batch ingestion & reconciliation tasks
    ],
)

celery_app.conf.update(
    # -----------------------------------------------------------------------
    # Serialisation — JSON everywhere for interoperability
    # -----------------------------------------------------------------------
    task_serializer          = "json",
    result_serializer        = "json",
    accept_content           = ["json"],
    result_expires           = 3600,          # keep results for 1 hour

    # -----------------------------------------------------------------------
    # Routing — all tasks go to the default queue unless overridden
    # -----------------------------------------------------------------------
    task_default_queue       = "finance_batch",
    task_routes              = {
        "worker.tasks.process_chunk":       {"queue": "finance_batch"},
        "worker.tasks.finalize_pipeline":   {"queue": "finance_batch"},
        "worker.tasks.run_batch_pipeline":  {"queue": "finance_batch"},
    },

    # -----------------------------------------------------------------------
    # Reliability
    # -----------------------------------------------------------------------
    task_acks_late                    = True,   # ack after completion, not on receive
    task_reject_on_worker_lost        = True,   # re-queue if worker dies mid-task
    worker_prefetch_multiplier        = 1,      # one task at a time per worker process

    # -----------------------------------------------------------------------
    # Timeouts — generous for large batches
    # -----------------------------------------------------------------------
    task_soft_time_limit     = 1800,    # 30 min soft limit — raises SoftTimeLimitExceeded
    task_time_limit          = 2100,    # 35 min hard kill

    # -----------------------------------------------------------------------
    # Upstash Redis SSL (set automatically if broker URL starts with rediss://)
    # -----------------------------------------------------------------------
    broker_use_ssl           = _broker_url().startswith("rediss://"),
    redis_backend_use_ssl    = _backend_url().startswith("rediss://"),
)
