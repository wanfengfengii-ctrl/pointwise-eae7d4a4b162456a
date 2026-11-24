import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from assembly.close import close, close_windowed
from issue.issued_store import issue, issue_windowed, open_issued_store, reissue
from issue.manifest_view import FrozenManifest, ManifestSlot, WindowedManifest
from issue.manifests import freeze, freeze_windowed
from issue.wire import decode_piece
from receipt.intake import receive
from receipt.inventory import open_inventory
from reports.audit import audit
from reports.retrieve import get
from reports.snapshot_store import open_report_store
from source.raw_store import open_source_store
from source.recordings import record
from source.revisions import revise


def prepare_station(root, sample):
    roll = sample['roll']
    left, right = sample['left'], sample['right']
    with closing(open_source_store(root / 'source.db')) as source, closing(open_issued_store(root / 'issuer.db')) as issuer:
        initial = record(source, roll, left['segment'], bytes.fromhex(left['initial_hex'])).ref
        corrected = revise(source, roll, left['segment'], bytes.fromhex(left['correction_hex'])).ref
        right_ref = record(source, roll, right['segment'], bytes.fromhex(right['initial_hex'])).ref
        manifest = freeze(source, issuer, roll, (
            ManifestSlot(initial.segment, (initial, corrected)), ManifestSlot(right_ref.segment, (right_ref,)),
        ))
        wires = tuple(
            issue(source, issuer, manifest.manifest_id, ref.segment, ref.revision)
            for ref in (right_ref, initial, corrected)
        )
        original_piece = decode_piece(wires[0]).identity.piece_id
        return manifest.to_bytes(), wires, reissue(issuer, original_piece)


def assemble_offline(root, handoff):
    manifest_wire, (right, initial, corrected), repeated_right = handoff
    manifest = FrozenManifest.from_bytes(manifest_wire)
    with closing(open_inventory(root / 'accepted.db')) as inventory:
        right_id = receive(inventory, right)
        initial_id = receive(inventory, initial)
        corrected_id = receive(inventory, corrected)
        repeat_id = receive(inventory, repeated_right)
        with closing(open_report_store(root / 'reports.db')) as reports:
            original, _ = close(inventory, reports, manifest, (right_id, initial_id))
            later, _ = close(inventory, reports, manifest, (corrected_id, repeat_id))
    with closing(open_report_store(root / 'reports.db')) as reports:
        historical, first = get(reports, original.report_id)
        updated, second = get(reports, later.report_id)
        inspected = audit(reports, historical.report_id, 1)
        return {
            'roll': historical.roll,
            'right_received': right_id,
            'right_received_again': repeat_id,
            'original': {'report_id': historical.report_id, 'receipt': first.receipt_number,
                         'accepted_ids': first.accepted_ids, 'records': [asdict(row) for row in historical.records]},
            'replacement': {'report_id': updated.report_id, 'receipt': second.receipt_number,
                            'accepted_ids': second.accepted_ids, 'records': [asdict(row) for row in updated.records]},
            'historical_audit': asdict(inspected),
        }


def _record_view(snapshot):
    return [
        {
            'number': row.number,
            'readings': list(row.values),
            'total': row.total,
            'origins': [
                [location.to_list() for location in origin.locations]
                for origin in row.origins
            ],
        }
        for row in snapshot.records
    ]


