from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.export import export_shift
from archive.store import ArchiveStore
from capture.orders import ReadStage
from capture.readout import Readout
from quality.limits import Verdict
from shift.catalog import Position, make_lane_catalog
from shift.manifest import open_shift_from_manifest
from shift.opening import open_shift
from terminal.device import LatchedRead
from terminal.selection import ManualTerminal
from terminal.simulator import SimulatedDevice


SAMPLE = Path(__file__).resolve().parent / "samples" / "day_shift.json"
CHECK_SHIFT = "CW-CHECK-01"
SHARED_POSITION = Position("A", 2)


class CountingDevice(SimulatedDevice):
    def __init__(self, catalog):
        super().__init__(catalog)
        self.reads: list[LatchedRead] = []

    def begin_read(self, order_id: str) -> LatchedRead:
        latched = super().begin_read(order_id)
        self.reads.append(latched)
        return latched


def open_example(store: ArchiveStore):
    day = open_shift_from_manifest(store, SAMPLE)
    check_catalog = make_lane_catalog([("A", [2])])
    check_profile = replace(day.profile_at(SHARED_POSITION), upper=Fraction(5, 2))
    check = open_shift(store, CHECK_SHIFT, check_catalog, {SHARED_POSITION: check_profile})
    device = CountingDevice(day.catalog)
    terminal = ManualTerminal(device, day)
    return day, check, device, terminal, Readout(terminal, store)


@contextmanager
def reject_insert_commit(admin: sqlite3.Connection, table: str = "receipts") -> Iterator[None]:
    if not table.isascii() or not table.isidentifier():
        raise ValueError("expected a simple SQLite table name")
    admin.executescript(f"""
        BEGIN;
        CREATE TABLE example_approved (id INTEGER PRIMARY KEY);
        CREATE TABLE example_guard (
            id INTEGER NOT NULL REFERENCES example_approved(id)
            DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TRIGGER example_reject_commit AFTER INSERT ON "{table}"
        BEGIN
            INSERT INTO example_guard VALUES (1);
        END;
        COMMIT;
    """)
    try:
        yield
    finally:
        admin.executescript("""
            BEGIN;
            DROP TRIGGER example_reject_commit;
            DROP TABLE example_guard;
            DROP TABLE example_approved;
            COMMIT;
        """)


def check_fixture(store: ArchiveStore, observer: ArchiveStore, admin: sqlite3.Connection) -> None:
    day, check, device, terminal, readout = open_example(store)
    assert check.profile_at(SHARED_POSITION).upper == Fraction(5, 2)
    terminal.select("A")
    assert terminal.select() == SHARED_POSITION
    order = readout.begin_read()
    assert readout.finish_read(order.order_id) is ReadStage.PENDING
    assert terminal.select("B") == Position("B", 1)
    device.deliver_reply(order.order_id, 6)
    with reject_insert_commit(admin):
        try:
            readout.finish_read(order.order_id)
        except sqlite3.IntegrityError as exc:
            if "FOREIGN KEY" not in str(exc):
                raise
        else:
            raise AssertionError("expected SQLite to reject the commit")
        assert export_shift(observer, day.shift_id) == []
        assert export_shift(observer, check.shift_id) == []
    receipt = readout.finish_read(order.order_id)
    assert len(device.reads) == 1 and device.reads[0] == order.latched
    assert receipt.position == SHARED_POSITION and receipt.raw == 6
    assert receipt.measurement.quantity.value == 3
    assert receipt.measurement.verdict is Verdict.PASS
    assert export_shift(observer, day.shift_id) == [receipt]
    assert export_shift(observer, check.shift_id) == []
    print(f"V1 fixture: {receipt.shift_id} A/2 raw={receipt.raw} verdict={receipt.measurement.verdict.value}")


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        database = Path(directory) / "example.sqlite"
        with (
            ArchiveStore.open(database) as store,
            ArchiveStore.open(database) as observer,
            closing(sqlite3.connect(database)) as admin,
        ):
            check_fixture(store, observer, admin)
