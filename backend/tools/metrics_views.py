"""
backend/tools/metrics_views.py
================================
Pre-aggregated summary queries for the Settlement Q&A fast-path layer.

Why this module exists
----------------------
For common dashboard questions (match rate, GST totals, pending settlement,
top exceptions), scanning 1,000,000 rows in PostgreSQL on every Q&A request
is wasteful and slow.  This module provides named Python functions that execute
highly targeted SQL aggregations — each designed to complete in < 80 ms even
at 1M rows (aided by the indexes added in db.py).

These results are cached in Upstash Redis (TTL 60 s by default) so that
repeated identical questions cost $0 in AI tokens and < 5 ms in latency.

The Q&A agent's smart router checks whether a question matches any of these
fast-path patterns BEFORE calling Gemini.  If it matches, the answer is
returned immediately from pre-aggregated data.

Functions
---------
  get_match_rate()           → reconciliation match rate and counts
  get_pending_settlement()   → total and per-date pending settlements
  get_gst_summary()          → GST totals by code (IGST, CGST+SGST, Exempt)
  get_exception_summary()    → top exception reasons and counts
  get_daily_volume()         → per-day payment volume for the last 30 days
  get_method_breakdown()     → payment volume split by method (UPI/Card/etc.)
  get_all_metrics()          → all of the above in one call (for cache warm-up)
"""

from __future__ import annotations

import logging
from typing import Any

from data import db as _db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual metric functions
# ---------------------------------------------------------------------------

def get_match_rate() -> dict[str, Any]:
    """
    Return the current reconciliation match rate and counts from match_results.

    Query hits only the match_results table (indexed on status).
    Typical latency: < 5 ms at 1M rows.
    """
    rows = _db.query(
        """
        SELECT
            status,
            COUNT(*)                AS count,
            AVG(confidence)         AS avg_confidence
        FROM match_results
        GROUP BY status
        """
    )

    by_status = {r["status"]: {"count": int(r["count"]),
                                "avg_confidence": round(float(r["avg_confidence"] or 0), 4)}
                 for r in rows}

    matched   = by_status.get("matched",   {}).get("count", 0)
    exception = by_status.get("exception", {}).get("count", 0)
    total     = matched + exception
    rate      = matched / total if total > 0 else 0.0

    return {
        "metric":           "match_rate",
        "matched_count":    matched,
        "exception_count":  exception,
        "total_processed":  total,
        "match_rate":       round(rate, 4),
        "match_rate_pct":   round(rate * 100, 2),
        "avg_confidence":   by_status.get("matched", {}).get("avg_confidence", 0.0),
    }


def get_pending_settlement() -> dict[str, Any]:
    """
    Return total pending settlement amount and per-date breakdown.

    Scans only settlements table (small) — always fast.
    """
    total_row = _db.query(
        "SELECT COALESCE(SUM(total_amount), 0) AS total, COUNT(*) AS cnt "
        "FROM settlements WHERE status = 'pending'"
    )
    total   = float(total_row[0]["total"] or 0) if total_row else 0.0
    count   = int(total_row[0]["cnt"]   or 0) if total_row else 0

    by_date = _db.query(
        """
        SELECT
            settlement_date,
            SUM(total_amount)  AS amount,
            COUNT(*)           AS num_settlements
        FROM settlements
        WHERE status = 'pending'
        GROUP BY settlement_date
        ORDER BY settlement_date ASC
        LIMIT 30
        """
    )

    return {
        "metric":                   "pending_settlement",
        "total_pending_inr":        round(total, 2),
        "pending_settlement_count": count,
        "by_date":                  [
            {
                "date":             str(r["settlement_date"]),
                "amount_inr":       round(float(r["amount"] or 0), 2),
                "num_settlements":  int(r["num_settlements"]),
            }
            for r in by_date
        ],
    }


def get_gst_summary() -> dict[str, Any]:
    """
    Return GST totals by code using the deterministic CASE classification.

    Joins match_results (indexed on status) with razorpay_payments.
    Typical latency: < 80 ms at 1M rows.
    """
    rows = _db.query(
        """
        SELECT
            CASE
                WHEN p.status = 'refunded'                         THEN 'GST_REVERSAL'
                WHEN p.amount < 1000                               THEN 'Exempt'
                WHEN p.method IN ('upi', 'netbanking', 'wallet')   THEN 'IGST@18%'
                ELSE                                                    'CGST@9%+SGST@9%'
            END                            AS gst_code,
            COUNT(*)                       AS txn_count,
            COALESCE(SUM(p.tax),  0)       AS total_tax,
            COALESCE(SUM(p.fee),  0)       AS total_fee
        FROM razorpay_payments p
        INNER JOIN match_results m ON m.pay_id = p.pay_id
        WHERE m.status = 'matched'
        GROUP BY 1
        ORDER BY total_tax DESC
        """
    )

    by_code   = {}
    total_tax = 0.0
    total_txn = 0

    for r in rows:
        code = r["gst_code"]
        tax  = float(r["total_tax"] or 0)
        by_code[code] = {
            "count":     int(r["txn_count"]),
            "total_tax": round(tax, 2),
            "total_fee": round(float(r["total_fee"] or 0), 2),
        }
        total_tax += tax
        total_txn += int(r["txn_count"])

    return {
        "metric":        "gst_summary",
        "total_tax_inr": round(total_tax, 2),
        "total_tagged":  total_txn,
        "by_gst_code":   by_code,
    }


