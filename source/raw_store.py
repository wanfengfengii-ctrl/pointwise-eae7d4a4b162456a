import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from source.locations import RevisionRef


@dataclass(frozen=True, slots=True)
class RawRevision:
    ref: RevisionRef
    raw_bytes: bytes
    body_start: int
    body_end: int

    def __post_init__(self) -> None:
        if type(self.raw_bytes) is not bytes:
            raise ValueError('recording must be bytes')
        if type(self.body_start) is not int or type(self.body_end) is not int:
            raise ValueError('body range must use integer offsets')
        if not 0 <= self.body_start <= self.body_end <= len(self.raw_bytes):
            raise ValueError('body range is outside the original recording')

    @property
    def data_body(self) -> bytes:
        return self.raw_bytes[self.body_start:self.body_end]


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS segments (
    roll TEXT NOT NULL,
    segment TEXT NOT NULL,
    PRIMARY KEY (roll, segment)
);
CREATE TABLE IF NOT EXISTS raw_revisions (
    roll TEXT NOT NULL,
    segment TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    raw_bytes BLOB NOT NULL,
    body_start INTEGER NOT NULL CHECK (body_start >= 0),
    body_end INTEGER NOT NULL CHECK (body_end >= body_start AND body_end <= length(raw_bytes)),
    PRIMARY KEY (roll, segment, revision),
    FOREIGN KEY (roll, segment) REFERENCES segments (roll, segment)
);
'''


def open_source_store(path) -> sqlite3.Connection:
    store = sqlite3.connect(path, isolation_level=None)
    try:
        store.execute('PRAGMA foreign_keys = ON')
        store.executescript(_SCHEMA)
    except BaseException:
        store.close()
        raise
    return store


@contextmanager
def source_transaction(store: sqlite3.Connection):
    store.execute('BEGIN IMMEDIATE')
    with store:
        yield store


def store_raw(store: sqlite3.Connection, revision: RawRevision) -> None:
    ref = revision.ref
    store.execute(
        '''INSERT INTO raw_revisions (roll, segment, revision, raw_bytes, body_start, body_end)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (ref.roll, ref.segment, ref.revision, revision.raw_bytes, revision.body_start, revision.body_end),
    )


def load_raw(store: sqlite3.Connection, ref: RevisionRef) -> RawRevision:
    row = store.execute(
        '''SELECT raw_bytes, body_start, body_end FROM raw_revisions
           WHERE roll = ? AND segment = ? AND revision = ?''',
        (ref.roll, ref.segment, ref.revision),
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown recording revision {ref!r}')
    return RawRevision(ref, *row)
