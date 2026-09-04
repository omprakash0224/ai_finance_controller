"""
backend/worker/chunker.py
==========================
Splits a DataBatch into equal-sized chunks for parallel Celery processing.

Chunking strategy
-----------------
Payments are the primary work unit for reconciliation, so chunks are sized
by payment count.  Bank transactions, ledger entries, and settlements that
share a settlement_id with payments in a given chunk are included with that
chunk so the SQL bulk JOIN operations see a consistent local subset.

Why chunking matters
--------------------
For 1,000,000 payments processed in a single serial task:
  - Reconciliation: ~5 seconds  (bulk SQL — already fast)
  - Tax tagging:    ~1 second   (pure SQL CASE expression)
  - Forecasting:    ~1 second   (SQL window function)
  Total: ~7 seconds serial

With 8 parallel chunks of 125,000 payments each:
  - All chunks reconcile concurrently on 8 Celery workers
  - Total wall-clock time: ~1-2 seconds
  - The finalize task assembles the aggregate report

Serialisation for Celery
-------------------------
Celery tasks must be JSON-serialisable.  DataBatch contains Pydantic models,
so each chunk is serialised using Pydantic's model_dump() with mode='json'
(which converts dates to ISO-8601 strings and Decimals to floats automatically).
The worker receives raw dicts and reconstructs DataBatch via model_validate().
"""

from __future__ import annotations

import math
from typing import Iterator

from data.schema import DataBatch, RazorpayPayment, BankTxn, LedgerEntry, Settlement


def chunk_batch(batch: DataBatch, chunk_size: int) -> list[dict]:
    """
    Split a DataBatch into a list of JSON-serialisable chunk dicts.

    Each chunk is a plain dict (Pydantic model_dump) ready to be passed as a
    Celery task argument over Redis.  The deserialisation back to DataBatch
    happens inside the worker via DataBatch.model_validate().

    Parameters
    ----------
    batch      : The full DataBatch to split.
    chunk_size : Maximum number of payments per chunk.

    Returns
    -------
    A list of dicts, each representing one DataBatch chunk.

    Example
    -------
    >>> chunks = chunk_batch(full_batch, chunk_size=5000)
    >>> len(chunks)         # ceil(len(full_batch.payments) / 5000)
    200
    >>> len(chunks[0]['payments'])
    5000
    """
    payments = batch.payments

    if not payments:
        return []

    num_chunks = math.ceil(len(payments) / chunk_size)

    # Index bank txns, ledger entries, and settlements by settlement_id for fast lookup
    bank_by_setl: dict[str, list[BankTxn]] = {}
    for t in batch.bank_txns:
        sid = t.settlement_id or ""
        bank_by_setl.setdefault(sid, []).append(t)

    ledger_by_ref: dict[str, list[LedgerEntry]] = {}
    for e in batch.ledger_entries:
        ledger_by_ref.setdefault(e.internal_ref, []).append(e)

    settlements_by_id: dict[str, Settlement] = {
        s.settlement_id: s for s in batch.settlements
    }

    chunks: list[dict] = []

    for i in range(num_chunks):
        start = i * chunk_size
        end   = min(start + chunk_size, len(payments))
        chunk_payments = payments[start:end]

        # Collect settlement_ids referenced in this chunk
        chunk_setl_ids: set[str] = {p.settlement_id for p in chunk_payments}
        chunk_pay_ids:  set[str] = {p.pay_id        for p in chunk_payments}

        # Gather related bank txns for this chunk's settlement IDs
        chunk_bank: list[BankTxn] = []
        for sid in chunk_setl_ids:
            chunk_bank.extend(bank_by_setl.get(sid, []))

        # Gather related ledger entries (keyed by settlement_id OR pay_id)
        chunk_ledger: list[LedgerEntry] = []
        for sid in chunk_setl_ids:
            chunk_ledger.extend(ledger_by_ref.get(sid, []))
        for pid in chunk_pay_ids:
            chunk_ledger.extend(ledger_by_ref.get(pid, []))

        # Deduplicate ledger entries (same entry_id can appear under multiple refs)
        seen_entry_ids: set[str] = set()
        unique_ledger: list[LedgerEntry] = []
        for e in chunk_ledger:
            if e.entry_id not in seen_entry_ids:
                seen_entry_ids.add(e.entry_id)
                unique_ledger.append(e)

        # Gather related settlements
        chunk_settlements: list[Settlement] = [
            settlements_by_id[sid]
            for sid in chunk_setl_ids
            if sid in settlements_by_id
        ]

        chunk_batch_obj = DataBatch(
            payments       = chunk_payments,
            bank_txns      = chunk_bank,
            ledger_entries = unique_ledger,
            settlements    = chunk_settlements,
        )

        # Serialise to JSON-safe dict for Celery
        chunks.append({
            "chunk_index": i,
            "num_chunks":  num_chunks,
            "total_payments_in_chunk": len(chunk_payments),
            "data": chunk_batch_obj.model_dump(mode="json"),
        })

    return chunks


def deserialise_chunk(chunk_dict: dict) -> tuple[int, int, DataBatch]:
    """
    Reconstruct a DataBatch from a JSON-serialised chunk dict.

    Parameters
    ----------
    chunk_dict : dict produced by chunk_batch()

    Returns
    -------
    (chunk_index, num_chunks, DataBatch)
    """
    chunk_index = chunk_dict["chunk_index"]
    num_chunks  = chunk_dict["num_chunks"]
    batch       = DataBatch.model_validate(chunk_dict["data"])
    return chunk_index, num_chunks, batch


def estimate_chunk_count(total_payments: int, chunk_size: int) -> int:
    """Return the number of chunks that would be produced for a given dataset size."""
    return math.ceil(total_payments / chunk_size) if total_payments > 0 else 0
