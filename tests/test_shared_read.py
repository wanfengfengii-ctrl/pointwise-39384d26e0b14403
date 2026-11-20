import sqlite3
import tempfile
import unittest
from contextlib import closing
from fractions import Fraction
from pathlib import Path

from archive.export import export_shift
from archive.readback import read_receipt, read_shared_receipt
from archive.store import ArchiveStore
from capture.orders import ReadOrderError, ReadStage
from capture.readout import Readout
from capture.replies import ReplyError
from quality.limits import Verdict
from quality.units import Quantity
from shared_examples import CHECK_SHIFT, SHARED_POSITION, open_example, reject_insert_commit
from shift.catalog import Position
from terminal.device import DeviceReply


class SharedReadTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(directory) / 'shared.sqlite'
        self.store = self.enterContext(ArchiveStore.open(self.database))
        self.day, self.check, self.device, self.terminal, self.readout = open_example(self.store)

    def _select_a2(self):
        self.terminal.select('A')
        self.assertEqual(self.terminal.select(), SHARED_POSITION)

    def _begin_shared(self):
        self._select_a2()
        order = self.readout.begin_shared_read(CHECK_SHIFT)
        self.assertEqual(order.position, SHARED_POSITION)
        return order

    def _sign_shared(self, raw=6):
        order = self._begin_shared()
        self.device.deliver_reply(order.order_id, raw)
        group = self.readout.finish_shared_read(order.order_id)
        return order, group

    def test_qualification_and_shared_read_slot(self):
        # The peer only covers A/2: B/1 cannot issue a shared read, and the
        # failed qualification must not occupy the read slot or call the device.
        self.terminal.select('B')
        with self.assertRaises(ReadOrderError):
            self.readout.begin_shared_read(CHECK_SHIFT)
        self.assertEqual(self.device.reads, [])
        order = self.readout.begin_read()
        self.assertEqual(order.position.profile, 'B')
        self.readout.cancel_read(order.order_id)
        single_reads = len(self.device.reads)

        for bad_shift in ('CW-NOPE', self.day.shift_id, '', '   '):
            with self.subTest(bad_shift=bad_shift):
                with self.assertRaises(ReadOrderError):
                    self.readout.begin_shared_read(bad_shift)
        # Failed qualification never reaches the device.
        self.assertEqual(len(self.device.reads), single_reads)

        self._select_a2()
        shared = self.readout.begin_shared_read(CHECK_SHIFT)
        # Exactly one new device read was issued for the shared order.
        self.assertEqual(len(self.device.reads), single_reads + 1)
        # Single and shared orders share the one in-progress position.
        with self.assertRaises(ReadOrderError):
            self.readout.begin_read()
        with self.assertRaises(ReadOrderError):
            self.readout.begin_shared_read(CHECK_SHIFT)
        # Wrong finish entry is rejected while the work item stays pending.
        with self.assertRaises(ReadOrderError):
            self.readout.finish_read(shared.order_id)
        self.assertIs(self.readout.finish_shared_read(shared.order_id), ReadStage.PENDING)

    def test_same_read_different_verdicts(self):
        order, group = self._sign_shared(6)
        home, other = group.receipts
        self.assertEqual(group.order_id, order.order_id)
        self.assertEqual(group.position, SHARED_POSITION)
        self.assertEqual(group.raw, 6)
        self.assertEqual((home.shift_id, other.shift_id),
                         (self.day.shift_id, CHECK_SHIFT))
        # Both shifts convert the same raw code with their own frozen profile.
        self.assertEqual(home.measurement.quantity, Quantity(Fraction(3), 'kPa'))
        self.assertEqual(other.measurement.quantity, Quantity(Fraction(3), 'kPa'))
        self.assertIs(home.measurement.verdict, Verdict.PASS)
        self.assertIs(other.measurement.verdict, Verdict.OUTSIDE)
        self.assertEqual(other.measurement.calibration.upper, Fraction(5, 2))
        # Outside-limit evidence is still issued, with distinct ids in the
        # original receipt numbering space and one real device order id.
        self.assertNotEqual(home.receipt_id, other.receipt_id)
        self.assertEqual(home.order_id, other.order_id)
        self.assertEqual(home.position, other.position)
        self.assertEqual(home.raw, other.raw)
        # Both members are visible together only after the joint commit.
        self.assertEqual(export_shift(self.store, self.day.shift_id), [home])
        self.assertEqual(export_shift(self.store, CHECK_SHIFT), [other])
        self.assertEqual(read_receipt(self.store, home.receipt_id), home)
        self.assertEqual(read_receipt(self.store, other.receipt_id), other)
        self.assertEqual(read_shared_receipt(self.store, home.receipt_id), group)
        self.assertEqual(read_shared_receipt(self.store, other.receipt_id), group)

    def test_delayed_reply_uses_latched_position(self):
        order = self._begin_shared()
        self.assertIs(self.readout.finish_shared_read(order.order_id), ReadStage.PENDING)
        self.assertEqual(self.terminal.select('B').profile, 'B')
        self.device.deliver_reply(order.order_id, 6)
        group = self.readout.finish_shared_read(order.order_id)
        self.assertEqual(group.position, SHARED_POSITION)
        self.assertEqual(self.device.position().profile, 'B')
        for member in group.receipts:
            self.assertEqual(member.position, SHARED_POSITION)
            self.assertEqual(member.order_id, order.order_id)

    def test_replay_accepts_same_evidence_only(self):
        order, group = self._sign_shared('6.0')
        # No-argument retry and identical re-delivery return the same group/ids.
        self.assertEqual(self.readout.finish_shared_read(order.order_id), group)
        identical = DeviceReply(order.order_id, SHARED_POSITION, '6.0')
        self.assertEqual(self.readout.finish_shared_read(order.order_id, identical), group)
        # Integer 6 and text "6.0" are different evidence even when equal in value.
        with self.assertRaises(ReplyError):
            self.readout.finish_shared_read(
                order.order_id, DeviceReply(order.order_id, SHARED_POSITION, 6))
        # Wrong order id, wrong lane, and the single-shift entry are all refused.
        with self.assertRaises(ReplyError):
            self.readout.finish_shared_read(
                order.order_id, DeviceReply('other-order', SHARED_POSITION, '6.0'))
        with self.assertRaises(ReplyError):
            self.readout.finish_shared_read(
                order.order_id, DeviceReply(order.order_id, Position('B', 1), '6.0'))
        with self.assertRaises(ReadOrderError):
            self.readout.finish_read(order.order_id)
        # The archived group remains the original one.
        self.assertEqual(read_shared_receipt(self.store, group.receipts[0].receipt_id), group)

    def test_single_and_shared_entries_keep_their_binding(self):
        _, group = self._sign_shared(6)
        shared_order_id = group.order_id
        # The legacy single-piece write entry must refuse an already signed
        # shared order, and the shared entry must refuse a single order.
        home = group.receipts[0]
        from archive.receipts import ReceiptDraft
        draft = ReceiptDraft(home.shift_id, home.order_id, home.position,
                             home.raw, home.measurement)
        with self.assertRaises(ValueError):
            self.store.commit_receipt(draft)
        self.terminal.select('B')
        single = self.readout.begin_read()
        self.device.deliver_reply(single.order_id, 5)
        receipt = self.readout.finish_read(single.order_id)
        with self.assertRaises(ReadOrderError):
            self.readout.finish_shared_read(single.order_id)
        # A legacy member id is not a shared group; unknown ids still error.
        self.assertIsNone(read_shared_receipt(self.store, receipt.receipt_id))
        with self.assertRaises(LookupError):
            read_shared_receipt(self.store, receipt.receipt_id + 999)
        with self.assertRaises(LookupError):
            read_receipt(self.store, receipt.receipt_id + 999)

    def test_joint_commit_failure_keeps_todo_and_evidence(self):
        first_order, first_group = self._sign_shared(6)
        order2 = self._begin_shared()
        self.device.deliver_reply(order2.order_id, 7)
        with (
            ArchiveStore.open(self.database) as observer,
            closing(sqlite3.connect(self.database)) as admin,
        ):
            with reject_insert_commit(admin, 'receipts'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'FOREIGN KEY'):
                    self.readout.finish_shared_read(order2.order_id)
                # Nothing from the failed joint write is visible.
                self.assertEqual(export_shift(observer, self.day.shift_id),
                                 [first_group.receipts[0]])
                self.assertEqual(export_shift(observer, CHECK_SHIFT),
                                 [first_group.receipts[1]])
            # Fix the fault and continue with the original order: no new read.
            group2 = self.readout.finish_shared_read(order2.order_id)
        self.assertEqual(len(self.device.reads), 2)
        self.assertEqual(group2.raw, 7)
        self.assertEqual(group2.order_id, order2.order_id)
        home2, other2 = group2.receipts
        self.assertIs(home2.measurement.verdict, Verdict.OUTSIDE)
        self.assertIs(other2.measurement.verdict, Verdict.OUTSIDE)
        self.assertEqual(export_shift(self.store, self.day.shift_id),
                         [first_group.receipts[0], home2])
        self.assertEqual(export_shift(self.store, CHECK_SHIFT),
                         [first_group.receipts[1], other2])

    def test_cancelled_shared_order_rejects_late_reply(self):
        self._select_a2()
        old = self.readout.begin_shared_read(CHECK_SHIFT)
        self.assertIs(self.readout.cancel_read(old.order_id), ReadStage.CANCELLED)
        late = self.device.deliver_reply(old.order_id, 6)
        with self.assertRaises(ReadOrderError):
            self.readout.finish_shared_read(old.order_id, late)
        with self.assertRaises(ReadOrderError):
            self.readout.finish_shared_read(old.order_id)
        with self.assertRaises(ReadOrderError):
            self.readout.cancel_read(old.order_id)
        self.assertEqual(export_shift(self.store, CHECK_SHIFT), [])
        # The slot is released and a replacement shared read signs normally.
        replacement = self.readout.begin_shared_read(CHECK_SHIFT)
        self.device.deliver_reply(replacement.order_id, 6)
        group = self.readout.finish_shared_read(replacement.order_id)
        self.assertEqual([m.measurement.verdict for m in group.receipts],
                         [Verdict.PASS, Verdict.OUTSIDE])

    def test_stop_and_reopen_member_lookup(self):
        order, group = self._sign_shared(6)
        home, other = group.receipts
        self.device.stop()
        self.store.close()
        with self.assertRaisesRegex(RuntimeError, 'device is stopped'):
            self.device.position()
        with ArchiveStore.open(self.database) as reopened:
            # Either member id returns the same group; by-id and by-shift work.
            self.assertEqual(read_shared_receipt(reopened, home.receipt_id), group)
            self.assertEqual(read_shared_receipt(reopened, other.receipt_id), group)
            self.assertEqual(read_receipt(reopened, home.receipt_id), home)
            self.assertEqual(read_receipt(reopened, other.receipt_id), other)
            self.assertEqual(export_shift(reopened, self.day.shift_id), [home])
            self.assertEqual(export_shift(reopened, CHECK_SHIFT), [other])
            with self.assertRaises(LookupError):
                export_shift(reopened, 'CW-UNKNOWN')

    def test_legacy_v1_database_keeps_numbers_and_behavior(self):
        # Build an actual V1 database with the original schema and data.
        self.store.close()
        with closing(sqlite3.connect(self.database)) as admin:
            admin.execute('DROP TABLE IF EXISTS receipts')
            admin.execute('DROP TABLE IF EXISTS shifts')
            admin.executescript('''
                CREATE TABLE shifts (
                    shift_id TEXT NOT NULL PRIMARY KEY,
                    configuration TEXT NOT NULL
                );
                CREATE TABLE receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
                    order_id TEXT NOT NULL UNIQUE,
                    body TEXT NOT NULL
                );
                CREATE INDEX receipts_by_shift_issue
                ON receipts (shift_id, receipt_id);
            ''')
        with ArchiveStore.open(self.database) as migrated:
            day, check, device, terminal, readout = open_example(migrated)
            terminal.select('A')
            terminal.select()
            legacy_order = readout.begin_read()
            device.deliver_reply(legacy_order.order_id, 6)
            legacy = readout.finish_read(legacy_order.order_id)
            self.assertEqual(legacy.receipt_id, 1)
            shared_order = readout.begin_shared_read(CHECK_SHIFT)
            device.deliver_reply(shared_order.order_id, 7)
            group = readout.finish_shared_read(shared_order.order_id)
            member_ids = sorted(member.receipt_id for member in group.receipts)
            # The shared members continue the original receipt-id space.
            self.assertEqual(member_ids, [2, 3])
            self.assertIsNone(read_shared_receipt(migrated, legacy.receipt_id))
            self.assertEqual(read_receipt(migrated, legacy.receipt_id), legacy)
            device.stop()
        with ArchiveStore.open(self.database) as reopened:
            self.assertEqual(read_receipt(reopened, 1), legacy)
            self.assertEqual(read_shared_receipt(reopened, 2), group)
            self.assertEqual(read_shared_receipt(reopened, 3), group)
            self.assertEqual([r.receipt_id for r in export_shift(reopened, day.shift_id)], [1, 2])
            self.assertEqual([r.receipt_id for r in export_shift(reopened, check.shift_id)], [3])


if __name__ == '__main__':
    unittest.main()
