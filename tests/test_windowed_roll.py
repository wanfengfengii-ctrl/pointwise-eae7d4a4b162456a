import hashlib
import json
from pathlib import Path

from assembly.close import close, close_windowed
from issue.issued_store import issue_windowed, open_issued_store, reissue
from issue.manifest_view import ManifestSlot, WindowedManifest
from issue.manifests import freeze_windowed
from issue.wire_v2 import decode_v2
from receipt.intake import receive
from reports.snapshot_store import open_report_store
from source.locations import ByteLocation
from source.raw_store import open_source_store
from source.recordings import record
from source.revisions import revise
from tests.support import HandoffCase


SAMPLE = json.loads(
    (Path(__file__).resolve().parent.parent / 'samples' / 'windowed_roll.json').read_text('utf-8')
)


def digest_json(fields):
    body = {key: value for key, value in fields.items() if key != 'digest'}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


class WindowedSampleCase(HandoffCase):
    def freeze_sample(self, sample=SAMPLE):
        roll = sample['roll']
        slots = []
        windows = {}
        refs_by_segment = {}
        for segment_spec in sample['slots']:
            name = segment_spec['segment']
            refs = []
            for index, revision_spec in enumerate(segment_spec['revisions']):
                raw = bytes.fromhex(revision_spec['raw_hex'])
                if index == 0:
                    revision = record(self.source, roll, name, raw)
                else:
                    revision = revise(self.source, roll, name, raw)
                refs.append(revision.ref)
                windows[revision.ref] = {
                    win_name: tuple(bounds)
                    for win_name, bounds in revision_spec['windows'].items()
                }
            refs_by_segment[name] = tuple(refs)
            slots.append(ManifestSlot(name, tuple(refs)))
        manifest = freeze_windowed(self.source, self.issuer, roll, tuple(slots), windows)
        return WindowedManifest.from_bytes(manifest.to_bytes()), refs_by_segment

    def issue_sample_pieces(self, manifest, refs):
        payloads = {}
        for segment, segment_refs in refs.items():
            for ref in segment_refs:
                payloads[(segment, ref.revision, None)] = issue_windowed(
                    self.source, self.issuer, manifest.manifest_id, segment, ref.revision
                )
                binding = manifest.windowing_for(ref)
                for window in binding.windows:
                    payloads[(segment, ref.revision, window.name)] = issue_windowed(
                        self.source,
                        self.issuer,
                        manifest.manifest_id,
                        segment,
                        ref.revision,
                        window=window.name,
                    )
        return payloads

    def receive_keys(self, payloads, keys):
        return tuple(receive(self.inventory, payloads[key]) for key in keys)


