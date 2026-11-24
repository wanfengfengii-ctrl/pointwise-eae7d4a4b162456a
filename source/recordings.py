import sqlite3

from source.raw_store import source_transaction
from source.revisions import Revision, append_revision
from source.segments import SegmentRef, register_segment


def record(
    store: sqlite3.Connection,
    roll: str,
    segment: str,
    raw_bytes: bytes,
    *,
    body_start: int = 0,
    body_end: int | None = None,
) -> Revision:
    target = SegmentRef(roll, segment)
    with source_transaction(store):
        register_segment(store, target)
        revision = append_revision(store, target, raw_bytes, body_start=body_start, body_end=body_end)
    return revision
