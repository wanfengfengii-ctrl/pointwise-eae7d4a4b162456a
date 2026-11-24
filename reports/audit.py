import sqlite3
from dataclasses import dataclass

from assembly.totals import TalliedRecord
from reports.snapshot_store import read_audit_row


@dataclass(frozen=True, slots=True)
class AuditResult:
    report_id: int
    record: TalliedRecord


def audit(store: sqlite3.Connection, report_id: int, record_number: int) -> AuditResult:
    record = read_audit_row(store, report_id, record_number)
    if record is None:
        raise LookupError(f'no record {record_number!r} in signed report {report_id!r}')
    return AuditResult(report_id, record)