class HandoverSampleTests(WindowedSampleCase):
    def test_missing_and_supplemented_windows_match_handover(self):
        manifest, refs = self.freeze_sample()
        payloads = self.issue_sample_pieces(manifest, refs)
        l1, l2 = refs['L']
        r1 = refs['R'][0]
        self.disconnect_source()

        r_boundary = receive(self.inventory, payloads[('R', 1, None)])
        r_mid = receive(self.inventory, payloads[('R', 1, 'mid')])
        r_tail = receive(self.inventory, payloads[('R', 1, 'tail')])
        r_first = receive(self.inventory, payloads[('R', 1, 'first')])
        l1_boundary = receive(self.inventory, payloads[('L', 1, None)])
        l1_left = receive(self.inventory, payloads[('L', 1, 'left')])
        l2_boundary = receive(self.inventory, payloads[('L', 2, None)])
        l2_left = receive(self.inventory, payloads[('L', 2, 'left')])

        r_missing = [r_boundary, r_mid, r_tail]

        l1_missing, first = close_windowed(
            self.inventory, self.reports, manifest,
            [l1_left, l1_boundary, r_tail, r_mid, r_boundary],
        )
        self.assertEqual(first.accepted_ids, tuple(s.accepted_id for s in l1_missing.selections))
        self.assertEqual(
            [(row.number, list(row.values), row.total) for row in l1_missing.records],
            [(1, [7], 7), (3, [9], 9)],
        )

        l2_missing, _ = close_windowed(
            self.inventory, self.reports, manifest,
            [l2_boundary, l2_left, r_boundary, r_mid, r_tail, r_boundary],
        )
        self.assertEqual(
            [(row.number, list(row.values), row.total) for row in l2_missing.records],
            [(1, [7], 7), (3, [32], 32), (4, [9], 9)],
        )

        l1_full, full_first = close_windowed(
            self.inventory, self.reports, manifest,
            [l1_boundary, l1_left, r_boundary, r_mid, r_tail, r_first, r_boundary],
        )
        self.assertEqual(
            [(row.number, list(row.values), row.total) for row in l1_full.records],
            [(1, [7], 7), (2, [10, 32], 42), (3, [9], 9)],
        )
        record_two = l1_full.records[1]
        self.assertEqual(record_two.number, 2)
        self.assertEqual(
            record_two.origins[0].locations,
            (ByteLocation(l1, 2), ByteLocation(r1, 0)),
        )
        self.assertEqual(
            record_two.origins[1].locations,
            (ByteLocation(r1, 1),),
        )
        self.assertEqual(
            full_first.accepted_ids, tuple(s.accepted_id for s in l1_full.selections)
        )
        # boundary first, then windows in canonical window order
        self.assertEqual(len(l1_full.selections), 6)

        l2_full, _ = close_windowed(
            self.inventory, self.reports, manifest,
            [l2_boundary, l2_left, r_boundary, r_first, r_mid, r_tail],
        )
        self.assertEqual(
            [(row.number, list(row.values)) for row in l2_full.records],
            [(1, [7]), (2, []), (3, [32]), (4, [9])],
        )
        empty_record = l2_full.records[1]
        self.assertEqual((empty_record.number, empty_record.values, empty_record.total), (2, (), 0))

    def test_reopen_reclaims_reports_and_audit_skips_missing_numbers(self):
        manifest, refs = self.freeze_sample()
        payloads = self.issue_sample_pieces(manifest, refs)
        self.disconnect_source()
        ids = self.receive_all(tuple(payloads.values()))
        # only L1 + R boundary/mid/tail (skip R first id)
        key_to_id = dict(zip(payloads.keys(), ids))
        chosen = [
            key_to_id[('L', 1, None)], key_to_id[('L', 1, 'left')],
            key_to_id[('R', 1, None)], key_to_id[('R', 1, 'mid')],
            key_to_id[('R', 1, 'tail')],
        ]
        snapshot, first = close_windowed(self.inventory, self.reports, manifest, chosen)
        report_id = snapshot.report_id
        self.reports.close()
        self.reports = self.open_db(open_report_store, 'reports.db')
        from reports.retrieve import get
        from reports.audit import audit

        reopened, reopened_first = get(self.reports, report_id)
        self.assertEqual(reopened, snapshot)
        self.assertEqual(reopened_first, first)
        self.assertEqual(audit(self.reports, report_id, 3).record.values, (9,))
        for skipped in (2, 4):
            with self.assertRaises(LookupError):
                audit(self.reports, report_id, skipped)


