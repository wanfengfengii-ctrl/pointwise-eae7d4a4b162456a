from dataclasses import dataclass, field

from assembly.origins import ValueOrigin, bind_value_origin
from assembly.selection import SelectedSegment
from issue.wire import RecordEnd
from source.locations import ByteLocation


@dataclass(frozen=True, slots=True)
class FramedRecord:
    values: tuple[int, ...]
    origins: tuple[ValueOrigin, ...]


@dataclass(slots=True)
class FramingState:
    pending_escape: ByteLocation | None = None
    current_values: list[int] = field(default_factory=list)
    current_origins: list[ValueOrigin] = field(default_factory=list)
    records: list[FramedRecord] = field(default_factory=list)


def consume_segment(state: FramingState, segment: SelectedSegment) -> None:
    evidence = segment.accepted.verified.envelope.evidence
    if evidence.escaped is None:
        raise ValueError('segment evidence does not cover both entry states')
    branch = evidence.ordinary if state.pending_escape is None else evidence.escaped
    for event in branch.events:
        if isinstance(event, RecordEnd):
            if state.pending_escape is not None:
                raise ValueError('record ended before consuming incoming escape')
            state.records.append(FramedRecord(tuple(state.current_values), tuple(state.current_origins)))
            state.current_values.clear()
            state.current_origins.clear()
        else:
            origin = bind_value_origin(segment.revision, event, state.pending_escape)
            state.current_values.append(event.value)
            state.current_origins.append(origin)
            state.pending_escape = None
    if branch.trailing_escape is not None:
        state.pending_escape = ByteLocation(segment.revision, branch.trailing_escape)


def finish_framing(state: FramingState) -> tuple[FramedRecord, ...]:
    if state.pending_escape is not None:
        raise ValueError('recording ends with a dangling escape')
    if state.current_values:
        raise ValueError('recording ends with an unfinished record')
    return tuple(state.records)
