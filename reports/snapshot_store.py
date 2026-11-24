import json
import sqlite3
from dataclasses import dataclass

from assembly.origins import ValueOrigin
from assembly.totals import TalliedRecord
from reports.receipts import FirstReceipt
from source.locations import ByteLocation, RevisionRef, is_positive_id


@dataclass(frozen=True, slots=True)
class ReportSelection:
    accepted_id: int
    piece_id: str
    revision: RevisionRef


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    report_id: int
    manifest_id: str
    roll: str
    selections: tuple[ReportSelection, ...]
    records: tuple[TalliedRecord, ...]


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS reports (
    report_id INTEGER PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    roll TEXT NOT NULL,
    selections TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_records (
    report_id INTEGER NOT NULL REFERENCES reports (report_id),
    record_number INTEGER NOT NULL CHECK (record_number > 0),
    readings TEXT NOT NULL,
    total_decimal TEXT NOT NULL,
    origins TEXT NOT NULL,
    PRIMARY KEY (report_id, record_number)
);
CREATE TABLE IF NOT EXISTS first_receipts (
    report_id INTEGER PRIMARY KEY REFERENCES reports (report_id),
    receipt_number TEXT NOT NULL UNIQUE
);
'''


def open_report_store(path) -> sqlite3.Connection:
    store = sqlite3.connect(path, isolation_level=None)
    try:
        store.execute('PRAGMA foreign_keys = ON')
        store.executescript(_SCHEMA)
    except BaseException:
        store.close()
        raise
    return store


def allocate_report_id(
    store: sqlite3.Connection, manifest_id: str, roll: str, selections: tuple[ReportSelection, ...]
) -> int:
    if not store.in_transaction:
        raise RuntimeError('report allocation requires a report transaction')
    packed = [[item.accepted_id, item.piece_id, item.revision.to_list()] for item in selections]
    cursor = store.execute(
        'INSERT INTO reports (manifest_id, roll, selections) VALUES (?, ?, ?)',
        (manifest_id, roll, json.dumps(packed)),
    )
    return cursor.lastrowid


def _record_row(report_id: int, record: TalliedRecord) -> tuple:
    origins = [[location.to_list() for location in origin.locations] for origin in record.origins]
    return report_id, record.number, json.dumps(record.values), str(record.total), json.dumps(origins)


def write_snapshot(store: sqlite3.Connection, snapshot: ReportSnapshot, first: FirstReceipt) -> None:
    if not store.in_transaction:
        raise RuntimeError('snapshot write requires a report transaction')
    accepted_ids = tuple(item.accepted_id for item in snapshot.selections)
    if (first.report_id, first.manifest_id, first.accepted_ids) != (snapshot.report_id, snapshot.manifest_id, accepted_ids):
        raise ValueError('receipt does not match the report snapshot')
    store.executemany(
        '''INSERT INTO report_records (report_id, record_number, readings, total_decimal, origins)
           VALUES (?, ?, ?, ?, ?)''',
        (_record_row(snapshot.report_id, record) for record in snapshot.records),
    )
    store.execute(
        'INSERT INTO first_receipts (report_id, receipt_number) VALUES (?, ?)',
        (snapshot.report_id, first.receipt_number),
    )


def _unpack_record(row: tuple) -> TalliedRecord:
    record_number, readings, total, locations = row
    origins = tuple(
        ValueOrigin(tuple(ByteLocation.from_list(location) for location in origin))
        for origin in json.loads(locations)
    )
    return TalliedRecord(record_number, tuple(json.loads(readings)), int(total), origins)


def read_report(store: sqlite3.Connection, report_id: int) -> tuple[ReportSnapshot, FirstReceipt] | None:
    if not is_positive_id(report_id):
        return None
    header = store.execute(
        '''SELECT r.manifest_id, r.roll, r.selections, f.receipt_number
           FROM reports AS r JOIN first_receipts AS f ON f.report_id = r.report_id
           WHERE r.report_id = ?''', (report_id,)
    ).fetchone()
    if header is None:
        return None
    manifest_id, roll, packed_selections, receipt_number = header
    selections = tuple(
        ReportSelection(accepted_id, piece_id, RevisionRef.from_list(ref))
        for accepted_id, piece_id, ref in json.loads(packed_selections)
    )
    rows = store.execute(
        '''SELECT record_number, readings, total_decimal, origins FROM report_records
           WHERE report_id = ? ORDER BY record_number''', (report_id,)
    )
    snapshot = ReportSnapshot(report_id, manifest_id, roll, selections, tuple(_unpack_record(row) for row in rows))
    first = FirstReceipt(receipt_number, report_id, manifest_id, tuple(item.accepted_id for item in selections))
    return snapshot, first


def read_audit_row(store: sqlite3.Connection, report_id: int, record_number: int) -> TalliedRecord | None:
    if not is_positive_id(report_id) or not is_positive_id(record_number):
        return None
    row = store.execute(
        '''SELECT r.record_number, r.readings, r.total_decimal, r.origins
           FROM report_records AS r JOIN first_receipts AS f ON f.report_id = r.report_id
           WHERE r.report_id = ? AND r.record_number = ?''', (report_id, record_number)
    ).fetchone()
    return None if row is None else _unpack_record(row)
