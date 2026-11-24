import json

from assembly.close import close
from issue.manifest_view import ManifestSlot
from reports.audit import audit
from reports.retrieve import get
from source.locations import ByteLocation
from source.recordings import record
from source.revisions import revise
from tests.support import HandoffCase


def whole_body_reference(parts, refs):
    body = b''.join(parts)
    locations = tuple((ref, offset) for part, ref in zip(parts, refs) for offset in range(len(part)))
    records = []
    current = []
    cursor = 0
    while cursor < len(body):
        octet = body[cursor]
        if octet == 27:
            current.append((body[cursor + 1], locations[cursor:cursor + 2]))
            cursor += 2
        elif octet == 10:
            records.append(tuple(current))
            current.clear()
            cursor += 1
        else:
            current.append((octet, locations[cursor:cursor + 1]))
            cursor += 1
    if current:
        raise AssertionError('reference fixtures must end at a record boundary')
    return tuple(records)


class AssemblyTests(HandoffCase):
    def test_same_right_piece_changes_boundary_and_preserves_both_origins(self):
        left = record(self.source, 'P', 'L', bytes.fromhex('07 1b')).ref
        plain = revise(self.source, 'P', 'L', bytes.fromhex('07')).ref
        right = record(self.source, 'P', 'R', bytes.fromhex('0a 0b 0a')).ref
        manifest, (left_wire, plain_wire, right_wire) = self.issue_fixed(
            'P', (ManifestSlot('L', (left, plain)), ManifestSlot('R', (right,)))
        )
        self.disconnect_source()
        shared, escaped_id, plain_id = self.receive_all((right_wire, left_wire, plain_wire))
        escaped_result, first = close(self.inventory, self.reports, manifest, [shared, escaped_id])
        plain_result, second = close(self.inventory, self.reports, manifest, [plain_id, shared])
        self.assertEqual(first.accepted_ids, (escaped_id, shared))
        self.assertEqual(second.accepted_ids, (plain_id, shared))
        self.assertEqual(escaped_result.selections[1], plain_result.selections[1])
        actual = [[list(row.values), row.total] for row in escaped_result.records]
        plain_actual = [[list(row.values), row.total] for row in plain_result.records]
        self.assertEqual(actual, [[[7, 10, 11], 28]])
        self.assertEqual(plain_actual, [[[7], 7], [[11], 11]])
        row = audit(self.reports, escaped_result.report_id, 1).record
        self.assertEqual(row, escaped_result.records[0])
        self.assertEqual(row.origins[0].locations, (ByteLocation(left, 0),))
        self.assertEqual(row.origins[1].locations, (ByteLocation(left, 1), ByteLocation(right, 0)))
        self.assertEqual(row.origins[2].locations, (ByteLocation(right, 1),))
        self.assertEqual(audit(self.reports, plain_result.report_id, 1).record.origins[0].locations,
                         (ByteLocation(plain, 0),))
        self.assertEqual(get(self.reports, escaped_result.report_id), (escaped_result, first))
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V5.seam_origins',
            'input': {'left': ['07 1b', '07'], 'shared_right': '0a 0b 0a'},
            'expected': {'escaped': [[[7, 10, 11], 28]], 'plain': [[[7], 7], [[11], 11]]},
            'actual': {'escaped': actual, 'plain': plain_actual},
            'downstream_expected': [[['P', 'L', 1], 1], [['P', 'R', 1], 0]],
            'downstream_actual': [position.to_list() for position in row.origins[1].locations],
            'code_paths': ['issue/segment_evidence.py', 'assembly/selection.py', 'assembly/framing.py',
                           'assembly/origins.py', 'assembly/totals.py', 'reports/audit.py'],
            'oracle': 'Manual concatenation and byte positions for the two left selections',
        }, separators=(',', ':'), sort_keys=True), flush=True)

    def test_reference_readings_for_all_two_cut_positions_and_empty_segments(self):
        data = bytes.fromhex('00 ff 1b 1b 0a 0a')
        cases = []
        for first in range(len(data) + 1):
            for second in range(first, len(data) + 1):
                parts = (data[:first], data[first:second], data[second:])
                roll = f'R-{first}-{second}'
                manifest, payloads, refs = self.issue_recording(roll, tuple(zip(('A', 'B', 'C'), parts)))
                cases.append((first, second, parts, manifest, payloads, refs))
        self.disconnect_source()
        for first, second, parts, manifest, payloads, refs in cases:
            with self.subTest(first=first, second=second):
                expected = whole_body_reference(parts, refs)
                accepted = self.receive_all(reversed(payloads))
                result, receipt = close(self.inventory, self.reports, manifest, accepted)
                self.assertEqual(receipt.accepted_ids, accepted[::-1])
                self.assertEqual(tuple(row.total for row in result.records), (282, 0))
                self.assertEqual(tuple(row.number for row in result.records), (1, 2))
                actual = tuple(
                    tuple((value, tuple((pos.revision, pos.offset) for pos in origin.locations))
                          for value, origin in zip(row.values, row.origins, strict=True))
                    for row in result.records
                )
                self.assertEqual(actual, expected)
                self.assertEqual(audit(self.reports, result.report_id, 2).record.values, ())

    def test_missing_duplicate_and_unknown_receipts_leave_no_results(self):
        manifest, payloads, _ = self.issue_recording('P', (('L', b'\x07'), ('R', b'\n')))
        self.disconnect_source()
        left, right = self.receive_all(payloads)
        for selection in ((left,), (left, left), (left, right + 100), (True, right)):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                close(self.inventory, self.reports, manifest, selection)
            self.assertEqual(self.report_counts(), (0, 0, 0))

    def test_wrong_manifest_with_same_revision_is_rejected(self):
        manifest, _, (ref,) = self.issue_recording('P', (('S', b'\x07\n'),))
        other, (payload,) = self.issue_fixed('P', (ManifestSlot('S', (ref,)),))
        self.assertNotEqual(manifest.manifest_id, other.manifest_id)
        self.disconnect_source()
        accepted, = self.receive_all((payload,))
        with self.assertRaisesRegex(ValueError, 'another frozen manifest'):
            close(self.inventory, self.reports, manifest, [accepted])
        self.assertEqual(self.report_counts(), (0, 0, 0))
        result, _ = close(self.inventory, self.reports, other, [accepted])
        self.assertEqual(result.records[0].values, (7,))

    def test_invalid_tail_refuses_entire_roll_even_after_completed_prefix(self):
        fixtures = []
        for roll, tail, error in (('ESC', b'\x1b', 'dangling escape'), ('OPEN', b'\x0b', 'unfinished record')):
            manifest, payloads, _ = self.issue_recording(roll, (('S', b'\x07\n' + tail), ('EMPTY', b'')))
            fixtures.append((manifest, payloads, error))
        self.disconnect_source()
        for manifest, payloads, error in fixtures:
            with self.subTest(roll=manifest.roll):
                accepted = self.receive_all(payloads)
                with self.assertRaisesRegex(ValueError, error):
                    close(self.inventory, self.reports, manifest, accepted)
                self.assertEqual(self.report_counts(), (0, 0, 0))

    def test_empty_body_produces_no_record(self):
        manifest, payloads, _ = self.issue_recording('P', (('A', b''), ('B', b'')))
        self.disconnect_source()
        snapshot, first = close(self.inventory, self.reports, manifest, self.receive_all(payloads))
        self.assertEqual(snapshot.records, ())
        self.assertEqual(self.report_counts(), (1, 0, 1))
        self.assertEqual(get(self.reports, snapshot.report_id), (snapshot, first))
