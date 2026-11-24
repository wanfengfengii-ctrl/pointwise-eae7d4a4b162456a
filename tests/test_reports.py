import sqlite3

from assembly.close import close
from issue.manifest_view import ManifestSlot
from reports.audit import audit
from reports.retrieve import get
from reports.snapshot_store import open_report_store
from source.locations import ByteLocation
from source.revisions import revise
from tests.support import HandoffCase


class ReportTests(HandoffCase):
    def test_before_commit_failure_rolls_back_all_rows_and_preserves_history(self):
        original, original_payloads, _ = self.issue_recording('P', (('S', b'\x07\n'),))
        new, new_payloads, _ = self.issue_recording('Q', (('S', b'\x0b\n\n'),))
        old, first = close(self.inventory, self.reports, original, self.receive_all(original_payloads))
        new_ids = self.receive_all(new_payloads)
        self.disconnect_source()
        self.assertEqual(self.report_counts(), (1, 1, 1))
        pending_ids = []

        def interrupt():
            self.assertTrue(self.reports.in_transaction)
            self.assertEqual(self.report_counts(), (2, 3, 2))
            pending_id, = self.reports.execute('SELECT MAX(report_id) FROM reports').fetchone()
            self.assertEqual(audit(self.reports, pending_id, 1).record.values, (11,))
            pending_ids.append(pending_id)
            raise RuntimeError('before commit interrupted')

        with self.assertRaisesRegex(RuntimeError, 'before commit interrupted'):
            close(self.inventory, self.reports, new, new_ids, before_commit=interrupt)
        self.assertFalse(self.reports.in_transaction)
        self.assertEqual(len(pending_ids), 1)
        self.reports.close()
        self.reports = self.open_db(open_report_store, 'reports.db')
        self.assertEqual(self.report_counts(), (1, 1, 1))
        with self.assertRaises(LookupError):
            get(self.reports, pending_ids[0])
        with self.assertRaises(LookupError):
            audit(self.reports, pending_ids[0], 1)
        self.assertEqual(get(self.reports, old.report_id), (old, first))
        self.assertEqual(audit(self.reports, old.report_id, 1).record, old.records[0])
        retry, retry_first = close(self.inventory, self.reports, new, new_ids)
        self.assertEqual(retry_first.report_id, retry.report_id)
        self.assertEqual(tuple(row.total for row in retry.records), (11, 0))

    def test_database_commit_error_also_rolls_back_snapshot(self):
        manifest, payloads, _ = self.issue_recording('P', (('S', b'\x07\n'),))
        accepted = self.receive_all(payloads)
        self.disconnect_source()
        self.reports.executescript('''
            CREATE TABLE test_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE test_commit_guard (
                missing INTEGER REFERENCES test_parent (id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER test_reject_commit AFTER INSERT ON first_receipts
            BEGIN
                INSERT INTO test_commit_guard (missing) VALUES (1);
            END;
        ''')
        with self.assertRaises(sqlite3.IntegrityError):
            close(self.inventory, self.reports, manifest, accepted)
        self.assertFalse(self.reports.in_transaction)
        self.assertEqual(self.report_counts(), (0, 0, 0))

    def test_history_isolation_after_source_correction_and_report_reopen(self):
        old_manifest, old_pieces, (old_ref,) = self.issue_recording('P', (('S', b'\x07\n'),))
        old, old_first = close(self.inventory, self.reports, old_manifest, self.receive_all(old_pieces))
        correction = revise(self.source, 'P', 'S', b'HEAD\x09\n', body_start=4)
        new_manifest, new_pieces = self.issue_fixed('P', (ManifestSlot('S', (correction.ref,)),))
        new, new_first = close(self.inventory, self.reports, new_manifest, self.receive_all(new_pieces))
        self.disconnect_source()
        self.reports.close()
        self.reports = self.open_db(open_report_store, 'reports.db')
        self.assertEqual(self.report_counts(), (2, 2, 2))
        self.assertNotEqual(old_first.receipt_number, new_first.receipt_number)
        self.assertEqual(get(self.reports, old.report_id), (old, old_first))
        self.assertEqual(get(self.reports, new.report_id), (new, new_first))
        old_record = audit(self.reports, old.report_id, 1).record
        new_record = audit(self.reports, new.report_id, 1).record
        self.assertEqual((old_record.values, old_record.total), ((7,), 7))
        self.assertEqual((new_record.values, new_record.total), ((9,), 9))
        self.assertEqual(old_record.origins[0].locations, (ByteLocation(old_ref, 0),))
        self.assertEqual(new_record.origins[0].locations, (ByteLocation(correction.ref, 4),))

    def test_unknown_report_and_unknown_record(self):
        with self.assertRaises(LookupError):
            get(self.reports, 1)
        with self.assertRaises(LookupError):
            audit(self.reports, 1, 1)
        manifest, payloads, _ = self.issue_recording('P', (('S', b'\n'),))
        output, _ = close(self.inventory, self.reports, manifest, self.receive_all(payloads))
        for number in (0, 2, True):
            with self.subTest(number=number), self.assertRaises(LookupError):
                audit(self.reports, output.report_id, number)
        with self.assertRaises(LookupError):
            audit(self.reports, output.report_id + 1, 1)
        self.assertEqual(audit(self.reports, output.report_id, 1).record.total, 0)
