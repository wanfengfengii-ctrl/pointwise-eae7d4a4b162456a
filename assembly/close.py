import sqlite3
from collections.abc import Callable, Iterable

from assembly.framing import FramingState, consume_segment, finish_framing
from assembly.selection import select_segments
from assembly.totals import tally_records
from assembly.windowed import assemble_windowed_records, select_windowed
from issue.manifest_view import FrozenManifest, WindowedManifest
from reports.publish import publish_report, publish_selections
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


def close_windowed(
    inventory: sqlite3.Connection,
    reports: sqlite3.Connection,
    manifest: WindowedManifest,
    accepted_ids: Iterable[int],
    *,
    before_commit: Callable[[], None] | None = None,
) -> tuple[ReportSnapshot, FirstReceipt]:
    choice = select_windowed(inventory, manifest, accepted_ids)
    records = assemble_windowed_records(manifest, choice)
    return publish_selections(
        reports,
        manifest.manifest_id,
        manifest.roll,
        choice.selections,
        records,
        before_commit=before_commit,
    )