class SingleSegmentControlTests(WindowedSampleCase):
    def freeze_single(self, roll, hexes, body_start, body_end, window=(3, 5)):
        refs = []
        windows = {}
        for index, text in enumerate(hexes):
            raw = bytes.fromhex(text)
            if index == 0:
                revision = record(
                    self.source, roll, 'S', raw, body_start=body_start, body_end=body_end
                )
            else:
                revision = revise(
                    self.source, roll, 'S', raw, body_start=body_start, body_end=body_end
                )
            refs.append(revision.ref)
            windows[revision.ref] = {'w': tuple(window)}
        manifest = freeze_windowed(
            self.source, self.issuer, roll,
            (ManifestSlot('S', tuple(refs)),), windows,
        )
        return WindowedManifest.from_bytes(manifest.to_bytes()), tuple(refs)

    def receive_and_close(self, manifest, ref, *, window_name='w', with_window=True):
        boundary = issue_windowed(
            self.source, self.issuer, manifest.manifest_id, 'S', ref.revision
        )
        accepted = [receive(self.inventory, boundary)]
        if with_window:
            window_payload = issue_windowed(
                self.source, self.issuer, manifest.manifest_id, 'S', ref.revision,
                window=window_name,
            )
            accepted.append(receive(self.inventory, window_payload))
        return close_windowed(self.inventory, self.reports, manifest, accepted)

    def test_header_cut_two_revisions(self):
        manifest, refs = self.freeze_single(
            'CTRL-A',
            ['F0 0A 21 20 0A FF', 'F0 21 0A 20 0A FF'],
            1, 5, (3, 5),
        )
        first, _ = self.receive_and_close(manifest, refs[0])
        self.assertEqual(first.records, ())
        second, _ = self.receive_and_close(manifest, refs[1])
        self.assertEqual([(r.number, list(r.values)) for r in second.records], [(2, [32])])
        location = second.records[0].origins[0].locations[0]
        self.assertEqual(location, ByteLocation(refs[1], 3))

    def test_tail_dangling_escape_and_open_record_reject_whole_roll(self):
        for roll, text in (('ESC-TAIL', '07 0A 1B'), ('OPEN-TAIL', '07 0A 55')):
            manifest, refs = self.freeze_single(roll, [text], 0, 3, (0, 2))
            with self.assertRaises(ValueError):
                self.receive_and_close(manifest, refs[0])
            self.assertEqual(self.report_counts(), (0, 0, 0))

    def test_boundary_only_clean_roll_signs_valid_empty_result(self):
        manifest, refs = self.freeze_single('EMPTY-OK', ['07 0A'], 0, 2, (0, 2))
        snapshot, first = self.receive_and_close(manifest, refs[0], with_window=False)
        self.assertEqual(snapshot.records, ())
        self.assertEqual(len(snapshot.selections), 1)
        self.assertEqual(first.accepted_ids, tuple(s.accepted_id for s in snapshot.selections))
        self.assertEqual(self.report_counts()[0], 1)

    def test_lone_terminator_signs_empty_record_at_original_number(self):
        ref = record(self.source, 'LONE', 'S', bytes.fromhex('07 0A 0A 09 0A')).ref
        windows = {'e1': (1, 2), 'e2': (2, 3), 'v': (3, 5)}
        manifest = freeze_windowed(
            self.source, self.issuer, 'LONE',
            (ManifestSlot('S', (ref,)),), {ref: windows},
        )
        boundary = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1)
        e2 = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='e2')
        value_window = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='v')
        self.disconnect_source()
        ids = self.receive_all((boundary, e2, value_window))
        snapshot, _ = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual(
            [(row.number, row.values) for row in snapshot.records],
            [(2, ()), (3, (9,))],
        )

    def test_gap_consumes_escape_in_summary_and_does_not_misattach(self):
        # 1B escapes the missing 0A at offset 2; resuming at offset 3 must not
        # attach the stale escape to 0x20 nor renumber its terminator as #1/#2.
        ref = record(self.source, 'GAP', 'S', bytes.fromhex('07 1B 0A 20 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'GAP',
            (ManifestSlot('S', (ref,)),),
            {ref: {'head': (0, 2), 'tail': (3, 5)}},
        )
        payloads = [
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='head'),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='tail'),
        ]
        self.disconnect_source()
        ids = self.receive_all(tuple(payloads))
        snapshot, _ = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual(snapshot.records, ())

    def test_supplement_gap_window_then_signs(self):
        ref = record(self.source, 'GAP2', 'S', bytes.fromhex('07 1B 0A 20 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'GAP2',
            (ManifestSlot('S', (ref,)),),
            {ref: {'head': (0, 2), 'mid': (2, 3), 'tail': (3, 5)}},
        )
        head = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='head')
        tail = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='tail')
        boundary = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1)
        mid = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='mid')
        self.disconnect_source()
        base_ids = self.receive_all((boundary, head, tail))
        empty_snapshot, _ = close_windowed(self.inventory, self.reports, manifest, base_ids)
        self.assertEqual(empty_snapshot.records, ())
        mid_id = receive(self.inventory, mid)
        full_ids = (*base_ids, mid_id)
        snapshot, _ = close_windowed(self.inventory, self.reports, manifest, full_ids)
        self.assertEqual([(r.number, list(r.values), r.total) for r in snapshot.records],
                         [(1, [7, 10, 32], 49)])
        self.assertEqual(
            snapshot.records[0].origins[1].locations,
            (ByteLocation(ref, 1), ByteLocation(ref, 2)),
        )

    def test_empty_final_segment_still_rejects_carried_open_record(self):
        left = record(self.source, 'CARRYOPEN', 'L', bytes.fromhex('20')).ref
        tail = record(self.source, 'CARRYOPEN', 'Z', b'').ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'CARRYOPEN',
            (ManifestSlot('L', (left,)), ManifestSlot('Z', (tail,))),
            {left: {'w': (0, 1)}, tail: {}},
        )
        payloads = [
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1, window='w'),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'Z', 1),
        ]
        self.disconnect_source()
        ids = self.receive_all(tuple(payloads))
        before = self.report_counts()
        with self.assertRaisesRegex(ValueError, 'unfinished record'):
            close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual(self.report_counts(), before)

    def test_carried_open_into_next_segment_skips_partial_first_record(self):
        # Full roll: 07 0A 09 0A -> #1 [7], #2 [9]. L byte 07 is not delivered,
        # so resuming at R body start must not sign a spurious empty #1.
        left = record(self.source, 'CARRYFRESH', 'L', bytes.fromhex('07')).ref
        right = record(self.source, 'CARRYFRESH', 'R', bytes.fromhex('0A 09 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'CARRYFRESH',
            (ManifestSlot('L', (left,)), ManifestSlot('R', (right,))),
            {left: {}, right: {'w': (0, 3)}},
        )
        payloads = [
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1, window='w'),
        ]
        self.disconnect_source()
        ids = self.receive_all(tuple(payloads))
        snapshot, _ = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual([(r.number, list(r.values)) for r in snapshot.records], [(2, [9])])

    def test_empty_segment_carries_escape_state_across_seam(self):
        left = record(self.source, 'EMPTYSEG', 'L', bytes.fromhex('07 1B')).ref
        middle = record(self.source, 'EMPTYSEG', 'E', b'').ref
        right = record(self.source, 'EMPTYSEG', 'R', bytes.fromhex('0A 09 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'EMPTYSEG',
            (ManifestSlot('L', (left,)), ManifestSlot('E', (middle,)), ManifestSlot('R', (right,))),
            {left: {'w': (0, 2)}, middle: {}, right: {'w': (0, 3)}},
        )
        payloads = [
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1, window='w'),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'E', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1),
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1, window='w'),
        ]
        self.disconnect_source()
        ids = self.receive_all(tuple(payloads))
        snapshot, _ = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual([list(row.values) for row in snapshot.records], [[7, 10, 9]])
        escape_origin = snapshot.records[0].origins[1].locations
        self.assertEqual((escape_origin[0].revision, escape_origin[0].offset), (left, 1))
        self.assertEqual((escape_origin[1].revision, escape_origin[1].offset), (right, 0))


