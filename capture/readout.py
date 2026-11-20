from __future__ import annotations

from archive.readback import read_receipt, read_shared_receipt
from archive.receipts import Receipt, SharedReceipt
from archive.store import ArchiveStore
from capture.intake import prepare_receipt, prepare_shared_receipts
from capture.orders import (
    ReadKind,
    ReadOrder,
    ReadOrderError,
    ReadOrders,
    ReadStage,
    new_order_id,
)
from capture.replies import ReplyError, validate_reply
from shift.configuration import ShiftConfig
from terminal.device import DeviceReply
from terminal.selection import ManualTerminal


class Readout:
    def __init__(self, terminal: ManualTerminal, archive: ArchiveStore):
        terminal.claim_readout()
        self._terminal = terminal
        self._archive = archive
        self._orders = ReadOrders()

    def begin_read(self) -> ReadOrder:
        self._orders.require_available()
        shift = self._terminal.shift_config()
        order_id = new_order_id()
        latched = self._terminal.begin_device_read(order_id)
        return self._orders.add_latched(order_id, shift, latched)

    def begin_shared_read(self, other_shift_id: str) -> ReadOrder:
        shift = self._terminal.shift_config()
        other_shift = self._qualify_shared_shift(shift, other_shift_id)
        self._orders.require_available()
        order_id = new_order_id()
        latched = self._terminal.begin_device_read(order_id)
        return self._orders.add_shared_latched(order_id, shift, other_shift, latched)

    def _qualify_shared_shift(
        self, home: ShiftConfig, other_shift_id: object,
    ) -> ShiftConfig:
        if not isinstance(other_shift_id, str) or not other_shift_id.strip():
            raise ReadOrderError('a shared read requires a registered peer shift identifier')
        if other_shift_id == home.shift_id:
            raise ReadOrderError('a shared read requires two distinct shifts')
        record = self._archive.load_shift_record(other_shift_id)
        if record is None:
            raise ReadOrderError(f'peer shift {other_shift_id!r} is not registered in the archive')
        other = ShiftConfig.from_record(record)
        current = self._terminal.current_position()
        if current is None:
            raise ReadOrderError('select a position before requesting a shared read')
        if not home.catalog.contains(current):
            raise ReadOrderError(
                f'device position is outside the home shift roster: {current!r}')
        if not other.catalog.contains(current):
            raise ReadOrderError(
                f'peer shift {other_shift_id!r} does not cover the current lane {current!r}')
        return other

    def finish_read(self, order_id: str, reply: DeviceReply | None = None) -> ReadStage | Receipt:
        order = self._orders.get(order_id)
        if order.kind is ReadKind.SHARED:
            raise ReadOrderError(
                f'order {order_id!r} is a shared read; use finish_shared_read')
        if order.stage is ReadStage.SIGNED:
            return self._replay(order, reply)
        order = self._orders.require_pending(order_id)
        if reply is None and order.reply is None:
            reply = self._terminal.poll_device_reply(order_id)
            if reply is None:
                return ReadStage.PENDING
        if reply is not None:
            validated = validate_reply(order.latched, reply)
            order = self._orders.record_reply(order_id, validated)
        receipt = self._archive.commit_receipt(prepare_receipt(order))
        self._orders.mark_signed(order_id, receipt)
        return receipt

    def finish_shared_read(
        self, order_id: str, reply: DeviceReply | None = None,
    ) -> ReadStage | SharedReceipt:
        order = self._orders.get(order_id)
        if order.kind is not ReadKind.SHARED:
            raise ReadOrderError(
                f'order {order_id!r} is not a shared read; use finish_read')
        if order.stage is ReadStage.SIGNED:
            return self._replay_shared(order, reply)
        order = self._orders.require_pending(order_id)
        if reply is None and order.reply is None:
            reply = self._terminal.poll_device_reply(order_id)
            if reply is None:
                return ReadStage.PENDING
        if reply is not None:
            validated = validate_reply(order.latched, reply)
            order = self._orders.record_reply(order_id, validated)
        home_draft, other_draft = prepare_shared_receipts(order)
        group = self._archive.commit_shared_receipts(order_id, home_draft, other_draft)
        self._orders.mark_shared_signed(order_id, group)
        return group

    def _replay(self, order: ReadOrder, reply: DeviceReply | None) -> Receipt:
        if reply is not None and validate_reply(order.latched, reply) != order.reply:
            raise ReplyError(f'conflicting replay for order {order.order_id!r}')
        if order.receipt_id is None:
            raise RuntimeError(f'signed order {order.order_id!r} lacks a receipt identifier')
        return read_receipt(self._archive, order.receipt_id)

    def _replay_shared(self, order: ReadOrder, reply: DeviceReply | None) -> SharedReceipt:
        if reply is not None and validate_reply(order.latched, reply) != order.reply:
            raise ReplyError(f'conflicting replay for order {order.order_id!r}')
        if order.receipt_id is None:
            raise RuntimeError(f'signed order {order.order_id!r} lacks a receipt identifier')
        group = read_shared_receipt(self._archive, order.receipt_id)
        if group is None:
            raise RuntimeError(
                f'signed shared order {order.order_id!r} has no archived member group')
        return group

    def cancel_read(self, order_id: str) -> ReadStage:
        return self._orders.cancel(order_id)
