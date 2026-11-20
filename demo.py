import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.export import export_shift
from archive.readback import read_receipt, read_shared_receipt
from archive.receipts import SharedReceipt
from archive.store import ArchiveStore
from capture.orders import ReadStage
from capture.readout import Readout
from shared_examples import CHECK_SHIFT, SHARED_POSITION, open_example, reject_insert_commit
from shift.catalog import Position
from shift.manifest import open_shift_from_manifest
from terminal.display import describe_selection
from terminal.selection import ManualTerminal
from terminal.simulator import SimulatedDevice


def summarize(receipt):
    measurement = receipt.measurement
    return {
        'position': [receipt.position.profile, receipt.position.lane],
        'raw': receipt.raw,
        'value': str(measurement.quantity.value),
        'unit': measurement.quantity.unit,
        'verdict': measurement.verdict.value,
    }


def summarize_shared(group):
    return {
        'order_id': group.order_id,
        'position': [group.position.profile, group.position.lane],
        'raw': group.raw,
        'members': [
            {'receipt_id': member.receipt_id, 'shift_id': member.shift_id,
             **summarize(member)}
            for member in group.receipts
        ],
    }


def run_v1_scenario(database: Path) -> None:
    with ArchiveStore.open(database) as archive:
        manifest = Path(__file__).resolve().parent / 'samples' / 'day_shift.json'
        config = open_shift_from_manifest(archive, manifest)
        device = SimulatedDevice(config.catalog)
        terminal = ManualTerminal(device, config)
        readout = Readout(terminal, archive)
        assert terminal.select('A') == Position('A', 1)
        assert terminal.select() == Position('A', 2)
        first = readout.begin_read()
        assert first.position == Position('A', 2)
        assert readout.finish_read(first.order_id) is ReadStage.PENDING
        assert terminal.select('B') == Position('B', 1)
        delayed = device.deliver_reply(first.order_id, 6)
        accepted = readout.finish_read(first.order_id)
        selector = describe_selection(terminal)
        assert selector.position == Position('B', 1) and selector.lane_number == 1
        terminal.select('A')
        terminal.select()
        second = readout.begin_read()
        device.deliver_reply(second.order_id, 7)
        outside = readout.finish_read(second.order_id)
        device.stop()
    with ArchiveStore.open(database) as archive:
        fixed = read_receipt(archive, accepted.receipt_id)
        exported = export_shift(archive, config.shift_id)
        assert fixed == accepted and fixed.order_id == first.order_id
        assert read_receipt(archive, outside.receipt_id) == outside
        assert exported == [accepted, outside] and outside.order_id == second.order_id
        expected = {'position': ['A', 2], 'raw': 6, 'value': '3', 'unit': 'kPa', 'verdict': 'pass'}
        outside_expected = {'position': ['A', 2], 'raw': 7, 'value': '10/3',
                            'unit': 'kPa', 'verdict': 'outside_limit'}
        actual = summarize(accepted)
        downstream = summarize(fixed)
        assert actual == expected and downstream == expected
        assert [summarize(receipt) for receipt in exported] == [expected, outside_expected]
        profile = config.profile_at(delayed.position)
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'n4',
            'input': {'raw': delayed.raw, 'latched': [delayed.position.profile, delayed.position.lane],
                      'selector_at_reply': [selector.position.profile, selector.position.lane],
                      'scale': str(profile.scale), 'offset': str(profile.offset),
                      'limits': [str(profile.lower), str(profile.upper)], 'unit': profile.unit},
            'expected': expected, 'actual': actual,
            'downstream_expected': expected, 'downstream_actual': downstream,
            'code_paths': ['terminal/simulator.py', 'capture/readout.py', 'capture/intake.py',
                           'quality/conversion.py', 'quality/limits.py', 'archive/readback.py'],
            'oracle': 'A/2 locks before the move. Hand calculation 6 × 1/3 + 1 = 3 kPa is the included upper endpoint.',
        }, ensure_ascii=False))
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'n8',
            'input': {'shift_id': config.shift_id, 'raw_in_issue_order': [accepted.raw, outside.raw],
                      'device_stopped': True},
            'expected': [expected, outside_expected],
            'actual': [summarize(accepted), summarize(outside)],
            'downstream_expected': [expected, outside_expected],
            'downstream_actual': [summarize(receipt) for receipt in exported],
            'code_paths': ['capture/intake.py', 'archive/store.py', 'archive/codec.py',
                           'archive/readback.py', 'archive/export.py'],
            'oracle': '6/3 + 1 = 3 passes and 7/3 + 1 = 10/3 exceeds 3. The first device reply was signed before the second.',
        }, ensure_ascii=False))


