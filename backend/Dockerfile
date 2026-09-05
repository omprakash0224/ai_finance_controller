# ============================================================
# AI Finance Controller — Backend Dockerfile
# ============================================================
# Builds a single image used by three services in docker-compose:
#   - backend   → uvicorn (FastAPI)
#   - worker    → celery worker
#   - flower    → celery monitoring UI
# ============================================================

FROM python:3.12-slim AS base

# System dependencies required by psycopg2-binary and other C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install Python dependencies ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Copy application source ───────────────────────────────────────────────────
COPY . .

# ── Metadata ─────────────────────────────────────────────────────────────────
EXPOSE 8000

# Default command: run the FastAPI server
# Override in docker-compose for worker / flower services
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
