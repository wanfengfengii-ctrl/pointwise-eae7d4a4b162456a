import sqlite3
from collections.abc import Callable, Iterable

from assembly.framing import FramingState, consume_segment, finish_framing
from assembly.selection import select_segments
from assembly.totals import tally_records
from issue.manifest_view import FrozenManifest
from reports.publish import publish_report
from reports.receipts import FirstReceipt
from reports.snapshot_store import ReportSnapshot


def close(
    inventory: sqlite3.Connection,
    reports: sqlite3.Connection,
    manifest: FrozenManifest,
    accepted_ids: Iterable[int],
    *,
    before_commit: Callable[[], None] | None = None,
) -> tuple[ReportSnapshot, FirstReceipt]:
    selected = select_segments(inventory, manifest, accepted_ids)
    state = FramingState()
    for segment in selected:
        consume_segment(state, segment)
    records = tally_records(finish_framing(state))
    return publish_report(reports, manifest, selected, records, before_commit=before_commit)
