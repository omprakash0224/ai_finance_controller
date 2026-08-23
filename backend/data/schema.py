"""
backend/data/schema.py
======================
Pydantic v2 models for every entity in the AI Finance Controller pipeline.

Tables
------
  RazorpayPayment  → razorpay_payments   (simulates Razorpay Payments API)
  BankTxn          → bank_statements     (simulates bank credit entries)
  LedgerEntry      → ledger_entries      (internal accounting ledger)
  Settlement       → settlements         (Razorpay settlement summary)
  MatchResult      → match_results       (reconciler output, one row per pay_id)
  ExceptionRecord  → exceptions          (escalated / unresolved mismatches)
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PaymentMethod(str, Enum):
    upi = "upi"
    card = "card"
    netbanking = "netbanking"
    wallet = "wallet"


class PaymentStatus(str, Enum):
    captured = "captured"
    refunded = "refunded"
    failed = "failed"


class SettlementStatus(str, Enum):
    pending = "pending"
    processed = "processed"
    on_hold = "on_hold"


class MatchType(str, Enum):
    exact = "exact"
    fuzzy_amount = "fuzzy_amount"
    fuzzy_date = "fuzzy_date"
    utr_match = "utr_match"
    multi_split = "multi_split"
    unmatched = "unmatched"


class RecordStatus(str, Enum):
    matched = "matched"
    exception = "exception"
    escalated = "escalated"


class ErrorType(str, Enum):
    """Ground-truth label injected by the generator — used for accuracy scoring."""
    clean = "clean"
    amount_delta = "amount_delta"
    date_slip = "date_slip"
    split = "split"
    no_bank_credit = "no_bank_credit"


# ---------------------------------------------------------------------------
# RazorpayPayment
# ---------------------------------------------------------------------------

class RazorpayPayment(BaseModel):
    """
    Simulates a row from the Razorpay Payments + Settlements API.
    Primary key: pay_id.
    """

    pay_id: str = Field(..., description="Razorpay payment ID, prefix pay_")
    order_id: str = Field(..., description="Razorpay order ID, prefix order_")
    captured_at: datetime.date = Field(..., description="Date the payment was captured (T day)")
    amount: Decimal = Field(..., gt=0, description="Gross amount in INR")
    currency: str = Field(default="INR", description="Always INR for this batch")
    method: PaymentMethod
    status: PaymentStatus = PaymentStatus.captured
    settlement_id: str = Field(..., description="Linked settlement ID, prefix setl_")
    settlement_date: datetime.date = Field(..., description="Expected bank credit date (T+0/T+1/T+2)")
    settlement_utr: str = Field(..., description="Bank UTR for reconciliation")
    fee: Decimal = Field(..., ge=0, description="Razorpay platform fee in INR")
    tax: Decimal = Field(..., ge=0, description="GST on fee (18%)")
    net_amount: Decimal = Field(..., description="amount - fee - tax (credited to merchant bank)")
    # Ground-truth label — set by generator, used for accuracy scoring only
    error_type: ErrorType = Field(default=ErrorType.clean, description="Injected error type for scoring")

    @field_validator("pay_id")
    @classmethod
    def pay_id_prefix(cls, v: str) -> str:
        if not v.startswith("pay_"):
            raise ValueError("pay_id must start with 'pay_'")
        return v

    @field_validator("order_id")
    @classmethod
    def order_id_prefix(cls, v: str) -> str:
        if not v.startswith("order_"):
            raise ValueError("order_id must start with 'order_'")
        return v

    @field_validator("settlement_id")
    @classmethod
    def settlement_id_prefix(cls, v: str) -> str:
        if not v.startswith("setl_"):
            raise ValueError("settlement_id must start with 'setl_'")
        return v

    @model_validator(mode="after")
    def net_amount_is_positive(self) -> "RazorpayPayment":
        if self.net_amount <= 0:
            raise ValueError(f"net_amount must be positive, got {self.net_amount}")
        return self

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# BankTxn
# ---------------------------------------------------------------------------

class BankTxn(BaseModel):
    """
    Simulates a credit entry from the merchant's bank statement.
    Primary key: txn_id.
    """

    txn_id: str = Field(..., description="Bank-side transaction ID, prefix btxn_")
    value_date: datetime.date = Field(..., description="Date the credit appeared in the bank account")
    amount: Decimal = Field(..., gt=0, description="INR amount credited to merchant account")
    description: str = Field(..., description="Free-text narration, contains UTR and merchant name")
    bank_ref: str = Field(..., description="UTR number — maps to settlement_utr in razorpay_payments")
    currency: str = Field(default="INR")
    # Foreign key to razorpay_payments.settlement_id (set by generator)
    settlement_id: Optional[str] = Field(default=None, description="Links this credit to a Razorpay settlement")

    @field_validator("txn_id")
    @classmethod
    def txn_id_prefix(cls, v: str) -> str:
        if not v.startswith("btxn_"):
            raise ValueError("txn_id must start with 'btxn_'")
        return v

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# LedgerEntry
# ---------------------------------------------------------------------------

class LedgerEntry(BaseModel):
    """
    Simulates an internal accounting ledger entry.
    Primary key: entry_id.
    """

    entry_id: str = Field(..., description="Internal accounting entry ID, prefix ent_")
    date: datetime.date = Field(..., description="Accounting date")
    amount: Decimal = Field(..., description="INR amount (positive = credit, negative = debit)")
    narration: str = Field(..., description="e.g. 'Razorpay settle setl_Qr8wK2mN7vJpLs'")
    account_code: str = Field(..., description="Chart-of-accounts code, e.g. 4001, 2100")
    internal_ref: str = Field(..., description="settlement_id or pay_id for reconciliation")

    @field_validator("entry_id")
    @classmethod
    def entry_id_prefix(cls, v: str) -> str:
        if not v.startswith("ent_"):
            raise ValueError("entry_id must start with 'ent_'")
        return v

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

class Settlement(BaseModel):
    """
    Razorpay settlement summary — one row per settlement_id.
    Primary key: settlement_id.
    """

    settlement_id: str = Field(..., description="Razorpay settlement ID, prefix setl_")
    settlement_date: datetime.date
    total_amount: Decimal = Field(..., gt=0, description="Sum of net_amount for all payments in this settlement")
    num_payments: int = Field(..., ge=1, description="Number of payments bundled into this settlement")
    status: SettlementStatus = SettlementStatus.pending

    @field_validator("settlement_id")
    @classmethod
    def settlement_id_prefix(cls, v: str) -> str:
        if not v.startswith("setl_"):
            raise ValueError("settlement_id must start with 'setl_'")
        return v

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------

class MatchResult(BaseModel):
    """
    Output of the Reconciler agent — one row per payment that was processed.
    Primary key: pay_id.
    """

    pay_id: str = Field(..., description="Razorpay payment ID being reconciled")
    entry_id: Optional[str] = Field(default=None, description="Matched ledger entry_id (None if unmatched)")
    txn_id: Optional[str] = Field(default=None, description="Matched bank txn_id (None if unmatched)")
    match_type: MatchType = MatchType.unmatched
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Match confidence score 0-1")
    delta: Optional[Decimal] = Field(default=None, description="Amount or date delta that triggered fuzzy match")
    status: RecordStatus = RecordStatus.exception
    ground_truth_error_type: ErrorType = Field(
        default=ErrorType.clean,
        description="Ground-truth label from generator for accuracy scoring"
    )

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# ExceptionRecord
# ---------------------------------------------------------------------------

class ExceptionRecord(BaseModel):
    """
    Every unmatched or escalated record must have a full exception record.
    Primary key: exception_id.
    """

    exception_id: str = Field(..., description="Unique exception ID, prefix exc_")
    source: str = Field(..., description="Agent that raised the exception, e.g. 'reconciler'")
    record_id: str = Field(..., description="The pay_id or entry_id that could not be matched")
    reason: str = Field(..., description="Short reason code, e.g. 'no_bank_credit'")
    agent_reasoning: str = Field(..., description="Full text explanation from the agent")
    suggested_action: str = Field(..., description="Actionable recommendation for a human reviewer")

    @field_validator("exception_id")
    @classmethod
    def exception_id_prefix(cls, v: str) -> str:
        if not v.startswith("exc_"):
            raise ValueError("exception_id must start with 'exc_'")
        return v

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Batch — top-level container returned by GET /api/data
# ---------------------------------------------------------------------------

class DataBatch(BaseModel):
    """
    Full synthetic batch returned by GET /api/data.
    All lists are parallel and reference each other via ID fields.
    """

    payments: list[RazorpayPayment]
    bank_txns: list[BankTxn]
    ledger_entries: list[LedgerEntry]
    settlements: list[Settlement]

    @property
    def payment_count(self) -> int:
        return len(self.payments)

    @property
    def bank_txn_count(self) -> int:
        return len(self.bank_txns)

    @property
    def ledger_entry_count(self) -> int:
        return len(self.ledger_entries)

    model_config = {"arbitrary_types_allowed": True}