class FreezeAndIssueWindowedTests(WindowedSampleCase):
    def test_freeze_validation_leaves_no_manifest(self):
        ref = record(self.source, 'FV', 'S', bytes.fromhex('01 02 03 04')).ref
        slot = ManifestSlot('S', (ref,))
        from source.locations import RevisionRef

        other = RevisionRef('FV', 'S', 2)
        bad_tables = (
            {ref: {'w': (True, 3)}},
            {ref: {'w': (1, 1)}},
            {ref: {'w': (0, 5)}},
            {ref: {'w': (-1, 2)}},
            {ref: {'w': '0,2'}},
            {},
            {ref: {'w': (0, 2)}, other: {}},
        )
        for windows in bad_tables:
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                freeze_windowed(self.source, self.issuer, 'FV', (slot,), windows)
        self.assertEqual(
            self.issuer.execute('SELECT COUNT(*) FROM manifests').fetchone(), (0,)
        )

    def test_v2_identity_digest_reissue_and_reopen(self):
        ref = record(self.source, 'ISSUE', 'S', bytes.fromhex('0A 20 0A')).ref
        win_manifest = freeze_windowed(
            self.source, self.issuer, 'ISSUE',
            (ManifestSlot('S', (ref,)),), {ref: {'mid': (1, 3), 'all': (0, 3)}},
        )
        boundary = issue_windowed(self.source, self.issuer, win_manifest.manifest_id, 'S', 1)
        mid = issue_windowed(self.source, self.issuer, win_manifest.manifest_id, 'S', 1, window='mid')
        all_window = issue_windowed(self.source, self.issuer, win_manifest.manifest_id, 'S', 1, window='all')
        wire_ids = [json.loads(p)['piece_id'] for p in (boundary, mid, all_window)]
        self.assertEqual(len(set(wire_ids)), 3)
        for payload in (boundary, mid, all_window):
            fields = json.loads(payload)
            self.assertEqual(fields['version'], 2)
            self.assertEqual(len(fields['digest']), 64)
            unsigned = {k: v for k, v in fields.items() if k != 'digest'}
            self.assertEqual(fields['digest'], digest_json(unsigned))
            decode_v2(payload)
        # same issue key returns same bytes
        again = issue_windowed(self.source, self.issuer, win_manifest.manifest_id, 'S', 1, window='mid')
        self.assertEqual(again, mid)
        self.assertEqual(reissue(self.issuer, wire_ids[1]), mid)
        # reopen issuer store
        self.issuer.close()
        self.issuer = self.open_db(open_issued_store, 'issuer.db')
        self.assertEqual(reissue(self.issuer, wire_ids[1]), mid)
        self.assertEqual(
            issue_windowed(self.source, self.issuer, win_manifest.manifest_id, 'S', 1, window='mid'),
            mid,
        )
        with self.assertRaises(LookupError):
            reissue(self.issuer, '0' * 64)

    def test_unfrozen_segment_revision_window_rejected(self):
        ref = record(self.source, 'BADISSUE', 'S', b'\x07\n').ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'BADISSUE',
            (ManifestSlot('S', (ref,)),), {ref: {}},
        )
        with self.assertRaises(ValueError):
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'X', 1)
        with self.assertRaises(ValueError):
            issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 2)
        with self.assertRaises(ValueError):
            issue_windowed(
                self.source, self.issuer, manifest.manifest_id, 'S', 1, window='nope'
            )