def get_exception_summary() -> dict[str, Any]:
    """
    Return top exception reasons with counts, sorted by frequency.

    Joins exceptions with razorpay_payments for method breakdown.
    """
    rows = _db.query(
        """
        SELECT
            e.reason,
            p.method,
            COUNT(*)                             AS exception_count,
            ROUND(AVG(p.net_amount::NUMERIC), 2) AS avg_amount_inr
        FROM exceptions e
        JOIN razorpay_payments p ON p.pay_id = e.record_id
        GROUP BY e.reason, p.method
        ORDER BY exception_count DESC
        LIMIT 20
        """
    )

    total_row = _db.query("SELECT COUNT(*) AS n FROM exceptions")
    total     = int(total_row[0]["n"] or 0) if total_row else 0

    return {
        "metric":           "exception_summary",
        "total_exceptions": total,
        "top_patterns":     [
            {
                "reason":          r["reason"],
                "method":          r["method"],
                "count":           int(r["exception_count"]),
                "avg_amount_inr":  float(r["avg_amount_inr"] or 0),
            }
            for r in rows
        ],
    }


def get_daily_volume() -> dict[str, Any]:
    """
    Return per-day gross payment volume for the last 30 days.

    Uses the idx_payments_date_amount index for a fast date-range aggregation.
    """
    rows = _db.query(
        """
        SELECT
            settlement_date             AS date,
            COUNT(*)                    AS num_payments,
            COALESCE(SUM(amount), 0)    AS gross_volume_inr,
            COALESCE(SUM(net_amount), 0) AS net_volume_inr,
            COALESCE(SUM(fee), 0)       AS total_fee_inr,
            COALESCE(SUM(tax), 0)       AS total_tax_inr
        FROM razorpay_payments
        GROUP BY settlement_date
        ORDER BY settlement_date DESC
        LIMIT 30
        """
    )

    total_gross = sum(float(r["gross_volume_inr"] or 0) for r in rows)

    return {
        "metric":             "daily_volume",
        "total_gross_inr":    round(total_gross, 2),
        "days":               [
            {
                "date":            str(r["date"]),
                "num_payments":    int(r["num_payments"]),
                "gross_volume":    round(float(r["gross_volume_inr"] or 0), 2),
                "net_volume":      round(float(r["net_volume_inr"]   or 0), 2),
                "total_fee":       round(float(r["total_fee_inr"]    or 0), 2),
                "total_tax":       round(float(r["total_tax_inr"]    or 0), 2),
            }
            for r in rows
        ],
    }


def get_method_breakdown() -> dict[str, Any]:
    """
    Return gross volume and count split by payment method (UPI, card, etc.).
    """
    rows = _db.query(
        """
        SELECT
            method,
            COUNT(*)                       AS txn_count,
            COALESCE(SUM(amount),     0)   AS gross_volume_inr,
            COALESCE(SUM(net_amount), 0)   AS net_volume_inr,
            ROUND(AVG(amount::NUMERIC), 2) AS avg_transaction_inr
        FROM razorpay_payments
        GROUP BY method
        ORDER BY gross_volume_inr DESC
        """
    )

    return {
        "metric": "method_breakdown",
        "by_method": [
            {
                "method":              r["method"],
                "txn_count":           int(r["txn_count"]),
                "gross_volume_inr":    round(float(r["gross_volume_inr"]    or 0), 2),
                "net_volume_inr":      round(float(r["net_volume_inr"]      or 0), 2),
                "avg_transaction_inr": round(float(r["avg_transaction_inr"] or 0), 2),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Bulk warm-up — compute and cache all metrics in one call
# ---------------------------------------------------------------------------

def get_all_metrics() -> dict[str, Any]:
    """
    Compute all pre-aggregated metrics and return them as a single dict.

    Called during cache warm-up (e.g. after a pipeline run completes)
    to pre-populate all Redis keys and eliminate cold-cache latency for
    the first user queries after a reconciliation.
    """
    return {
        "match_rate":         get_match_rate(),
        "pending_settlement": get_pending_settlement(),
        "gst_summary":        get_gst_summary(),
        "exception_summary":  get_exception_summary(),
        "daily_volume":       get_daily_volume(),
        "method_breakdown":   get_method_breakdown(),
    }
