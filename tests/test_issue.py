from assembly.close import close
from issue.issued_store import issue, open_issued_store, reissue
from issue.manifest_view import ManifestSlot
from issue.manifests import freeze, read_manifest
from issue.wire import decode_piece
from receipt.intake import receive
from source.locations import ByteLocation, RevisionRef
from source.raw_store import open_source_store
from source.recordings import record
from source.revisions import read_revision, revise
from tests.support import HandoffCase


class IssueTests(HandoffCase):
    def test_revision_snapshot_and_independent_issue(self):
        old = record(self.source, 'P', 'L', b'SVC\x07\n!', body_start=3, body_end=5)
        new = revise(self.source, 'P', 'L', b'H\x09\n', body_start=1)
        self.assertEqual((old.ref.revision, new.ref.revision), (1, 2))
        self.source.close()
        self.source = self.open_db(open_source_store, 'source.db')
        self.assertEqual(read_revision(self.source, old.ref).raw.raw_bytes, b'SVC\x07\n!')
        self.assertEqual(read_revision(self.source, new.ref).raw.raw_bytes, b'H\x09\n')
        self.assertEqual(old.locate(0), ByteLocation(old.ref, 3))
        with self.assertRaises(ValueError):
            old.locate(2)
        manifest, payloads = self.issue_fixed('P', (ManifestSlot('L', (old.ref, new.ref)),))
        self.assertNotEqual(payloads[0], payloads[1])
        for index, (revision, value, offset) in enumerate(((old, 7, 3), (new, 9, 1))):
            payload = payloads[index]
            self.assertEqual(decode_piece(payload).identity.revision, revision.ref)
            self.assertEqual(issue(self.source, self.issuer, manifest.manifest_id, 'L', revision.ref.revision), payload)
            self.assertEqual(reissue(self.issuer, decode_piece(payload).identity.piece_id), payload)
        self.disconnect_source()
        for payload, revision, value, offset in zip(payloads, (old, new), (7, 9), (3, 1)):
            accepted_id = receive(self.inventory, payload)
            report, _ = close(self.inventory, self.reports, manifest, [accepted_id])
            row, = report.records
            self.assertEqual((row.values, row.total), ((value,), value))
            self.assertEqual(row.origins[0].locations, (ByteLocation(revision.ref, offset),))

    def test_reissue_bytes_after_reopen(self):
        manifest, (payload,), (ref,) = self.issue_recording('P', (('S', b'\x07\n'),))
        piece_id = decode_piece(payload).identity.piece_id
        revise(self.source, 'P', 'S', b'\x09\n')
        self.issuer.close()
        self.issuer = self.open_db(open_issued_store, 'issuer.db')
        self.assertEqual(read_manifest(self.issuer, manifest.manifest_id), manifest)
        self.assertEqual(reissue(self.issuer, piece_id), payload)
        self.assertEqual(issue(self.source, self.issuer, manifest.manifest_id, 'S', ref.revision), payload)
        with self.assertRaises(LookupError):
            reissue(self.issuer, 'unknown')

    def test_unlisted_revision_cannot_be_issued(self):
        manifest, (payload,), _ = self.issue_recording('P', (('S', b'\x07\n'),))
        newer = revise(self.source, 'P', 'S', b'\x09\n')
        with self.assertRaisesRegex(ValueError, 'frozen candidates'):
            issue(self.source, self.issuer, manifest.manifest_id, 'S', newer.ref.revision)
        self.assertEqual(self.issuer.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (1,))
        self.assertEqual(reissue(self.issuer, decode_piece(payload).identity.piece_id), payload)

    def test_cross_roll_freeze_is_rejected(self):
        left = record(self.source, 'P', 'L', b'\x07').ref
        foreign = record(self.source, 'Q', 'R', b'\n').ref
        with self.assertRaisesRegex(ValueError, 'another roll'):
            freeze(self.source, self.issuer, 'P', (ManifestSlot('L', (left,)), ManifestSlot('R', (foreign,))))
        self.assertEqual(self.issuer.execute('SELECT COUNT(*) FROM manifests').fetchone(), (0,))
        self.assertEqual(self.issuer.execute('SELECT COUNT(*) FROM issued_pieces').fetchone(), (0,))

    def test_missing_and_duplicate_slots_leave_no_manifest(self):
        ref = record(self.source, 'P', 'L', b'\x07\n').ref
        slot = ManifestSlot('L', (ref,))
        for slots in ((), (slot, slot), (ManifestSlot('X', (RevisionRef('P', 'X', 1),)),),
                      (ManifestSlot('L', (RevisionRef('P', 'L', 2),)),)):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                freeze(self.source, self.issuer, 'P', slots)
        self.assertEqual(self.issuer.execute('SELECT COUNT(*) FROM manifests').fetchone(), (0,))

    def test_invalid_record_rolls_back_registration(self):
        with self.assertRaises(ValueError):
            record(self.source, 'P', 'L', b'\n', body_end=2)
        with self.assertRaises(LookupError):
            revise(self.source, 'P', 'L', b'\n')
        saved = record(self.source, 'P', 'L', b'\x07\n')
        with self.assertRaises(ValueError):
            record(self.source, 'P', 'L', b'\x0b\n')
        self.assertEqual(saved.ref.revision, 1)
        self.assertEqual(read_revision(self.source, saved.ref).raw.raw_bytes, b'\x07\n')