class WindowedIntakeTests(WindowedSampleCase):
    def test_corrupt_v2_and_collision(self):
        ref = record(self.source, 'INTAKE', 'S', bytes.fromhex('07 0A 55')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'INTAKE',
            (ManifestSlot('S', (ref,)),), {ref: {'w': (0, 2)}},
        )
        payload = issue_windowed(
            self.source, self.issuer, manifest.manifest_id, 'S', 1, window='w'
        )
        fields = json.loads(payload)
        fields['digest'] = '0' * 64
        bad_digest = json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()
        with self.assertRaises(ValueError):
            receive(self.inventory, bad_digest)
        with self.assertRaises(ValueError):
            receive(self.inventory, payload[:-1])
        self.assertEqual(self.accepted_count(), 0)

        accepted_id = receive(self.inventory, payload)
        self.assertEqual(receive(self.inventory, payload), accepted_id)
        # same piece number, different valid content => collision
        changed = json.loads(payload)
        changed['body']['data_hex'] = '08' + changed['body']['data_hex'][2:]
        changed['digest'] = digest_json(changed)
        mutated = json.dumps(changed, sort_keys=True, separators=(',', ':')).encode()
        with self.assertRaises(ValueError):
            receive(self.inventory, mutated)
        self.assertEqual(self.accepted_count(), 1)


