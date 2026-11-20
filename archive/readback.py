from __future__ import annotations

from archive.receipts import Receipt, SharedReceipt
from archive.store import ArchiveStore


def read_receipt(store: ArchiveStore, receipt_id: int) -> Receipt:
    receipt = store.find_receipt(receipt_id)
    if receipt is None:
        raise LookupError(f'unknown receipt {receipt_id!r}')
    return receipt


def read_shared_receipt(store: ArchiveStore, receipt_id: int) -> SharedReceipt | None:
    """Return the shared group for a member identifier; legacy single ids give None."""
    if type(receipt_id) is not int or not 0 < receipt_id < 2**63:
        raise LookupError(f'unknown receipt {receipt_id!r}')
    group = store.find_shared_group(receipt_id)
    if group is None and store.find_receipt(receipt_id) is None:
        raise LookupError(f'unknown receipt {receipt_id!r}')
    return group
