from dataclasses import dataclass

from issue.wire import EvidenceReading
from source.locations import ByteLocation, RevisionRef


@dataclass(frozen=True, slots=True)
class ValueOrigin:
    locations: tuple[ByteLocation, ...]


def bind_value_origin(
    revision: RevisionRef, reading: EvidenceReading, pending_escape: ByteLocation | None
) -> ValueOrigin:
    data = ByteLocation(revision, reading.offset)
    if reading.from_previous:
        if pending_escape is None:
            raise ValueError('evidence requires an unbound incoming escape')
        return ValueOrigin((pending_escape, data))
    if pending_escape is not None:
        raise ValueError('reading failed to consume the incoming escape')
    if reading.escape_offset is not None:
        return ValueOrigin((ByteLocation(revision, reading.escape_offset), data))
    return ValueOrigin((data,))
