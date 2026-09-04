# FinClear AI

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![React 18](https://img.shields.io/badge/React-18-61DAFB.svg)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-5.0+-646CFF.svg)](https://vitejs.dev/)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-2.7.0-4285F4.svg)](https://github.com/google/adk)
[![Gemini](https://img.shields.io/badge/Gemini-2.5%20Flash%20%7C%203.5%20Flash%20Lite-orange.svg)](https://deepmind.google/technologies/gemini/)
[![PostgreSQL](https://img.shields.io/badge/Neon-PostgreSQL-336791.svg)](https://neon.tech/)
[![Redis](https://img.shields.io/badge/Upstash-Redis-FF4438.svg)](https://upstash.com/)
[![Celery](https://img.shields.io/badge/Celery-5.4+-37814A.svg)](https://docs.celeryq.dev/)

An autonomous, enterprise-grade **agentic finance-operations pipeline** that closes the loop across multi-source financial records: matching payment gateway settlements (Razorpay), bank statement credits, and internal accounting ledgers.

The platform provides deterministic verification, AI-powered root cause clustering, automated GST tax code tagging, 30-day cash flow forecasting with T+0/T+1/T+2 settlement cycle awareness, three-tier cached natural-language settlement Q&A, and an interactive real-time React dashboard.

---

## Table of Contents

- [Overview & Problem Statement](#overview--problem-statement)
- [Key Features](#key-features)
- [System Architecture](#system-architecture)
- [Reconciliation & Processing Engine](#reconciliation--processing-engine)
- [Three-Tier Q&A Architecture](#three-tier-qa-architecture)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
  - [Automated Boot (Windows PowerShell)](#automated-boot-windows-powershell)
  - [Manual Setup](#manual-setup)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Testing](#testing)
- [Scaling to Million Records & Cost Optimization](#scaling-to-million-records--cost-optimization)
- [Architecture & Design Decisions (ARCHITECTURE.md)](ARCHITECTURE.md)
- [License](#license)

---

## Overview & Problem Statement

In enterprise finance operations, **verification capacity, not generation speed**, is the critical bottleneck. Reconciliation, settlement clearing, and cash forecasting are traditionally manual, labor-intensive processes performed in spreadsheets.

**FinClear AI** solves this by combining:
1. **Deterministic SQL Speed**: Set-based relational matching handles high-volume transactions in milliseconds inside PostgreSQL.
2. **Cost-Minimised Agentic Reasoning**: Google ADK (`2.7.0`) and Gemini LLMs reason about discrepancies, cluster systemic exceptions, and answer natural-language queries.
3. **Auditability & Honest Reporting**: No cherry-picked data. Every exception is surfaced with ground-truth verification, confidence scoring, agent reasoning, and recommended remediation actions.

---

## Key Features

- **Multi-Source Financial Ingestion**: Ingests and correlates Razorpay payments (`pay_...`), orders (`order_...`), settlement batches (`setl_...`), bank statement credits (with bank UTR references), and internal general ledger accounting entries.
- **Two-Layer Hybrid Reconciliation**:
  - *Layer 1 (Deterministic SQL)*: Bulk set-based priority matching (`utr_match` &rarr; `exact_match` &rarr; `fuzzy_match` &rarr; `split_match` &rarr; `flag_exception`) executing directly in PostgreSQL. Achieves &ge;75% match rates in seconds.
  - *Layer 2 (AI Root Cause Clustering)*: Instead of sending every exception to an LLM, exceptions are clustered by `(reason, method)`. A single Gemini prompt diagnoses root causes across entire clusters, reducing AI token costs by >99%.
- **Settlement Cycle Intelligence**: Models real-world Indian banking settlement cycles:
  - **T+0 Instant Settlements** (~10%)
  - **T+1 Standard Settlements** (~70%)
  - **T+2 Extended Settlements** (~20%)
  - Automatic exclusion of Sundays, 2nd/4th Saturdays, and Indian statutory banking holidays.
- **Three-Tier Settlement Q&A Engine**:
  - **Tier 1 (Redis Cache)**: <5 ms response, $0 AI cost on exact question hits.
  - **Tier 2 (SQL Fast-Path)**: <80 ms response, $0 AI cost on aggregate queries (match rates, GST summaries, pending settlements, error distributions).
  - **Tier 3 (Gemini 2.5/3.5 LLM)**: Deep reasoning for novel queries; automatically populates Tier 1 for future queries.
- **GST Tax Categorization**: Automated tagging of transactions to GST rates (0%, 5%, 12%, 18%, 28%) with confidence scoring and ambiguity escalation.
- **30-Day Forward Cash Forecaster**: Linear regression and moving-average cash projection incorporating matched inflows, pending T+1/T+2 settlements, and recurring outflows.
- **Real-Time React Dashboard**: Dark-mode dashboard with real-time Server-Sent Events (SSE) agent logs, sortable Match Table, Exception Inspector, Settlement Timeline, Recharts Cash Flow Projections, and an interactive Settlement Q&A chat assistant.
- **Asynchronous Batch Processing**: Built-in Celery + Redis integration supporting chunked parallel execution for enterprise-scale workloads (`POST /api/run/async`).

---

## System Architecture

```mermaid
graph TD
    subgraph CLIENT["1. Client Layer (React 18 + Vite)"]
        UI["Interactive Dashboard<br/><i>Match Table • Exception Inspector • 30-Day Forecast • SSE Activity Log • Q&A Chat</i>"]
    end

    subgraph API["2. Gateway Layer (FastAPI)"]
        GATEWAY["API Gateway & SSE Streamer<br/><code>/api/data</code> • <code>/api/run</code> (SSE) • <code>/api/run/async</code> • <code>/api/qa</code>"]
    end

    subgraph ORCH["3. Agent Core (Google ADK 2.7.0 + Gemini)"]
        direction LR
        ORCHESTRATOR["Orchestrator<br/><i>ReAct Coordinator</i>"]
        RECONCILER["Reconciler Agent<br/><i>Layer 1 SQL + Layer 2 AI</i>"]
        TAX["Tax Matcher<br/><i>GST Classification</i>"]
        FORECAST["Forecaster<br/><i>Cash Projection</i>"]
        QA["Settlement Q&A<br/><i>3-Tier NL-to-SQL</i>"]

        ORCHESTRATOR --> RECONCILER & TAX & FORECAST
    end

    subgraph CACHE_QUEUE["4. Cache & Async Processing"]
        REDIS["Upstash Redis<br/><i>Tier 1 Q&A Cache (under 5ms)</i>"]
        CELERY["Celery Workers<br/><i>Parallel Batch Chunking (finance_batch)</i>"]
    end

    subgraph DB["5. Storage Layer (Neon PostgreSQL)"]
        POSTGRES[("Neon Serverless PostgreSQL<br/><i>Payments • Bank Statements • Ledgers<br/>Match Results • Materialized Metric Views</i>")]
    end

    %% Key Workflows
    CLIENT <-->|"HTTP REST & Live SSE"| GATEWAY
    GATEWAY -->|"Sync Pipeline Run"| ORCHESTRATOR
    GATEWAY -->|"Async Batch Dispatch"| CELERY
    CELERY -->|"Execute Chunks"| RECONCILER
    
    GATEWAY <-->|"User Questions"| QA
    QA <-->|"Tier 1 Fast Lookup"| REDIS
    QA -->|"Tier 2 & 3 SQL Queries"| POSTGRES

    RECONCILER & TAX & FORECAST -->|"Bulk Set Operations & Views"| POSTGRES
```

---

## Reconciliation & Processing Engine

The reconciliation pipeline uses a deterministic cascade designed to handle millions of records efficiently:

```mermaid
flowchart TD
    A(["Incoming Payment Batch"]) --> B["Layer 1: Deterministic SQL Engine"]
    
    B --> C{"1. UTR Match"}
    C -->|"Match: settlement_utr = bank_ref"| M1["Flag as utr_match (High Confidence)"]
    C -->|"Unmatched"| D{"2. Exact Match"}
    
    D -->|"Match: net_amount + settlement_date"| M2["Flag as exact_match (100 pct Confidence)"]
    D -->|"Unmatched"| E{"3. Fuzzy Match"}
    
    E -->|"Delta: amount within 5 INR or date within 2 days"| M3["Flag as fuzzy_match (85-90 pct Confidence)"]
    E -->|"Unmatched"| F{"4. Split Match"}
    
    F -->|"1 Settlement ID sums to N Ledger Lines"| M4["Flag as split_match (92 pct Confidence)"]
    F -->|"Unmatched"| G["5. Unmatched Exceptions"]
    
    G --> H["Layer 2: Exception Fingerprint Clustering"]
    H --> I["SQL: GROUP BY reason, method"]
    I --> J["Single Gemini Call with Clustered Signatures"]
    J --> K["Attach Root Cause & Remediation to Exceptions"]
    
    M1 --> L["Tax Matcher Agent: GST Code Tagging"]
    M2 --> L
    M3 --> L
    M4 --> L
    L --> M["Forecaster Agent: 30-Day Cash Flow Projection"]
    M --> N(["Final Audited Report & Cache Warmup"])
```

### Match Strategy Breakdown

| Match Type | Condition / Algorithm | Confidence | Error Mode Handled |
|---|---|---|---|
| `utr_match` | `settlement_utr = bank_statements.bank_ref` | **99%** | Clean settlements via NEFT/RTGS/IMPS |
| `exact_match` | `net_amount = amount` AND `settlement_date = value_date` | **100%** | Standard 1:1 ledger and bank credits |
| `fuzzy_amount` | `\|net_amount - amount\| <= 5 INR` | **88%** | Fee rounding or gateway commission adjustments |
| `fuzzy_date` | `\|settlement_date - value_date\| <= 2 days` | **85%** | Bank clearing and holiday processing lag |
| `split_match` | `settlement_id` matches sum of N ledger entries | **92%** | Batch order disbursements & consolidated ledger entries |
| `unmatched` | No criteria met after cascade | **0%** | Dropped bank credits, chargebacks, customer refunds |

---

## Three-Tier Q&A Architecture

The `/api/qa` endpoint serves natural language queries against financial data using a tiered cost-reduction model:

```
                  User Natural-Language Question
                                │
                                ▼
                   ┌─────────────────────────┐
                   │ Tier 1: Upstash Redis   │ ──(Cache Hit: <5 ms, $0 AI cost)──► Return Response
                   └─────────────────────────┘
                                │ Cache Miss
                                ▼
                   ┌─────────────────────────┐
                   │ Tier 2: SQL Fast-Path   │ ──(Regex Match: <80 ms, $0 AI cost)──► Return Response
                   └─────────────────────────┘
                                │ Novel / Unrecognized
                                ▼
                   ┌─────────────────────────┐
                   │ Tier 3: Gemini ADK LLM  │ ──(Generate SQL, Execute, Cache)────► Return Response
                   └─────────────────────────┘
```

- **Tier 1 (Redis)**: Normalized MD5 question hashing with configurable TTL (default 300s).
- **Tier 2 (SQL Fast-Path)**: Instant regex routing for match rates, GST breakdowns, pending settlements, exception counts, and transaction volume.
- **Tier 3 (Gemini 2.5 Flash / 3.5 Flash Lite)**: ReAct SQL generation and execution for nuanced financial queries, with responses written back to Tier 1.

---

## Repository Structure

```
finclear-ai/
├── backend/
│   ├── agents/
│   │   ├── orchestrator.py     # Multi-agent orchestrator & SSE event stream
│   │   ├── reconciler.py       # Two-layer reconciliation & exception clustering
│   │   ├── settlement_qa.py    # Three-tier natural language Q&A engine
│   │   ├── tax_matcher.py      # GST classification & ambiguity detector
│   │   └── forecaster.py       # 30-day cash projection agent
│   ├── data/
│   │   ├── db.py               # Neon PostgreSQL connection pool & migrations
│   │   ├── generator.py        # Realistic Razorpay synthetic data generator
│   │   └── schema.py           # Pydantic v2 domain models
│   ├── tools/
│   │   ├── cache.py            # Upstash Redis client & cache management
│   │   ├── db_tools.py         # SQL query tools for agents
│   │   ├── metrics_views.py    # Fast-path pre-aggregated SQL views
│   │   ├── razorpay_tools.py   # Settlement cycle & Razorpay ID resolution
│   │   └── reconcile_tools.py  # Exact, fuzzy, UTR, and split match logic
│   ├── worker/
│   │   ├── celery_app.py       # Celery application & queue configuration
│   │   ├── chunker.py          # Batch chunking & parallelization
│   │   └── tasks.py            # Distributed background pipeline tasks
│   ├── tests/
│   │   ├── test_api.py         # FastAPI endpoint integration tests
│   │   ├── test_db.py          # PostgreSQL schema and query tests
│   │   ├── test_generator.py   # Synthetic data and settlement cycle tests
│   │   └── test_schema.py      # Pydantic validation tests
│   ├── main.py                 # FastAPI application, routes, and lifespan
│   └── requirements.txt        # Pinned Python dependencies
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── AgentLog.jsx           # Live SSE agent activity panel
│   │   │   ├── ExceptionList.jsx      # Exceptions with root cause analysis
│   │   │   ├── ForecastChart.jsx      # Recharts 30-day cash balance curve
│   │   │   ├── MatchTable.jsx         # Sortable, filterable match breakdown
│   │   │   ├── SettlementQA.jsx       # Interactive chat interface
│   │   │   └── SettlementTimeline.jsx # T+0/T+1/T+2 settlement schedule
│   │   ├── App.jsx             # Main dashboard layout & state management
│   │   ├── App.css             # Component layout styles
│   │   ├── index.css           # Design tokens, typography, dark theme
│   │   └── main.jsx            # React root entry point
│   ├── package.json            # Frontend dependencies (React 18, Recharts, Lucide)
│   └── vite.config.js          # Vite configuration with /api proxy
├── extras/
│   └── scaling_and_cost_optimization.md # Guide for 1M+ records & enterprise setup
├── .env.example                # Template for environment configuration
├── PLAN.md                     # Comprehensive technical implementation blueprint
├── start.ps1                   # Single-command startup script (PowerShell)
└── README.md                   # Project documentation
```

---

## Prerequisites

- **Python**: `3.11` or later (`google-adk 2.x` requirement)
- **Node.js**: `18.x` or later (for Vite and React 18)
- **Database**: Neon PostgreSQL connection string (or standard PostgreSQL 15+)
- **Redis**: Upstash Redis REST credentials or local Redis server
- **API Keys**: Google Gemini API key (`GOOGLE_API_KEY`)

---

## Quick Start

### Automated Boot (Windows PowerShell)

Ensure `.env` exists in `backend/` (or copy `.env.example` to `backend/.env` and add your `DATABASE_URL` and `GOOGLE_API_KEY`).

Run the automated boot script from the project root:

```powershell
.\start.ps1
```

This script will:
1. Validate required environment configurations.
2. Create and activate `backend/.venv` (if missing).
3. Install backend Python dependencies via `pip`.
4. Launch the FastAPI server at `http://localhost:8000` in a dedicated terminal.
5. Install frontend npm dependencies and start the Vite dev server at `http://localhost:5173`.
6. Open your default browser to `http://localhost:5173`.

---

### Manual Setup

#### 1. Configure Environment Variables

Copy `.env.example` to `backend/.env`:

```bash
cp .env.example backend/.env
```

Edit `backend/.env` with your credentials:
```env
GOOGLE_API_KEY=AIzaSy...
DATABASE_URL=postgresql://user:password@ep-xyz.region.aws.neon.tech/dbname?sslmode=require
SEED_DB=true
```

#### 2. Backend Setup

```bash
cd backend

# Create virtual environment (Python 3.11+)
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run FastAPI server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The API docs (Swagger UI) will be accessible at `http://localhost:8000/docs`.

#### 3. Frontend Setup

In a separate terminal:

```bash
cd frontend

# Install packages
npm install

# Start Vite dev server
npm run dev
```

The React dashboard will be accessible at `http://localhost:5173`.

#### 4. Background Workers (Optional — for Celery Async Runs)

If you plan to process batches asynchronously with Celery:

```bash
cd backend
celery -A worker.celery_app worker --loglevel=info -Q finance_batch
```

To run the Flower monitoring UI (optional):
```bash
celery -A worker.celery_app flower --port=5555
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | **Yes** | — | PostgreSQL connection string (Neon serverless supported) |
| `GOOGLE_API_KEY` | **Yes** | — | API key for Gemini 2.5 Flash / 3.5 Flash Lite |
| `SEED_DB` | No | `true` | When `true`, automatically creates schema and seeds synthetic records on startup |
| `RAZORPAY_KEY_ID` | No | — | Razorpay API key identifier |
| `RAZORPAY_KEY_SECRET` | No | — | Razorpay API secret key |
| `UPSTASH_REDIS_URL` | No | — | Upstash Redis REST URL for Tier 1 Q&A caching |
| `UPSTASH_REDIS_TOKEN` | No | — | Upstash Redis REST token |
| `QA_CACHE_TTL_SECONDS` | No | `300` | TTL in seconds for Q&A answers in Redis |
| `METRICS_CACHE_TTL_SECONDS`| No | `60` | TTL in seconds for pre-computed metric snapshots |
| `CELERY_BROKER_URL` | No | `redis://localhost:6379/0` | Broker URL for asynchronous Celery jobs |
| `CELERY_RESULT_BACKEND` | No | `redis://localhost:6379/1` | Celery task result backend |
| `CELERY_CHUNK_SIZE` | No | `5000` | Payment records per parallel worker chunk |
| `CELERY_MAX_CONCURRENCY`| No | `8` | Maximum parallel worker chunk tasks |

---

## API Reference

### Health & Metadata

- `GET /health`: Service liveness, PostgreSQL table counts, cache status, and Celery broker connection.

### Data Endpoints

- `GET /api/data`: Returns the complete raw synthetic batch as JSON.
- `GET /api/data/payments`: All Razorpay payment records ordered by `captured_at DESC`.
- `GET /api/data/bank`: Bank statement credits ordered by `value_date DESC`.
- `GET /api/data/ledger`: Internal accounting ledger entries ordered by `date DESC`.
- `GET /api/data/settlements`: Razorpay settlement summaries.
- `GET /api/data/summary`: Batch metrics (total volume INR, pending settlement INR, error distribution).

### Pipeline & Reconciliation

- `POST /api/run`: Triggers the synchronous pipeline (Reconciler &rarr; Tax Matcher &rarr; Forecaster). Streams Server-Sent Events (`text/event-stream`).
- `GET /api/report`: Returns the consolidated report from the latest run (match rate, tax summary, 30-day forecast, exceptions).
- `GET /api/accuracy`: Confusion matrix comparing agent decisions against generator ground-truth labels.
- `GET /api/match-results`: List of all matched records with match type, confidence, and UTR linkage.
- `GET /api/exceptions`: List of all flagged exceptions with agent reasoning and recommended actions.

### Settlement Q&A & Cache

- `POST /api/qa`: Natural-language question answering (`{ "question": "What is pending settlement?" }`).
- `GET /api/qa/cache/status`: Returns Upstash Redis cache availability, token validation, and TTL configs.
- `POST /api/qa/cache/warm`: Pre-computes and caches common financial aggregates.
- `POST /api/qa/cache/flush`: Invalidates and purges all cached Q&A responses.

### Background Batch Jobs (Celery)

- `POST /api/run/async`: Dispatches chunked parallel reconciliation to Celery workers. Returns `job_id`.
- `GET /api/jobs/{job_id}`: Polls progress (`state`, `progress_pct`, `chunks_done`, `total_chunks`, `report`).
- `GET /api/jobs`: Lists the 100 most recent background jobs.

---

## Testing

The test suite covers unit logic, synthetic data generation, settlement date business rules, schema validation, and FastAPI integration.

Run the test suite using `pytest`:

```bash
cd backend

# Run all tests
pytest -v

# Run generator unit tests (no database required)
pytest tests/test_generator.py -v

# Run schema validation tests
pytest tests/test_schema.py -v

# Run database and API integration tests (requires DATABASE_URL in .env)
pytest tests/test_db.py tests/test_api.py -v
```

---

## Scaling to Million Records & Cost Optimization

For processing batches of 1,000,000+ records and minimizing LLM expenses:

1. **Deterministic SQL Core**: The Layer 1 engine runs inside PostgreSQL using multi-column indices (`(settlement_utr)`, `(net_amount, settlement_date)`), processing 1M records in <5 seconds.
2. **Cluster-Based LLM Diagnostics**: O(exceptions) LLM calls are compressed to O(clusters) via `GROUP BY reason, method`, keeping token usage under 5,000 tokens even on huge batches.
3. **Partitioning & Unlogged Staging**: Use PostgreSQL table partitioning by month and unlogged staging tables for fast data ingestion.
4. **Architecture Decisions**: Refer to [ARCHITECTURE.md](ARCHITECTURE.md) for full Architecture Decision Records (ADRs), flow diagrams, and detailed breakdowns of scaling, effectiveness, and costing impacts.

---

## License

This project is licensed under the MIT License.
