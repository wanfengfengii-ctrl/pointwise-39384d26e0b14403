from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

_RECEIPT_KINDS = ('single', 'shared')


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        'SELECT 1 FROM sqlite_master WHERE type = \'table\' AND name = ?', (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _create_shared_tables(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS shared_orders (
            order_id TEXT NOT NULL PRIMARY KEY,
            home_shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
            other_shift_id TEXT NOT NULL REFERENCES shifts(shift_id)
        )
    ''')


def _create_receipt_indexes(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS receipts_single_order
        ON receipts (order_id) WHERE kind = 'single'
    ''')
    connection.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS receipts_shared_member
        ON receipts (order_id, shift_id) WHERE kind = 'shared'
    ''')
    connection.execute('''
        CREATE INDEX IF NOT EXISTS receipts_by_shift_issue
        ON receipts (shift_id, receipt_id)
    ''')


def _create_fresh_schema(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            shift_id TEXT NOT NULL PRIMARY KEY,
            configuration TEXT NOT NULL
        )
    ''')
    connection.execute(f'''
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
            order_id TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'single'
                CHECK (kind IN ('single', 'shared'))
        )
    ''')
    _create_receipt_indexes(connection)
    _create_shared_tables(connection)


def _migrate_legacy_receipts(connection: sqlite3.Connection) -> None:
    """Rebuild the V1 receipts table, keeping every persisted identifier."""
    connection.execute('''
        CREATE TABLE receipts_v2 (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
            order_id TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('single', 'shared'))
        )
    ''')
    connection.execute('''
        INSERT INTO receipts_v2 (receipt_id, shift_id, order_id, body)
        SELECT receipt_id, shift_id, order_id, body FROM receipts
    ''')
    connection.execute('DROP TABLE receipts')
    connection.execute('ALTER TABLE receipts_v2 RENAME TO receipts')
    _create_receipt_indexes(connection)
    _create_shared_tables(connection)


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute('PRAGMA foreign_keys = ON')
    legacy_receipts = (
        _table_exists(connection, 'receipts')
        and 'kind' not in _table_columns(connection, 'receipts')
    )
    with write_transaction(connection):
        if legacy_receipts:
            _migrate_legacy_receipts(connection)
        else:
            _create_fresh_schema(connection)
