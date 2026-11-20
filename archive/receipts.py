from __future__ import annotations

from dataclasses import dataclass

from quality.result import Measurement
from shift.catalog import Position


@dataclass(frozen=True, slots=True)
class ReceiptDraft:
    shift_id: str
    order_id: str
    position: Position
    raw: int | str
    measurement: Measurement


@dataclass(frozen=True, slots=True)
class Receipt(ReceiptDraft):
    receipt_id: int


@dataclass(frozen=True, slots=True)
class SharedReceipt:
    """Two shift receipts issued from one physical device read."""

    order_id: str
    position: Position
    raw: int | str
    receipts: tuple[Receipt, Receipt]

    def __post_init__(self) -> None:
        if len(self.receipts) != 2:
            raise ValueError('a shared receipt requires exactly two member receipts')
        home, other = self.receipts
        for member in self.receipts:
            if member.order_id != self.order_id or member.position != self.position:
                raise ValueError('shared member receipt does not match the common read order')
            if member.raw != self.raw:
                raise ValueError('shared member receipt does not carry the common raw evidence')
        if home.shift_id == other.shift_id:
            raise ValueError('a shared read requires two distinct shifts')


def seal_receipt(receipt_id: int, draft: ReceiptDraft) -> Receipt:
    return Receipt(draft.shift_id, draft.order_id, draft.position, draft.raw,
                   draft.measurement, receipt_id)


def seal_shared_receipt(home: Receipt, other: Receipt) -> SharedReceipt:
    return SharedReceipt(home.order_id, home.position, home.raw, (home, other))
