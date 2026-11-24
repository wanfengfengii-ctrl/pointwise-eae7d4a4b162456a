import sqlite3
from dataclasses import dataclass

from source.locations import require_name


@dataclass(frozen=True, slots=True)
class SegmentRef:
    roll: str
    segment: str

    def __post_init__(self) -> None:
        require_name(self.roll)
        require_name(self.segment)


def register_segment(store: sqlite3.Connection, ref: SegmentRef) -> None:
    result = store.execute(
        '''INSERT INTO segments (roll, segment) VALUES (?, ?)
           ON CONFLICT (roll, segment) DO NOTHING''',
        (ref.roll, ref.segment),
    )
    if result.rowcount == 0:
        raise ValueError(f'segment already recorded: {ref!r}')


def require_segment(store: sqlite3.Connection, ref: SegmentRef) -> SegmentRef:
    row = store.execute(
        'SELECT 1 FROM segments WHERE roll = ? AND segment = ?', (ref.roll, ref.segment)
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown segment {ref!r}')
    return ref
