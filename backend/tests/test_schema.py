"""
tests/test_schema.py
====================
Unit tests for backend/data/schema.py Pydantic models.

Covers
------
- Valid instantiation of every model
- Prefix validators (pay_, order_, setl_, btxn_, ent_, exc_)
- Enum validation (method, status, match_type, etc.)
- Net-amount positivity constraint
- Decimal field precision
- DataBatch container properties
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from data.schema import (
    BankTxn,
    DataBatch,
    ErrorType,
    ExceptionRecord,
    LedgerEntry,
    MatchResult,
    MatchType,
    PaymentMethod,
    PaymentStatus,
    RazorpayPayment,
    RecordStatus,
    Settlement,
    SettlementStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payment(**overrides) -> RazorpayPayment:
    defaults = dict(
        pay_id="pay_TestId00000001",
        order_id="order_TestId000001",
        captured_at=datetime.date(2026, 7, 15),
        amount=Decimal("5000.00"),
        currency="INR",
        method=PaymentMethod.upi,
        status=PaymentStatus.captured,
        settlement_id="setl_TestId000001",
        settlement_date=datetime.date(2026, 7, 16),
        settlement_utr="HDFCN2607160001234",
        fee=Decimal("100.00"),
        tax=Decimal("18.00"),
        net_amount=Decimal("4882.00"),
        error_type=ErrorType.clean,
    )
    defaults.update(overrides)
    return RazorpayPayment(**defaults)


def _bank_txn(**overrides) -> BankTxn:
    defaults = dict(
        txn_id="btxn_abc12345",
        value_date=datetime.date(2026, 7, 16),
        amount=Decimal("4882.00"),
        description="NEFT CR HDFCN2607160001234 AI FINANCE CO LTD",
        bank_ref="HDFCN2607160001234",
        currency="INR",
        settlement_id="setl_TestId000001",
    )
    defaults.update(overrides)
    return BankTxn(**defaults)


def _ledger_entry(**overrides) -> LedgerEntry:
    defaults = dict(
        entry_id="ent_abc12345",
        date=datetime.date(2026, 7, 16),
        amount=Decimal("4882.00"),
        narration="Razorpay settle setl_TestId000001",
        account_code="4001",
        internal_ref="setl_TestId000001",
    )
    defaults.update(overrides)
    return LedgerEntry(**defaults)


def _settlement(**overrides) -> Settlement:
    defaults = dict(
        settlement_id="setl_TestId000001",
        settlement_date=datetime.date(2026, 7, 16),
        total_amount=Decimal("4882.00"),
        num_payments=1,
        status=SettlementStatus.processed,
    )
    defaults.update(overrides)
    return Settlement(**defaults)


# ---------------------------------------------------------------------------
# RazorpayPayment
# ---------------------------------------------------------------------------

class TestRazorpayPayment:
    def test_valid_instantiation(self):
        p = _payment()
        assert p.pay_id == "pay_TestId00000001"
        assert p.currency == "INR"
        assert p.net_amount == Decimal("4882.00")

    def test_pay_id_prefix_required(self):
        with pytest.raises(Exception, match="pay_"):
            _payment(pay_id="PAY_wrongprefix")

    def test_order_id_prefix_required(self):
        with pytest.raises(Exception, match="order_"):
            _payment(order_id="ORD_wrongprefix")

    def test_settlement_id_prefix_required(self):
        with pytest.raises(Exception, match="setl_"):
            _payment(settlement_id="SETL_wrongprefix")

    def test_negative_net_amount_rejected(self):
        with pytest.raises(Exception):
            _payment(fee=Decimal("3000.00"), tax=Decimal("3000.00"), net_amount=Decimal("-1.00"))

    def test_zero_net_amount_rejected(self):
        with pytest.raises(Exception):
            _payment(net_amount=Decimal("0.00"))

    def test_all_payment_methods(self):
        for method in PaymentMethod:
            p = _payment(method=method)
            assert p.method == method

    def test_all_payment_statuses(self):
        for status in PaymentStatus:
            p = _payment(status=status)
            assert p.status == status

    def test_all_error_types(self):
        for et in ErrorType:
            p = _payment(error_type=et)
            assert p.error_type == et

    def test_amount_must_be_positive(self):
        with pytest.raises(Exception):
            _payment(amount=Decimal("-100.00"))

    def test_fee_must_be_non_negative(self):
        with pytest.raises(Exception):
            _payment(fee=Decimal("-1.00"))

    def test_tax_must_be_non_negative(self):
        with pytest.raises(Exception):
            _payment(tax=Decimal("-1.00"))


# ---------------------------------------------------------------------------
# BankTxn
# ---------------------------------------------------------------------------

class TestBankTxn:
    def test_valid_instantiation(self):
        t = _bank_txn()
        assert t.txn_id.startswith("btxn_")
        assert t.currency == "INR"
        assert t.amount == Decimal("4882.00")

    def test_txn_id_prefix_required(self):
        with pytest.raises(Exception, match="btxn_"):
            _bank_txn(txn_id="TXN_wrongprefix")

    def test_amount_positive(self):
        with pytest.raises(Exception):
            _bank_txn(amount=Decimal("-100.00"))

    def test_settlement_id_optional(self):
        t = _bank_txn(settlement_id=None)
        assert t.settlement_id is None

    def test_bank_ref_stored_correctly(self):
        t = _bank_txn(bank_ref="SBIN260720ABCDEF")
        assert t.bank_ref == "SBIN260720ABCDEF"


# ---------------------------------------------------------------------------
# LedgerEntry
# ---------------------------------------------------------------------------

class TestLedgerEntry:
    def test_valid_instantiation(self):
        e = _ledger_entry()
        assert e.entry_id.startswith("ent_")
        assert e.account_code == "4001"

    def test_entry_id_prefix_required(self):
        with pytest.raises(Exception, match="ent_"):
            _ledger_entry(entry_id="ENTRY_wrongprefix")

    def test_negative_amount_allowed(self):
        # Debit entries are negative
        e = _ledger_entry(amount=Decimal("-4882.00"))
        assert e.amount == Decimal("-4882.00")

    def test_narration_stored(self):
        e = _ledger_entry(narration="Razorpay settle setl_abc (split)")
        assert "split" in e.narration


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class TestSettlement:
    def test_valid_instantiation(self):
        s = _settlement()
        assert s.settlement_id.startswith("setl_")
        assert s.num_payments == 1
        assert s.status == SettlementStatus.processed

    def test_settlement_id_prefix_required(self):
        with pytest.raises(Exception, match="setl_"):
            _settlement(settlement_id="SETL_wrong")

    def test_total_amount_positive(self):
        with pytest.raises(Exception):
            _settlement(total_amount=Decimal("-1.00"))

    def test_num_payments_at_least_one(self):
        with pytest.raises(Exception):
            _settlement(num_payments=0)

    def test_all_settlement_statuses(self):
        for status in SettlementStatus:
            s = _settlement(status=status)
            assert s.status == status


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------

class TestMatchResult:
    def test_defaults(self):
        mr = MatchResult(pay_id="pay_TestId00000001")
        assert mr.match_type == MatchType.unmatched
        assert mr.status == RecordStatus.exception
        assert mr.confidence == 0.0
        assert mr.entry_id is None
        assert mr.txn_id is None
        assert mr.delta is None

    def test_all_match_types(self):
        for mt in MatchType:
            mr = MatchResult(pay_id="pay_TestId00000001", match_type=mt)
            assert mr.match_type == mt

    def test_confidence_bounds(self):
        MatchResult(pay_id="pay_TestId00000001", confidence=0.0)
        MatchResult(pay_id="pay_TestId00000001", confidence=1.0)
        MatchResult(pay_id="pay_TestId00000001", confidence=0.85)
        with pytest.raises(Exception):
            MatchResult(pay_id="pay_TestId00000001", confidence=1.1)
        with pytest.raises(Exception):
            MatchResult(pay_id="pay_TestId00000001", confidence=-0.1)

    def test_delta_stored(self):
        mr = MatchResult(
            pay_id="pay_TestId00000001",
            match_type=MatchType.fuzzy_amount,
            delta=Decimal("2.50"),
            confidence=0.9,
            status=RecordStatus.matched,
        )
        assert mr.delta == Decimal("2.50")


# ---------------------------------------------------------------------------
# ExceptionRecord
# ---------------------------------------------------------------------------

class TestExceptionRecord:
    def test_valid_instantiation(self):
        exc = ExceptionRecord(
            exception_id="exc_abc12345",
            source="reconciler",
            record_id="pay_TestId00000001",
            reason="no_bank_credit",
            agent_reasoning="No bank_statement row found with matching UTR after 3 attempts.",
            suggested_action="Contact Razorpay support with settlement ID setl_TestId000001.",
        )
        assert exc.exception_id.startswith("exc_")
        assert exc.source == "reconciler"

    def test_exception_id_prefix_required(self):
        with pytest.raises(Exception, match="exc_"):
            ExceptionRecord(
                exception_id="EXC_wrong",
                source="reconciler",
                record_id="pay_TestId00000001",
                reason="no_bank_credit",
                agent_reasoning="...",
                suggested_action="...",
            )


# ---------------------------------------------------------------------------
# DataBatch
# ---------------------------------------------------------------------------

class TestDataBatch:
    def _make_batch(self) -> DataBatch:
        return DataBatch(
            payments=[_payment()],
            bank_txns=[_bank_txn()],
            ledger_entries=[_ledger_entry()],
            settlements=[_settlement()],
        )

    def test_payment_count_property(self):
        batch = self._make_batch()
        assert batch.payment_count == 1

    def test_bank_txn_count_property(self):
        batch = self._make_batch()
        assert batch.bank_txn_count == 1

    def test_ledger_entry_count_property(self):
        batch = self._make_batch()
        assert batch.ledger_entry_count == 1

    def test_empty_batch_allowed(self):
        batch = DataBatch(payments=[], bank_txns=[], ledger_entries=[], settlements=[])
        assert batch.payment_count == 0
