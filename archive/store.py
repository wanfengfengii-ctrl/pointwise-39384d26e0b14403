from __future__ import annotations

import json
import sqlite3
from os import PathLike

from archive.codec import decode_receipt, encode_receipt_body
from archive.receipts import (
    Receipt,
    ReceiptDraft,
    SharedReceipt,
    seal_receipt,
    seal_shared_receipt,
)
from archive.schema import write_transaction, initialize_schema


class ArchiveStore:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    @classmethod
    def open(cls, path: str | PathLike[str]) -> ArchiveStore:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            initialize_schema(connection)
        except BaseException:
            connection.close()
            raise
        return cls(connection)

    def __enter__(self) -> ArchiveStore:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def load_shift_record(self, shift_id: str) -> dict | None:
        if not isinstance(shift_id, str):
            return None
        row = self._connection.execute(
            'SELECT configuration FROM shifts WHERE shift_id = ?', (shift_id,),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def register_shift_record(self, record: dict) -> dict:
        shift_id = record['shift_id']
        payload = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        with write_transaction(self._connection):
            self._connection.execute('''
                INSERT INTO shifts (shift_id, configuration) VALUES (?, ?)
                ON CONFLICT (shift_id) DO NOTHING
            ''', (shift_id, payload))
            persisted = self.load_shift_record(shift_id)
            if persisted is None:
                raise sqlite3.DatabaseError(f'could not register shift {shift_id!r}')
        return persisted

    def commit_receipt(self, draft: ReceiptDraft) -> Receipt:
        body = encode_receipt_body(draft)
        with write_transaction(self._connection):
            prior = self._connection.execute('''
                SELECT receipt_id, shift_id, order_id, body, kind
                FROM receipts WHERE order_id = ?
            ''', (draft.order_id,)).fetchall()
            if prior:
                if len(prior) != 1 or prior[0][4] != 'single':
                    raise ValueError(
                        f'order {draft.order_id!r} already belongs to a shared read group')
                row = prior[0]
                if row[1] != draft.shift_id or row[3] != body:
                    raise ValueError(f'conflicting archived evidence for order {draft.order_id!r}')
                receipt = decode_receipt(row[0], row[1], row[2], row[3])
            else:
                result = self._connection.execute('''
                    INSERT INTO receipts (shift_id, order_id, body, kind)
                    VALUES (?, ?, ?, 'single')
                ''', (draft.shift_id, draft.order_id, body))
                receipt = seal_receipt(result.lastrowid, draft)
        return receipt

    def commit_shared_receipts(
        self, order_id: str, home: ReceiptDraft, other: ReceiptDraft,
    ) -> SharedReceipt:
        if home.shift_id == other.shift_id:
            raise ValueError('a shared read requires two distinct shifts')
        if home.order_id != order_id or other.order_id != order_id:
            raise ValueError('shared drafts must carry the common read order identifier')
        if home.position != other.position or home.raw != other.raw:
            raise ValueError('shared drafts must share the latched position and raw evidence')
        home_body = encode_receipt_body(home)
        other_body = encode_receipt_body(other)
        with write_transaction(self._connection):
            existing = self._connection.execute(
                'SELECT kind FROM receipts WHERE order_id = ?', (order_id,),
            ).fetchall()
            binding = self._connection.execute(
                'SELECT home_shift_id, other_shift_id FROM shared_orders WHERE order_id = ?',
                (order_id,),
            ).fetchone()
            if existing or binding is not None:
                group = self._load_shared_group(order_id)
                if group is None:
                    raise ValueError(
                        f'order {order_id!r} already has a non-shared archive entry')
                if binding != (home.shift_id, other.shift_id):
                    raise ValueError(f'conflicting shift binding for shared order {order_id!r}')
                stored = {member.shift_id: member for member in group.receipts}
                for draft, body in ((home, home_body), (other, other_body)):
                    member = stored.get(draft.shift_id)
                    if member is None or encode_receipt_body(
                            ReceiptDraft(member.shift_id, member.order_id, member.position,
                                         member.raw, member.measurement)) != body:
                        raise ValueError(
                            f'conflicting archived evidence for shared order {order_id!r}')
                return group
            self._connection.execute('''
                INSERT INTO shared_orders (order_id, home_shift_id, other_shift_id)
                VALUES (?, ?, ?)
            ''', (order_id, home.shift_id, other.shift_id))
            home_result = self._connection.execute('''
                INSERT INTO receipts (shift_id, order_id, body, kind)
                VALUES (?, ?, ?, 'shared')
            ''', (home.shift_id, order_id, home_body))
            other_result = self._connection.execute('''
                INSERT INTO receipts (shift_id, order_id, body, kind)
                VALUES (?, ?, ?, 'shared')
            ''', (other.shift_id, order_id, other_body))
            home_receipt = seal_receipt(home_result.lastrowid, home)
            other_receipt = seal_receipt(other_result.lastrowid, other)
        return seal_shared_receipt(home_receipt, other_receipt)

    def find_receipt(self, receipt_id: int) -> Receipt | None:
        if type(receipt_id) is not int or not 0 < receipt_id < 2**63:
            return None
        row = self._connection.execute('''
            SELECT receipt_id, shift_id, order_id, body FROM receipts WHERE receipt_id = ?
        ''', (receipt_id,)).fetchone()
        return None if row is None else decode_receipt(*row)

    def find_shared_group(self, receipt_id: int) -> SharedReceipt | None:
        if type(receipt_id) is not int or not 0 < receipt_id < 2**63:
            return None
        row = self._connection.execute('''
            SELECT order_id FROM receipts WHERE receipt_id = ? AND kind = 'shared'
        ''', (receipt_id,)).fetchone()
        if row is None:
            return None
        return self._load_shared_group(row[0])

    def _load_shared_group(self, order_id: str) -> SharedReceipt | None:
        binding = self._connection.execute('''
            SELECT home_shift_id, other_shift_id FROM shared_orders WHERE order_id = ?
        ''', (order_id,)).fetchone()
        if binding is None:
            return None
        home_shift_id, other_shift_id = binding
        rows = self._connection.execute('''
            SELECT receipt_id, shift_id, order_id, body FROM receipts
            WHERE order_id = ? AND kind = 'shared'
        ''', (order_id,)).fetchall()
        members = {row[1]: decode_receipt(*row) for row in rows}
        if home_shift_id not in members or other_shift_id not in members:
            raise sqlite3.DatabaseError(
                f'shared order {order_id!r} is missing a member receipt')
        return seal_shared_receipt(members[home_shift_id], members[other_shift_id])

    def list_shift_receipts(self, shift_id: str) -> list[Receipt]:
        rows = self._connection.execute('''
            SELECT receipt_id, shift_id, order_id, body FROM receipts
            WHERE shift_id = ? ORDER BY receipt_id
        ''', (shift_id,))
        return [decode_receipt(*row) for row in rows]
