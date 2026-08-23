"""
backend/data/generator.py
=========================
Generates a reproducible 60-row synthetic batch that mimics Razorpay payments,
bank credits, ledger entries, and settlement summaries — with controlled error
injection so the reconciler faces a realistic, non-trivial workload.

Batch size   : 60 payments  (> 50 specified in PLAN.md)
Seed         : 42  (fixed for reproducibility)
Error rates  :
  ~55%  clean   — UTR + amount exact match
  ~15%  amount_delta  — net_amount in bank row mutated ±1-5 INR
  ~10%  date_slip     — value_date in bank row mutated ±1-2 days
  ~8%   split         — one settlement_id maps to multiple ledger entries
  ~12%  no_bank_credit — bank row dropped; ledger entry orphaned
"""

from __future__ import annotations

import datetime
import random
import string
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from data.schema import (
    BankTxn,
    DataBatch,
    ErrorType,
    LedgerEntry,
    PaymentMethod,
    PaymentStatus,
    RazorpayPayment,
    Settlement,
    SettlementStatus,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BATCH_SIZE = 60
SEED = 42

# Indian public holidays in scope (add more as needed)
_HOLIDAYS: frozenset[datetime.date] = frozenset(
    {
        datetime.date(2026, 1, 26),  # Republic Day
        datetime.date(2026, 8, 15),  # Independence Day
        datetime.date(2026, 10, 2),  # Gandhi Jayanti
        datetime.date(2026, 11, 5),  # Diwali (approx)
    }
)

# Chart-of-accounts codes used in ledger entries
_COA_CODES = ["4001", "4002", "2100", "2101", "1001", "1002"]

# Error-type weights — must sum to 1.0
_ERROR_WEIGHTS = [
    (ErrorType.clean,         0.55),
    (ErrorType.amount_delta,  0.15),
    (ErrorType.date_slip,     0.10),
    (ErrorType.split,         0.08),
    (ErrorType.no_bank_credit, 0.12),
]
_ERROR_TYPES, _ERROR_PROBS = zip(*_ERROR_WEIGHTS)

# Settlement tier weights (T0, T1, T2)
_TIER_WEIGHTS = [("T0", 0.10), ("T1", 0.70), ("T2", 0.20)]
_TIERS, _TIER_PROBS = zip(*_TIER_WEIGHTS)


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def razorpay_id(prefix: str, length: int = 14) -> str:
    """Generate a Razorpay-style random ID, e.g. pay_Z6t7VFTb9xHeOs."""
    chars = string.ascii_letters + string.digits
    return f"{prefix}_{''.join(random.choices(chars, k=length))}"


def bank_utr(value_date: datetime.date) -> str:
    """
    Generate a realistic bank UTR reference.
    Format: <BANK_CODE><YYMMDD><7-digit-seq>
    e.g. HDFCN26082200001
    """
    bank_codes = ["HDFCN", "ICICIN", "SBIN", "AXISB", "KOTAKN"]
    code = random.choice(bank_codes)
    date_str = value_date.strftime("%y%m%d")
    seq = str(random.randint(1, 9_999_999)).zfill(7)
    return f"{code}{date_str}{seq}"


def _short_id(prefix: str = "ent") -> str:
    """Short 8-char unique ID using uuid4 hex."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Settlement date computation (per PLAN.md spec)
# ---------------------------------------------------------------------------

def compute_settlement_date(captured_at: datetime.date, tier: str) -> datetime.date:
    """
    Return the working-day-adjusted settlement date.

    Rules (from PLAN.md):
      T0 → same day as capture
      T1 → next working day
      T2 → 2 working days after capture

    Working day excludes:
      - Sundays (weekday == 6)
      - 2nd and 4th Saturdays (weekday == 5 and (day-1)//7 in {1, 3})
      - Indian public holidays listed in _HOLIDAYS
    """
    days_to_add = {"T0": 0, "T1": 1, "T2": 2}[tier]
    d = captured_at
    added = 0
    while added < days_to_add:
        d += datetime.timedelta(days=1)
        if d.weekday() == 6:
            continue  # Sunday
        if d.weekday() == 5 and (d.day - 1) // 7 in (1, 3):
            continue  # 2nd or 4th Saturday
        if d in _HOLIDAYS:
            continue
        added += 1
    return d


def is_working_day(d: datetime.date) -> bool:
    """Return True if *d* is an Indian bank working day."""
    if d.weekday() == 6:
        return False
    if d.weekday() == 5 and (d.day - 1) // 7 in (1, 3):
        return False
    if d in _HOLIDAYS:
        return False
    return True


# ---------------------------------------------------------------------------
# Fee + tax computation
# ---------------------------------------------------------------------------

def _compute_fee(amount: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """
    Return (fee, tax, net_amount) for a given gross amount.

    Razorpay fee rate  : 2% of amount, capped at ₹30 000
    GST on fee         : 18%
    net_amount         : amount - fee - tax
    All values rounded to 2 decimal places.
    """
    two_pct = (amount * Decimal("0.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    fee = min(two_pct, Decimal("30000.00"))
    tax = (fee * Decimal("0.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net_amount = (amount - fee - tax).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return fee, tax, net_amount


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

class BatchGenerator:
    """
    Generates the full synthetic batch.

    Usage::

        gen = BatchGenerator(seed=42)
        batch = gen.generate()
    """

    def __init__(self, seed: int = SEED, batch_size: int = BATCH_SIZE) -> None:
        self.seed = seed
        self.batch_size = batch_size
        random.seed(seed)

        # Capture date window: last 90 calendar days ending today
        self._end_date = datetime.date(2026, 8, 22)
        self._start_date = self._end_date - datetime.timedelta(days=90)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def generate(self) -> DataBatch:
        """
        Generate a full batch.  Returns a DataBatch with:
          - payments        (RazorpayPayment list)
          - bank_txns       (BankTxn list, possibly fewer than payments)
          - ledger_entries  (LedgerEntry list, possibly more than payments due to splits)
          - settlements     (Settlement list, one per unique settlement_id)
        """
        random.seed(self.seed)  # reset so every call is deterministic

        payments: list[RazorpayPayment] = []
        bank_txns: list[BankTxn] = []
        ledger_entries: list[LedgerEntry] = []
        # track settlements: settlement_id → {date, payments, total}
        settlements_map: dict[str, dict] = {}

        # Pre-assign error types (fixed, reproducible)
        error_types: list[ErrorType] = random.choices(
            list(_ERROR_TYPES), weights=list(_ERROR_PROBS), k=self.batch_size
        )

        for i in range(self.batch_size):
            error_type = error_types[i]
            pay = self._make_payment(i, error_type)
            payments.append(pay)

            # Accumulate settlement summary
            if pay.settlement_id not in settlements_map:
                settlements_map[pay.settlement_id] = {
                    "settlement_date": pay.settlement_date,
                    "payments": [],
                    "total_amount": Decimal("0.00"),
                }
            settlements_map[pay.settlement_id]["payments"].append(pay.pay_id)
            settlements_map[pay.settlement_id]["total_amount"] += pay.net_amount

            # Bank txn
            if error_type != ErrorType.no_bank_credit:
                txn = self._make_bank_txn(pay, error_type)
                bank_txns.append(txn)

            # Ledger entries (possibly split)
            entries = self._make_ledger_entries(pay, error_type)
            ledger_entries.extend(entries)

        # Build Settlement objects
        settlements: list[Settlement] = [
            Settlement(
                settlement_id=sid,
                settlement_date=data["settlement_date"],
                total_amount=data["total_amount"].quantize(Decimal("0.01")),
                num_payments=len(data["payments"]),
                status=SettlementStatus.processed
                if data["settlement_date"] <= self._end_date
                else SettlementStatus.pending,
            )
            for sid, data in settlements_map.items()
        ]

        return DataBatch(
            payments=payments,
            bank_txns=bank_txns,
            ledger_entries=ledger_entries,
            settlements=settlements,
        )

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _random_date(self) -> datetime.date:
        """Random date within the capture window."""
        delta = (self._end_date - self._start_date).days
        return self._start_date + datetime.timedelta(days=random.randint(0, delta))

    def _make_payment(self, index: int, error_type: ErrorType) -> RazorpayPayment:
        captured_at = self._random_date()
        tier = random.choices(list(_TIERS), weights=list(_TIER_PROBS))[0]
        settle_date = compute_settlement_date(captured_at, tier)

        amount = Decimal(str(round(random.uniform(500, 50_000), 2)))
        fee, tax, net_amount = _compute_fee(amount)

        utr = bank_utr(settle_date)

        return RazorpayPayment(
            pay_id=razorpay_id("pay"),
            order_id=razorpay_id("order"),
            captured_at=captured_at,
            amount=amount,
            currency="INR",
            method=random.choice(list(PaymentMethod)),
            status=PaymentStatus.captured,
            settlement_id=razorpay_id("setl"),
            settlement_date=settle_date,
            settlement_utr=utr,
            fee=fee,
            tax=tax,
            net_amount=net_amount,
            error_type=error_type,
        )

    def _make_bank_txn(self, pay: RazorpayPayment, error_type: ErrorType) -> BankTxn:
        value_date = pay.settlement_date
        amount = pay.net_amount

        if error_type == ErrorType.amount_delta:
            # Mutate net_amount by ±1-5 INR (fee rounding simulation)
            delta = Decimal(str(round(random.uniform(1, 5), 2)))
            if random.random() < 0.5:
                delta = -delta
            amount = (pay.net_amount + delta).quantize(Decimal("0.01"))
            # Ensure amount stays positive
            if amount <= 0:
                amount = pay.net_amount + abs(delta)

        elif error_type == ErrorType.date_slip:
            # Mutate value_date by ±1-2 days
            slip = random.randint(1, 2) * random.choice([-1, 1])
            value_date = pay.settlement_date + datetime.timedelta(days=slip)

        return BankTxn(
            txn_id=_short_id("btxn"),
            value_date=value_date,
            amount=amount,
            description=f"NEFT CR {pay.settlement_utr} AI FINANCE CO LTD",
            bank_ref=pay.settlement_utr,
            currency="INR",
            settlement_id=pay.settlement_id,
        )

    def _make_ledger_entries(
        self, pay: RazorpayPayment, error_type: ErrorType
    ) -> list[LedgerEntry]:
        if error_type == ErrorType.split:
            # Split one settlement into 2-3 ledger entries that sum to net_amount
            parts = random.randint(2, 3)
            base = (pay.net_amount / parts).quantize(Decimal("0.01"))
            amounts: list[Decimal] = [base] * (parts - 1)
            amounts.append((pay.net_amount - base * (parts - 1)).quantize(Decimal("0.01")))
            entries = []
            for amt in amounts:
                entries.append(
                    LedgerEntry(
                        entry_id=_short_id("ent"),
                        date=pay.settlement_date,
                        amount=amt,
                        narration=f"Razorpay settle {pay.settlement_id} (split)",
                        account_code=random.choice(_COA_CODES),
                        internal_ref=pay.settlement_id,
                    )
                )
            return entries

        # All other cases: single ledger entry
        return [
            LedgerEntry(
                entry_id=_short_id("ent"),
                date=pay.settlement_date,
                amount=pay.net_amount,
                narration=f"Razorpay settle {pay.settlement_id}",
                account_code=random.choice(_COA_CODES),
                internal_ref=pay.settlement_id,
            )
        ]


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_cached_batch: Optional[DataBatch] = None


def get_batch(seed: int = SEED) -> DataBatch:
    """
    Return the cached synthetic batch.  Generates once and caches in process memory.
    Call reset_batch() to regenerate (useful in tests).
    """
    global _cached_batch
    if _cached_batch is None:
        _cached_batch = BatchGenerator(seed=seed).generate()
    return _cached_batch


def reset_batch() -> None:
    """Clear the cached batch so the next call to get_batch() regenerates it."""
    global _cached_batch
    _cached_batch = None