def run_windowed_demo():
    sample = json.loads(
        (Path(__file__).with_name('samples') / 'windowed_roll.json').read_text(encoding='utf-8')
    )
    with TemporaryDirectory(prefix='windowed-roll-') as directory:
        root = Path(directory)
        roll = sample['roll']
        with closing(open_source_store(root / 'w-source.db')) as source, \
                closing(open_issued_store(root / 'w-issuer.db')) as issuer:
            slots = []
            windows = {}
            refs = {}
            for segment_spec in sample['slots']:
                name = segment_spec['segment']
                segment_refs = []
                for index, revision_spec in enumerate(segment_spec['revisions']):
                    raw = bytes.fromhex(revision_spec['raw_hex'])
                    if index == 0:
                        revision = record(source, roll, name, raw)
                    else:
                        revision = revise(source, roll, name, raw)
                    segment_refs.append(revision.ref)
                    windows[revision.ref] = {
                        win_name: tuple(bounds)
                        for win_name, bounds in revision_spec['windows'].items()
                    }
                refs[name] = tuple(segment_refs)
                slots.append(ManifestSlot(name, tuple(segment_refs)))
            manifest = freeze_windowed(source, issuer, roll, tuple(slots), windows)
            manifest_wire = manifest.to_bytes()

            def issue_one(segment, revision, window=None):
                return issue_windowed(
                    source, issuer, manifest.manifest_id, segment, revision, window=window
                )

            pieces = {
                'R_boundary': issue_one('R', 1),
                'R_mid': issue_one('R', 1, 'mid'),
                'R_tail': issue_one('R', 1, 'tail'),
                'R_first': issue_one('R', 1, 'first'),
                'L1_boundary': issue_one('L', 1),
                'L1_left': issue_one('L', 1, 'left'),
                'L2_boundary': issue_one('L', 2),
                'L2_left': issue_one('L', 2, 'left'),
            }
            reissued = reissue(issuer, json.loads(pieces['R_mid'])['piece_id'])
            assert reissued == pieces['R_mid']
        # Source and issuer are now closed.
        manifest = WindowedManifest.from_bytes(manifest_wire)

        with closing(open_inventory(root / 'w-accepted.db')) as inventory:
            ids = {name: receive(inventory, payload) for name, payload in pieces.items()}
            with closing(open_report_store(root / 'w-reports.db')) as reports:
                r_missing = [ids['R_boundary'], ids['R_mid'], ids['R_tail']]
                l1_missing, l1_missing_first = close_windowed(
                    inventory, reports, manifest,
                    [ids['L1_boundary'], ids['L1_left'], *r_missing],
                )
                l2_missing, l2_missing_first = close_windowed(
                    inventory, reports, manifest,
                    [ids['L2_boundary'], ids['L2_left'], *r_missing],
                )
                # A supplementary window arrives after the missing-window signing.
                r_full = [ids['R_boundary'], ids['R_first'], ids['R_mid'], ids['R_tail']]
                l1_full, l1_full_first = close_windowed(
                    inventory, reports, manifest,
                    [ids['L1_boundary'], ids['L1_left'], *r_full],
                )
                l2_full, l2_full_first = close_windowed(
                    inventory, reports, manifest,
                    [ids['L2_boundary'], ids['L2_left'], *r_full],
                )
                l1_full_id = l1_full.report_id
                l1_missing_id = l1_missing.report_id

        # Reopen the report store and reclaim by original report number.
        with closing(open_report_store(root / 'w-reports.db')) as reopened:
            missing_back, missing_first_back = get(reopened, l1_missing_id)
            full_back, full_first_back = get(reopened, l1_full_id)
            audited_two = audit(reopened, l1_full_id, 2)
            try:
                audit(reopened, l1_missing_id, 2)
                skipped_lookup = 'unexpectedly found skipped number 2'
            except LookupError:
                skipped_lookup = 'skipped record number 2 is absent as required'

        return {
            'roll': roll,
            'note': 'R first byte window is absent for the missing-window reports',
            'missing_window': {
                'L1': {
                    'report_id': l1_missing.report_id,
                    'first_receipt': l1_missing_first.receipt_number,
                    'accepted_ids': list(l1_missing_first.accepted_ids),
                    'records': _record_view(l1_missing),
                },
                'L2': {
                    'report_id': l2_missing.report_id,
                    'first_receipt': l2_missing_first.receipt_number,
                    'accepted_ids': list(l2_missing_first.accepted_ids),
                    'records': _record_view(l2_missing),
                },
            },
            'supplemented_window': {
                'L1': {
                    'report_id': l1_full.report_id,
                    'first_receipt': l1_full_first.receipt_number,
                    'accepted_ids': list(l1_full_first.accepted_ids),
                    'records': _record_view(l1_full),
                },
                'L2': {
                    'report_id': l2_full.report_id,
                    'first_receipt': l2_full_first.receipt_number,
                    'accepted_ids': list(l2_full_first.accepted_ids),
                    'records': _record_view(l2_full),
                },
            },
            'reopened': {
                'missing_report_id': missing_back.report_id,
                'missing_first_receipt': missing_first_back.receipt_number,
                'missing_records': _record_view(missing_back),
                'full_report_id': full_back.report_id,
                'full_first_receipt': full_first_back.receipt_number,
                'full_accepted_ids': list(full_first_back.accepted_ids),
                'full_records': _record_view(full_back),
                'audit_L1_full_number_2': {
                    'values': list(audited_two.record.values),
                    'total': audited_two.record.total,
                    'origins': [
                        [location.to_list() for location in origin.locations]
                        for origin in audited_two.record.origins
                    ],
                },
                'skipped_number_2_in_missing_report': skipped_lookup,
            },
        }


def main():
    sample_path = Path(__file__).with_name('samples') / 'night_shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='pressure-handoff-') as directory:
        root = Path(directory)
        handoff = prepare_station(root, sample)
        result = assemble_offline(root, handoff)
    result['windowed_roll'] = run_windowed_demo()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
