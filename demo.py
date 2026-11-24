import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from assembly.close import close
from issue.issued_store import issue, open_issued_store, reissue
from issue.manifest_view import FrozenManifest, ManifestSlot
from issue.manifests import freeze
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


def main():
    sample_path = Path(__file__).with_name('samples') / 'night_shift.json'
    sample = json.loads(sample_path.read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='pressure-handoff-') as directory:
        root = Path(directory)
        handoff = prepare_station(root, sample)
        result = assemble_offline(root, handoff)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