class WindowedRejectionTests(WindowedSampleCase):
    def build_two_segments(self, roll='REJ'):
        left = record(self.source, roll, 'L', bytes.fromhex('07 0A')).ref
        right = record(self.source, roll, 'R', bytes.fromhex('09 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, roll,
            (ManifestSlot('L', (left,)), ManifestSlot('R', (right,))),
            {left: {'w': (0, 2)}, right: {'w': (0, 2)}},
        )
        manifest = WindowedManifest.from_bytes(manifest.to_bytes())
        lb = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1)
        lw = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1, window='w')
        rb = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1)
        rw = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'R', 1, window='w')
        v1_manifest, v1_payloads = self.issue_fixed(
            roll + '-V1', (ManifestSlot('X', (record(self.source, roll + '-V1', 'X', b'\x01\n').ref,)),)
        )
        self.disconnect_source()
        ids = {
            'lb': receive(self.inventory, lb), 'lw': receive(self.inventory, lw),
            'rb': receive(self.inventory, rb), 'rw': receive(self.inventory, rw),
            'v1': receive(self.inventory, v1_payloads[0]),
        }
        return manifest, ids

    def assert_rejected(self, manifest, selected):
        before = self.report_counts()
        with self.assertRaises(ValueError):
            close_windowed(self.inventory, self.reports, manifest, selected)
        self.assertEqual(self.report_counts(), before)

    def test_selection_rejection_matrix(self):
        manifest, ids = self.build_two_segments()
        full = [ids['lb'], ids['lw'], ids['rb'], ids['rw']]
        # happy path first
        snapshot, first = close_windowed(self.inventory, self.reports, manifest, full)
        self.assertEqual(len(snapshot.records), 2)
        self.assertEqual(first.accepted_ids, tuple(s.accepted_id for s in snapshot.selections))
        # repeating the same receipt number selects it once and stays valid
        deduped, dedup_first = close_windowed(
            self.inventory, self.reports, manifest,
            [ids['lb'], ids['lb'], ids['lw'], ids['rb'], ids['rw']],
        )
        self.assertEqual(dedup_first.accepted_ids.count(ids['lb']), 1)
        self.assertEqual(len(deduped.selections), 4)
        # missing R boundary (window only)
        self.assert_rejected(manifest, [ids['lb'], ids['lw'], ids['rw']])
        # unknown receipt
        self.assert_rejected(manifest, full + [ids['rw'] + 1000])
        # V1 piece mixed in
        self.assert_rejected(manifest, full + [ids['v1']])
        # ordinary close still rejects V2
        with self.assertRaises(ValueError):
            close(self.inventory, self.reports, manifest, [ids['lb'], ids['rb']])

    def test_two_distinct_boundaries_for_one_segment_rejected(self):
        old = record(self.source, 'DUPB', 'L', bytes.fromhex('07 0A'))
        new = revise(self.source, 'DUPB', 'L', bytes.fromhex('08 0A'))
        manifest = freeze_windowed(
            self.source, self.issuer, 'DUPB',
            (ManifestSlot('L', (old.ref, new.ref)),),
            {old.ref: {}, new.ref: {}},
        )
        b1 = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 1)
        b2 = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'L', 2)
        self.disconnect_source()
        i1, i2 = self.receive_all((b1, b2))
        before = self.report_counts()
        with self.assertRaises(ValueError):
            close_windowed(self.inventory, self.reports, manifest, [i1, i2])
        self.assertEqual(self.report_counts(), before)

    def test_overlap_contradiction_rejected(self):
        ref = record(self.source, 'OVL', 'S', bytes.fromhex('07 0a 09 0a 05 0a')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'OVL',
            (ManifestSlot('S', (ref,)),),
            {ref: {'a': (0, 3), 'b': (2, 6)}},
        )
        manifest = WindowedManifest.from_bytes(manifest.to_bytes())
        boundary = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1)
        pa = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='a')
        pb = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='b')
        self.disconnect_source()
        boundary_id = receive(self.inventory, boundary)
        # mutate the overlapping last byte (raw offset 2) of window a with a
        # fresh valid outer digest while keeping its piece number/identity.
        fields = json.loads(pa)
        hex_text = fields['body']['data_hex']
        fields['body']['data_hex'] = hex_text[:-2] + 'ff'
        fields['digest'] = digest_json(fields)
        bad_a = json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()
        a_id = receive(self.inventory, bad_a)
        b_id = receive(self.inventory, pb)
        self.assert_rejected(manifest, [boundary_id, a_id, b_id])

    def test_overlapping_windows_agree_and_count_once(self):
        ref = record(self.source, 'OVLGOOD', 'S', bytes.fromhex('07 0a 09 0a 05 0a')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'OVLGOOD',
            (ManifestSlot('S', (ref,)),),
            {ref: {'a': (0, 3), 'b': (2, 6)}},
        )
        pa = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='a')
        pb = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='b')
        boundary = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1)
        self.disconnect_source()
        ids = self.receive_all((boundary, pa, pb))
        snapshot, first = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual(
            [(r.number, list(r.values)) for r in snapshot.records],
            [(1, [7]), (2, [9]), (3, [5])],
        )
        self.assertEqual(first.accepted_ids, tuple(s.accepted_id for s in snapshot.selections))

    def test_before_commit_failure_rolls_back_windowed_report(self):
        ref = record(self.source, 'ATOMIC', 'S', bytes.fromhex('07 0A')).ref
        manifest = freeze_windowed(
            self.source, self.issuer, 'ATOMIC',
            (ManifestSlot('S', (ref,)),), {ref: {'w': (0, 2)}},
        )
        payload_b = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1)
        payload_w = issue_windowed(self.source, self.issuer, manifest.manifest_id, 'S', 1, window='w')
        self.disconnect_source()
        ids = self.receive_all((payload_b, payload_w))
        calls = []

        def interrupt():
            calls.append(True)
            raise RuntimeError('stop before commit')

        with self.assertRaisesRegex(RuntimeError, 'stop before commit'):
            close_windowed(
                self.inventory, self.reports, manifest, ids, before_commit=interrupt
            )
        self.assertEqual(calls, [True])
        self.assertEqual(self.report_counts(), (0, 0, 0))
        snapshot, first = close_windowed(self.inventory, self.reports, manifest, ids)
        self.assertEqual(self.report_counts()[0], 1)
        from reports.retrieve import get

        self.assertEqual(get(self.reports, snapshot.report_id), (snapshot, first))
