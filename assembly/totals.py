from collections.abc import Iterable
from dataclasses import dataclass

from assembly.framing import FramedRecord
from assembly.origins import ValueOrigin


@dataclass(frozen=True, slots=True)
class TalliedRecord:
    number: int
    values: tuple[int, ...]
    total: int
    origins: tuple[ValueOrigin, ...]


def tally_records(records: Iterable[FramedRecord]) -> tuple[TalliedRecord, ...]:
    return tuple(
        TalliedRecord(number, record.values, sum(record.values), record.origins)
        for number, record in enumerate(records, start=1)
    )
