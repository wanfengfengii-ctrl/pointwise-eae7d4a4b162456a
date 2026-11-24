import tempfile
import unittest
from pathlib import Path

from issue.issued_store import issue, open_issued_store
from issue.manifest_view import FrozenManifest, ManifestSlot
from issue.manifests import freeze
from receipt.intake import receive
from receipt.inventory import open_inventory
from reports.snapshot_store import open_report_store
from source.raw_store import open_source_store
from source.recordings import record


class HandoffCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.open_db(open_source_store, 'source.db')
        self.issuer = self.open_db(open_issued_store, 'issuer.db')
        self.inventory = self.open_db(open_inventory, 'accepted.db')
        self.reports = self.open_db(open_report_store, 'reports.db')

    def open_db(self, opener, filename):
        db = opener(self.root / filename)
        self.addCleanup(db.close)
        return db

    def disconnect_source(self):
        self.source.close()
        self.issuer.close()

    def issue_fixed(self, roll, slots):
        manifest = freeze(self.source, self.issuer, roll, slots)
        payloads = tuple(
            issue(self.source, self.issuer, manifest.manifest_id, ref.segment, ref.revision)
            for slot in manifest.slots for ref in slot.candidates
        )
        return FrozenManifest.from_bytes(manifest.to_bytes()), payloads

    def issue_recording(self, roll, parts):
        refs = tuple(record(self.source, roll, name, raw).ref for name, raw in parts)
        manifest, payloads = self.issue_fixed(roll, tuple(ManifestSlot(ref.segment, (ref,)) for ref in refs))
        return manifest, payloads, refs

    def receive_all(self, payloads):
        return tuple(receive(self.inventory, payload) for payload in payloads)

    def report_counts(self):
        return self.reports.execute('''SELECT (SELECT COUNT(*) FROM reports),
                                             (SELECT COUNT(*) FROM report_records),
                                             (SELECT COUNT(*) FROM first_receipts)''').fetchone()

    def accepted_count(self):
        return self.inventory.execute('SELECT COUNT(*) FROM accepted_pieces').fetchone()[0]
