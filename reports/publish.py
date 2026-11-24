import sqlite3
from collections.abc import Callable

from assembly.selection import SelectedSegment
from assembly.totals import TalliedRecord
from issue.manifest_view import FrozenManifest
from reports.receipts import FirstReceipt, make_first_receipt
from reports.snapshot_store import ReportSelection, ReportSnapshot, allocate_report_id, write_snapshot


def publish_report(
    store: sqlite3.Connection,
    manifest: FrozenManifest,
    selected: tuple[SelectedSegment, ...],
    records: tuple[TalliedRecord, ...],
    *,
    before_commit: Callable[[], None] | None = None,
) -> tuple[ReportSnapshot, FirstReceipt]:
    selections = tuple(
        ReportSelection(item.accepted.accepted_id, item.accepted.verified.piece_id, item.revision)
        for item in selected
    )
    accepted_ids = tuple(item.accepted_id for item in selections)
    store.execute('BEGIN IMMEDIATE')
    with store:
        report_id = allocate_report_id(store, manifest.manifest_id, manifest.roll, selections)
        snapshot = ReportSnapshot(report_id, manifest.manifest_id, manifest.roll, selections, records)
        first = make_first_receipt(report_id, manifest.manifest_id, accepted_ids)
        write_snapshot(store, snapshot, first)
        if before_commit is not None:
            before_commit()
    return snapshot, first
