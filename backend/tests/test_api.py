"""
tests/test_api.py
=================
Integration tests for the FastAPI endpoints defined in main.py.

Uses httpx + FastAPI's TestClient (ASGI).  The lifespan context manager runs,
connecting to Neon PostgreSQL and seeding the 60-row batch, so these tests
hit a fully initialised database.

These tests are SKIPPED automatically when DATABASE_URL is not set in the
environment.  To run them:

    $env:DATABASE_URL = "postgresql://user:pass@host/dbname?sslmode=require"
    .venv\\Scripts\\pytest tests/test_api.py -v

Covers
------
- GET  /health            — status=ok, db_backend=neon_postgresql, db counts present
- GET  /api/data          — full DataBatch JSON, correct counts
- GET  /api/data/payments — list of payment dicts
- GET  /api/data/bank     — list of bank txn dicts
- GET  /api/data/ledger   — list of ledger entry dicts
- GET  /api/data/settlements — list of settlement dicts
- GET  /api/data/summary  — stats summary structure
- POST /api/run           — Phase 2 placeholder (200 + detail key)
- GET  /api/report        — Phase 2 placeholder (200 + detail key)
- GET  /api/accuracy      — Phase 2 placeholder (200 + detail key)
- POST /api/qa            — Phase 2 placeholder (200 + detail key)
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from main import app
from data.generator import BATCH_SIZE
from data.schema import ErrorType

# ---------------------------------------------------------------------------
# Skip entire module if DATABASE_URL is not available
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping Neon API integration tests",
)


# ---------------------------------------------------------------------------
# Client fixture — lifespan runs once per module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_status_ok(self, client: TestClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "ai-finance-controller"

    def test_db_backend_is_neon(self, client: TestClient):
        resp = client.get("/health")
        data = resp.json()
        assert data.get("db_backend") == "neon_postgresql"

    def test_db_counts_present(self, client: TestClient):
        resp = client.get("/health")
        data = resp.json()
        assert "db" in data
        assert data["db"]["razorpay_payments"] == BATCH_SIZE


# ---------------------------------------------------------------------------
# GET /api/data  — full DataBatch
# ---------------------------------------------------------------------------

class TestGetData:
    def test_status_200(self, client: TestClient):
        resp = client.get("/api/data")
        assert resp.status_code == 200

    def test_has_payments_key(self, client: TestClient):
        resp = client.get("/api/data")
        data = resp.json()
        assert "payments" in data

    def test_payment_count(self, client: TestClient):
        resp = client.get("/api/data")
        data = resp.json()
        assert len(data["payments"]) == BATCH_SIZE

    def test_has_bank_txns_key(self, client: TestClient):
        resp = client.get("/api/data")
        data = resp.json()
        assert "bank_txns" in data

    def test_has_ledger_entries_key(self, client: TestClient):
        resp = client.get("/api/data")
        data = resp.json()
        assert "ledger_entries" in data

    def test_has_settlements_key(self, client: TestClient):
        resp = client.get("/api/data")
        data = resp.json()
        assert "settlements" in data

    def test_payment_fields_present(self, client: TestClient):
        resp = client.get("/api/data")
        payment = resp.json()["payments"][0]
        required_fields = {
            "pay_id", "order_id", "captured_at", "amount", "currency",
            "method", "status", "settlement_id", "settlement_date",
            "settlement_utr", "fee", "tax", "net_amount", "error_type",
        }
        assert required_fields.issubset(payment.keys())

    def test_bank_txn_fields_present(self, client: TestClient):
        resp = client.get("/api/data")
        txns = resp.json()["bank_txns"]
        assert len(txns) > 0
        txn = txns[0]
        assert {"txn_id", "value_date", "amount", "description", "bank_ref", "currency"}.issubset(txn.keys())

    def test_all_pay_ids_have_prefix(self, client: TestClient):
        resp = client.get("/api/data")
        for p in resp.json()["payments"]:
            assert p["pay_id"].startswith("pay_")

    def test_all_settlement_ids_have_prefix(self, client: TestClient):
        resp = client.get("/api/data")
        for p in resp.json()["payments"]:
            assert p["settlement_id"].startswith("setl_")

    def test_bank_txns_fewer_than_payments(self, client: TestClient):
        """Because no_bank_credit records have no bank row."""
        resp = client.get("/api/data")
        data = resp.json()
        assert len(data["bank_txns"]) < len(data["payments"])

    def test_net_amount_positive(self, client: TestClient):
        resp = client.get("/api/data")
        for p in resp.json()["payments"]:
            assert p["net_amount"] > 0

    def test_currency_inr(self, client: TestClient):
        resp = client.get("/api/data")
        for p in resp.json()["payments"]:
            assert p["currency"] == "INR"


# ---------------------------------------------------------------------------
# GET /api/data/payments
# ---------------------------------------------------------------------------

class TestGetPayments:
    def test_status_200(self, client: TestClient):
        assert client.get("/api/data/payments").status_code == 200

    def test_returns_list(self, client: TestClient):
        data = client.get("/api/data/payments").json()
        assert isinstance(data, list)

    def test_correct_count(self, client: TestClient):
        data = client.get("/api/data/payments").json()
        assert len(data) == BATCH_SIZE

    def test_sorted_by_captured_at_desc(self, client: TestClient):
        data = client.get("/api/data/payments").json()
        dates = [row["captured_at"] for row in data]
        assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# GET /api/data/bank
# ---------------------------------------------------------------------------

class TestGetBank:
    def test_status_200(self, client: TestClient):
        assert client.get("/api/data/bank").status_code == 200

    def test_returns_list(self, client: TestClient):
        data = client.get("/api/data/bank").json()
        assert isinstance(data, list)

    def test_all_txn_ids_prefixed(self, client: TestClient):
        data = client.get("/api/data/bank").json()
        for row in data:
            assert row["txn_id"].startswith("btxn_")


# ---------------------------------------------------------------------------
# GET /api/data/ledger
# ---------------------------------------------------------------------------

class TestGetLedger:
    def test_status_200(self, client: TestClient):
        assert client.get("/api/data/ledger").status_code == 200

    def test_returns_list(self, client: TestClient):
        data = client.get("/api/data/ledger").json()
        assert isinstance(data, list)

    def test_all_entry_ids_prefixed(self, client: TestClient):
        data = client.get("/api/data/ledger").json()
        for row in data:
            assert row["entry_id"].startswith("ent_")

    def test_internal_refs_start_with_setl(self, client: TestClient):
        data = client.get("/api/data/ledger").json()
        for row in data:
            assert row["internal_ref"].startswith("setl_")


# ---------------------------------------------------------------------------
# GET /api/data/settlements
# ---------------------------------------------------------------------------

class TestGetSettlements:
    def test_status_200(self, client: TestClient):
        assert client.get("/api/data/settlements").status_code == 200

    def test_returns_list(self, client: TestClient):
        data = client.get("/api/data/settlements").json()
        assert isinstance(data, list)

    def test_all_settlement_ids_prefixed(self, client: TestClient):
        data = client.get("/api/data/settlements").json()
        for row in data:
            assert row["settlement_id"].startswith("setl_")

    def test_status_values_valid(self, client: TestClient):
        valid = {"pending", "processed", "on_hold"}
        data = client.get("/api/data/settlements").json()
        for row in data:
            assert row["status"] in valid


# ---------------------------------------------------------------------------
# GET /api/data/summary
# ---------------------------------------------------------------------------

class TestGetDataSummary:
    def test_status_200(self, client: TestClient):
        assert client.get("/api/data/summary").status_code == 200

    def test_has_row_counts(self, client: TestClient):
        data = client.get("/api/data/summary").json()
        assert "row_counts" in data
        assert data["row_counts"]["razorpay_payments"] == BATCH_SIZE

    def test_has_error_distribution(self, client: TestClient):
        data = client.get("/api/data/summary").json()
        assert "error_distribution" in data
        dist = data["error_distribution"]
        assert "clean" in dist

    def test_has_total_volume(self, client: TestClient):
        data = client.get("/api/data/summary").json()
        assert "total_volume_inr" in data
        assert data["total_volume_inr"] > 0

    def test_has_pending_settlement(self, client: TestClient):
        data = client.get("/api/data/summary").json()
        assert "pending_settlement_inr" in data

    def test_error_distribution_sums_to_batch_size(self, client: TestClient):
        data = client.get("/api/data/summary").json()
        total = sum(data["error_distribution"].values())
        assert total == BATCH_SIZE


# ---------------------------------------------------------------------------
# Phase 2 placeholders (should return 200 with "detail" key, NOT 501/404)
# ---------------------------------------------------------------------------

class TestPhase2Placeholders:
    def test_run_pipeline(self, client: TestClient):
        resp = client.post("/api/run")
        assert resp.status_code == 200
        assert "detail" in resp.json()

    def test_get_report(self, client: TestClient):
        resp = client.get("/api/report")
        assert resp.status_code == 200
        assert "detail" in resp.json()

    def test_get_accuracy(self, client: TestClient):
        resp = client.get("/api/accuracy")
        assert resp.status_code == 200
        assert "detail" in resp.json()

    def test_qa(self, client: TestClient):
        resp = client.post("/api/qa")
        assert resp.status_code == 200
        assert "detail" in resp.json()
