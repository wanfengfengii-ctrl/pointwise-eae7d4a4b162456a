import sqlite3

from reports.receipts import FirstReceipt
from reports.snapshot_store import ReportSnapshot, read_report


def get(store: sqlite3.Connection, report_id: int) -> tuple[ReportSnapshot, FirstReceipt]:
    result = read_report(store, report_id)
    if result is None:
        raise LookupError(f'unknown signed report {report_id!r}')
    return result