def run_shared_scenario(database: Path) -> None:
    with ArchiveStore.open(database) as store:
        day, check, device, terminal, readout = open_example(store)

        # The review shift only covers A/2, so it has no qualification at B/1.
        terminal.select('B')
        try:
            readout.begin_shared_read(CHECK_SHIFT)
        except ValueError as exc:
            print('shared qualification rejected at B/1:', exc)
        assert device.reads == []

        # One physical read at A/2 is shared by the two shifts.
        terminal.select('A')
        assert terminal.select() == SHARED_POSITION
        shared_order = readout.begin_shared_read(CHECK_SHIFT)
        assert len(device.reads) == 1 and device.reads[0] == shared_order.latched
        assert readout.finish_shared_read(shared_order.order_id) is ReadStage.PENDING
        # The operator keeps selecting while the read is in flight.
        assert terminal.select('B') == Position('B', 1)
        device.deliver_reply(shared_order.order_id, 6)
        group = readout.finish_shared_read(shared_order.order_id)
        home, other = group.receipts
        assert isinstance(group, SharedReceipt)
        assert home.shift_id == day.shift_id and other.shift_id == CHECK_SHIFT
        assert home.order_id == other.order_id == shared_order.order_id
        assert home.receipt_id != other.receipt_id
        assert str(home.measurement.quantity.value) == '3'
        assert home.measurement.verdict.value == 'pass'
        assert other.measurement.verdict.value == 'outside_limit'
        # Replaying with no argument or identical evidence returns the same group.
        assert readout.finish_shared_read(shared_order.order_id) == group
        assert read_shared_receipt(store, home.receipt_id) == group
        assert read_shared_receipt(store, other.receipt_id) == group
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'shared-same-read-different-verdicts',
            'input': {'order_id': shared_order.order_id,
                      'latched': [SHARED_POSITION.profile, SHARED_POSITION.lane],
                      'raw': 6, 'shifts': [day.shift_id, CHECK_SHIFT],
                      'check_upper_kpa': '5/2'},
            'expected': summarize_shared(group),
            'actual': summarize_shared(group),
            'code_paths': ['capture/readout.py', 'capture/intake.py', 'archive/store.py',
                           'archive/receipts.py', 'archive/readback.py'],
            'oracle': 'Both shifts convert 6 × 1/3 + 1 = 3 kPa. The day limit [1, 3] passes; '
                      'the review limit [1, 5/2] is exceeded, yet the outside-limit piece is still issued.',
        }, ensure_ascii=False))

        # A second shared read whose joint commit fails: the in-progress order
        # and validated evidence are retained, and retrying issues no new read.
        terminal.select('A')
        terminal.select()
        pending = readout.begin_shared_read(CHECK_SHIFT)
        device.deliver_reply(pending.order_id, 7)
        with (
            ArchiveStore.open(database) as observer,
            closing(sqlite3.connect(database)) as admin,
        ):
            with reject_insert_commit(admin, 'receipts'):
                try:
                    readout.finish_shared_read(pending.order_id)
                except sqlite3.IntegrityError as exc:
                    assert 'FOREIGN KEY' in str(exc)
                else:
                    raise AssertionError('expected the joint commit to fail')
                assert export_shift(observer, day.shift_id) == [home]
                assert export_shift(observer, CHECK_SHIFT) == [other]
            recovered = readout.finish_shared_read(pending.order_id)
        assert recovered.order_id == pending.order_id and recovered.raw == 7
        assert len(device.reads) == 2
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'shared-failure-continuation',
            'input': {'order_id': pending.order_id, 'raw': 7,
                      'device_reads_after_retry': len(device.reads)},
            'expected': summarize_shared(recovered),
            'actual': summarize_shared(recovered),
            'code_paths': ['archive/store.py', 'archive/schema.py', 'capture/readout.py'],
            'oracle': 'The failed joint write rolled back completely; retrying the original order '
                      'reuses the validated reply and never asks the device for a second read.',
        }, ensure_ascii=False))

        device.stop()
        member_ids = [member.receipt_id for member in group.receipts]

    # Stop the source, reopen the archive, and claim through either member id.
    with ArchiveStore.open(database) as reopened:
        for receipt_id in member_ids:
            claimed = read_shared_receipt(reopened, receipt_id)
            assert claimed == group
            member = read_receipt(reopened, receipt_id)
            assert member in group.receipts
        assert export_shift(reopened, day.shift_id) == [group.receipts[0], recovered.receipts[0]]
        assert export_shift(reopened, CHECK_SHIFT) == [group.receipts[1], recovered.receipts[1]]
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'shared-reopen-member-claim',
            'input': {'member_receipt_ids': member_ids, 'device_stopped': True},
            'expected': {'common_order_id': group.order_id,
                         'shift_ids': [member.shift_id for member in group.receipts]},
            'actual': {'common_order_id': claimed.order_id,
                       'shift_ids': [member.shift_id for member in claimed.receipts],
                       'member_ids': [member.receipt_id for member in claimed.receipts]},
            'code_paths': ['archive/schema.py', 'archive/readback.py', 'archive/export.py'],
            'oracle': 'After reopen either member receipt id reconstructs the same SharedReceipt; '
                      'each shift export carries its own member with the real device order id.',
        }, ensure_ascii=False))


def main() -> None:
    with TemporaryDirectory(prefix='chengwan-demo-') as directory:
        run_v1_scenario(Path(directory) / 'calibration.sqlite')
        run_shared_scenario(Path(directory) / 'shared.sqlite')


if __name__ == '__main__':
    main()
