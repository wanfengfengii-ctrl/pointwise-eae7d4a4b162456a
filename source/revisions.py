import sqlite3
from dataclasses import dataclass

from source.locations import ByteLocation, RevisionRef, locate_byte
from source.raw_store import RawRevision, load_raw, source_transaction, store_raw
from source.segments import SegmentRef, require_segment


@dataclass(frozen=True, slots=True)
class Revision:
    raw: RawRevision

    @property
    def ref(self) -> RevisionRef:
        return self.raw.ref

    def locate(self, body_offset: int) -> ByteLocation:
        return locate_byte(self.ref, body_offset, self.raw.body_start, self.raw.body_end - self.raw.body_start)


def append_revision(
    store: sqlite3.Connection,
    segment: SegmentRef,
    raw_bytes: bytes,
    *,
    body_start: int = 0,
    body_end: int | None = None,
) -> Revision:
    if not store.in_transaction:
        raise RuntimeError('append_revision requires a source transaction')
    require_segment(store, segment)
    if type(raw_bytes) is not bytes:
        raise ValueError('recording must be bytes')
    (previous,) = store.execute(
        'SELECT MAX(revision) FROM raw_revisions WHERE roll = ? AND segment = ?',
        (segment.roll, segment.segment),
    ).fetchone()
    ref = RevisionRef(segment.roll, segment.segment, 1 if previous is None else previous + 1)
    raw = RawRevision(ref, raw_bytes, body_start, len(raw_bytes) if body_end is None else body_end)
    store_raw(store, raw)
    return Revision(raw)


def read_revision(store: sqlite3.Connection, ref: RevisionRef) -> Revision:
    return Revision(load_raw(store, ref))


def revise(
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
        revision = append_revision(store, target, raw_bytes, body_start=body_start, body_end=body_end)
    return revision
