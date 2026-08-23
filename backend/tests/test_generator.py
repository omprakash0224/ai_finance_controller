"""
tests/test_generator.py
=======================
Unit tests for backend/data/generator.py.

Covers
------
- razorpay_id() ID format validation
- bank_utr() UTR format validation
- compute_settlement_date() T+0/T+1/T+2 logic (including holiday & weekend skipping)
- is_working_day() working-day predicate
- BatchGenerator output: batch sizes, ID prefixes, error distribution, net_amount arithmetic
- Determinism: same seed → same output
- Cache: get_batch() / reset_batch() behaviour
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal

import pytest

from data.generator import (
    BATCH_SIZE,
    BatchGenerator,
    compute_settlement_date,
    get_batch,
    is_working_day,
    razorpay_id,
    bank_utr,
    reset_batch,
    _HOLIDAYS,
)
from data.schema import DataBatch, ErrorType


# ---------------------------------------------------------------------------
# razorpay_id()
# ---------------------------------------------------------------------------

class TestRazorpayId:
    def test_pay_prefix(self):
        rid = razorpay_id("pay")
        assert rid.startswith("pay_")

    def test_order_prefix(self):
        rid = razorpay_id("order")
        assert rid.startswith("order_")

    def test_setl_prefix(self):
        rid = razorpay_id("setl")
        assert rid.startswith("setl_")

    def test_default_length(self):
        rid = razorpay_id("pay")
        # format: "pay_" + 14 chars = 18 total
        assert len(rid) == len("pay_") + 14

    def test_custom_length(self):
        rid = razorpay_id("pay", length=8)
        assert len(rid) == len("pay_") + 8

    def test_alphanumeric_suffix(self):
        rid = razorpay_id("pay")
        suffix = rid.split("_", 1)[1]
        assert re.fullmatch(r"[a-zA-Z0-9]+", suffix), f"Non-alphanumeric suffix: {suffix}"

    def test_uniqueness_across_calls(self):
        ids = {razorpay_id("pay") for _ in range(100)}
        # With 14 alphanumeric chars there should be no collisions in 100 calls
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# bank_utr()
# ---------------------------------------------------------------------------

class TestBankUtr:
    def test_contains_date(self):
        d = datetime.date(2026, 8, 22)
        utr = bank_utr(d)
        assert "260822" in utr

    def test_format(self):
        d = datetime.date(2026, 8, 22)
        utr = bank_utr(d)
        # Should be all uppercase alphanumeric
        assert re.fullmatch(r"[A-Z0-9]+", utr), f"Unexpected UTR format: {utr}"

    def test_minimum_length(self):
        d = datetime.date(2026, 8, 22)
        utr = bank_utr(d)
        assert len(utr) >= 10


# ---------------------------------------------------------------------------
# compute_settlement_date()
# ---------------------------------------------------------------------------

class TestComputeSettlementDate:
    def test_t0_same_day(self):
        d = datetime.date(2026, 7, 20)  # Monday
        assert compute_settlement_date(d, "T0") == d

    def test_t1_next_working_day_from_monday(self):
        monday = datetime.date(2026, 7, 20)
        result = compute_settlement_date(monday, "T1")
        assert result == datetime.date(2026, 7, 21)  # Tuesday

    def test_t2_two_working_days(self):
        monday = datetime.date(2026, 7, 20)
        result = compute_settlement_date(monday, "T2")
        assert result == datetime.date(2026, 7, 22)  # Wednesday

    def test_t1_skips_sunday(self):
        # Saturday 18 Jul 2026 → next WD skips Sunday → Monday 20 Jul
        saturday = datetime.date(2026, 7, 18)
        result = compute_settlement_date(saturday, "T1")
        # Sunday 19 Jul is skipped
        assert result.weekday() != 6  # not Sunday

    def test_t1_skips_2nd_saturday(self):
        # 2nd Saturday of August 2026 = 8 Aug 2026
        # Friday 7 Aug 2026 + T1 should skip 8 Aug (2nd Sat)
        friday = datetime.date(2026, 8, 7)
        result = compute_settlement_date(friday, "T1")
        # 8 Aug is 2nd Saturday — must be skipped
        assert result != datetime.date(2026, 8, 8)
        assert result == datetime.date(2026, 8, 10)  # Monday

    def test_t1_skips_4th_saturday(self):
        # 4th Saturday of August 2026 = 22 Aug 2026
        friday = datetime.date(2026, 8, 21)
        result = compute_settlement_date(friday, "T1")
        assert result != datetime.date(2026, 8, 22)  # 4th Sat must be skipped
        assert result == datetime.date(2026, 8, 24)  # Monday

    def test_t1_skips_holiday(self):
        # 14 Aug 2026 (Friday) + T1 should skip 15 Aug (Independence Day)
        day_before = datetime.date(2026, 8, 14)
        result = compute_settlement_date(day_before, "T1")
        assert datetime.date(2026, 8, 15) not in {result}
        assert result > datetime.date(2026, 8, 15)

    def test_settlement_date_not_before_capture(self):
        d = datetime.date(2026, 7, 20)
        for tier in ("T0", "T1", "T2"):
            result = compute_settlement_date(d, tier)
            assert result >= d

    def test_invalid_tier_raises(self):
        with pytest.raises(KeyError):
            compute_settlement_date(datetime.date(2026, 7, 20), "T3")


# ---------------------------------------------------------------------------
# is_working_day()
# ---------------------------------------------------------------------------

class TestIsWorkingDay:
    def test_sunday_is_not_working_day(self):
        # 19 Jul 2026 is Sunday
        assert not is_working_day(datetime.date(2026, 7, 19))

    def test_monday_is_working_day(self):
        # 20 Jul 2026 is Monday
        assert is_working_day(datetime.date(2026, 7, 20))

    def test_2nd_saturday_is_not_working_day(self):
        # 8 Aug 2026 is 2nd Saturday of August
        assert not is_working_day(datetime.date(2026, 8, 8))

    def test_4th_saturday_is_not_working_day(self):
        # 22 Aug 2026 is 4th Saturday of August
        assert not is_working_day(datetime.date(2026, 8, 22))

    def test_1st_saturday_is_working_day(self):
        # 1 Aug 2026 is 1st Saturday of August
        assert is_working_day(datetime.date(2026, 8, 1))

    def test_3rd_saturday_is_working_day(self):
        # 15 Aug 2026 is 3rd Saturday — but it's also Independence Day!
        # So it's not a working day due to the holiday rule.
        # Pick 3rd Saturday of July: 18 Jul is actually the 3rd Saturday.
        # 18 Jul 2026: weekday = 5 (Sat), (18-1)//7 = 2 → 3rd Saturday → working
        assert is_working_day(datetime.date(2026, 7, 18))

    def test_holiday_is_not_working_day(self):
        for holiday in _HOLIDAYS:
            assert not is_working_day(holiday)


# ---------------------------------------------------------------------------
# BatchGenerator
# ---------------------------------------------------------------------------

class TestBatchGenerator:
    @pytest.fixture(scope="class")
    def batch(self) -> DataBatch:
        return BatchGenerator(seed=42).generate()

    def test_payment_count(self, batch: DataBatch):
        assert len(batch.payments) == BATCH_SIZE

    def test_all_pay_ids_have_correct_prefix(self, batch: DataBatch):
        for p in batch.payments:
            assert p.pay_id.startswith("pay_"), f"Bad pay_id: {p.pay_id}"

    def test_all_order_ids_have_correct_prefix(self, batch: DataBatch):
        for p in batch.payments:
            assert p.order_id.startswith("order_"), f"Bad order_id: {p.order_id}"

    def test_all_settlement_ids_have_correct_prefix(self, batch: DataBatch):
        for p in batch.payments:
            assert p.settlement_id.startswith("setl_"), f"Bad setl_id: {p.settlement_id}"

    def test_all_bank_txn_ids_have_correct_prefix(self, batch: DataBatch):
        for t in batch.bank_txns:
            assert t.txn_id.startswith("btxn_"), f"Bad txn_id: {t.txn_id}"

    def test_all_ledger_entry_ids_have_correct_prefix(self, batch: DataBatch):
        for e in batch.ledger_entries:
            assert e.entry_id.startswith("ent_"), f"Bad entry_id: {e.entry_id}"

    def test_all_settlement_summary_ids_correct(self, batch: DataBatch):
        for s in batch.settlements:
            assert s.settlement_id.startswith("setl_"), f"Bad setl_id: {s.settlement_id}"

    def test_net_amount_positive_for_all_payments(self, batch: DataBatch):
        for p in batch.payments:
            assert p.net_amount > 0, f"Non-positive net_amount for {p.pay_id}"

    def test_fee_tax_net_relationship(self, batch: DataBatch):
        """net_amount should equal amount - fee - tax (within 1 cent rounding)."""
        for p in batch.payments:
            expected = p.amount - p.fee - p.tax
            diff = abs(expected - p.net_amount)
            assert diff < Decimal("0.02"), (
                f"net_amount mismatch for {p.pay_id}: "
                f"{p.amount} - {p.fee} - {p.tax} = {expected}, got {p.net_amount}"
            )

    def test_settlement_date_not_before_captured_at(self, batch: DataBatch):
        for p in batch.payments:
            assert p.settlement_date >= p.captured_at, (
                f"Settlement date {p.settlement_date} before capture {p.captured_at}"
            )

    def test_bank_txns_fewer_than_payments_due_to_no_credit(self, batch: DataBatch):
        """Some payments have no bank credit (error_type=no_bank_credit)."""
        no_credit_count = sum(
            1 for p in batch.payments if p.error_type == ErrorType.no_bank_credit
        )
        assert len(batch.bank_txns) == BATCH_SIZE - no_credit_count

    def test_split_payments_produce_multiple_ledger_entries(self, batch: DataBatch):
        """Payments with error_type=split should have ≥ 2 ledger entries."""
        split_payments = {p.settlement_id for p in batch.payments if p.error_type == ErrorType.split}
        for sid in split_payments:
            entries = [e for e in batch.ledger_entries if e.internal_ref == sid]
            assert len(entries) >= 2, f"Expected split entries for {sid}, got {len(entries)}"

    def test_error_distribution_approximately_correct(self, batch: DataBatch):
        """Error type counts should be close to target rates (within ±10 rows)."""
        from collections import Counter
        counts = Counter(p.error_type for p in batch.payments)
        total = BATCH_SIZE
        tolerance = 10  # allow generous tolerance for small batch

        clean_rate = counts[ErrorType.clean] / total
        assert 0.40 <= clean_rate <= 0.70, f"clean rate {clean_rate:.0%} out of range"

        no_credit_rate = counts[ErrorType.no_bank_credit] / total
        assert 0.02 <= no_credit_rate <= 0.25, f"no_bank_credit rate {no_credit_rate:.0%} out of range"

    def test_all_currencies_are_inr(self, batch: DataBatch):
        for p in batch.payments:
            assert p.currency == "INR"
        for t in batch.bank_txns:
            assert t.currency == "INR"

    def test_settlements_have_positive_total(self, batch: DataBatch):
        for s in batch.settlements:
            assert s.total_amount > 0
            assert s.num_payments >= 1

    def test_bank_utr_in_bank_description(self, batch: DataBatch):
        """Each bank txn description should reference its own bank_ref (UTR)."""
        for t in batch.bank_txns:
            assert t.bank_ref in t.description, (
                f"UTR {t.bank_ref} missing from description: {t.description}"
            )

    def test_ledger_narration_contains_settlement_id(self, batch: DataBatch):
        for e in batch.ledger_entries:
            assert e.internal_ref in e.narration, (
                f"internal_ref {e.internal_ref} missing from narration: {e.narration}"
            )

    def test_amount_range_reasonable(self, batch: DataBatch):
        for p in batch.payments:
            assert Decimal("100") <= p.amount <= Decimal("200000"), (
                f"Unexpected amount {p.amount} for {p.pay_id}"
            )


class TestBatchDeterminism:
    def test_same_seed_same_output(self):
        b1 = BatchGenerator(seed=42).generate()
        b2 = BatchGenerator(seed=42).generate()
        assert [p.pay_id for p in b1.payments] == [p.pay_id for p in b2.payments]
        assert [t.txn_id for t in b1.bank_txns] == [t.txn_id for t in b2.bank_txns]

    def test_different_seed_different_output(self):
        b1 = BatchGenerator(seed=42).generate()
        b2 = BatchGenerator(seed=99).generate()
        # It would be astronomically unlikely for all 60 IDs to match
        assert [p.pay_id for p in b1.payments] != [p.pay_id for p in b2.payments]


class TestGetBatchCache:
    def test_returns_databatch(self):
        reset_batch()
        batch = get_batch()
        assert isinstance(batch, DataBatch)

    def test_cached_call_returns_same_object(self):
        reset_batch()
        b1 = get_batch()
        b2 = get_batch()
        assert b1 is b2

    def test_reset_clears_cache(self):
        reset_batch()
        b1 = get_batch()
        reset_batch()
        b2 = get_batch()
        # After reset, a new object is created (different identity)
        assert b1 is not b2

    def test_batch_size_after_reset(self):
        reset_batch()
        batch = get_batch()
        assert len(batch.payments) == BATCH_SIZE
