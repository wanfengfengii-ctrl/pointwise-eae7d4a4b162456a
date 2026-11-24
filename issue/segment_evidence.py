from issue.wire import ESCAPE, RECORD_END, EvidenceBranch, EvidenceReading, RecordEnd, SegmentEvidence
from source.locations import locate_byte
from source.raw_store import RawRevision


def _interpret(raw: RawRevision, body: bytes, entering_escaped: bool) -> EvidenceBranch:
    events = []
    needs_previous = entering_escaped
    pending_offset = None
    for index, value in enumerate(body):
        offset = locate_byte(raw.ref, index, raw.body_start, len(body)).offset
        if needs_previous:
            events.append(EvidenceReading(value, offset, from_previous=True))
            needs_previous = False
        elif pending_offset is not None:
            events.append(EvidenceReading(value, offset, escape_offset=pending_offset))
            pending_offset = None
        elif value == ESCAPE:
            pending_offset = offset
        elif value == RECORD_END:
            events.append(RecordEnd(offset))
        else:
            events.append(EvidenceReading(value, offset))
    return EvidenceBranch(tuple(events), pending_offset)


def build_segment_evidence(raw: RawRevision) -> SegmentEvidence:
    body = raw.data_body
    return SegmentEvidence(
        ordinary=_interpret(raw, body, entering_escaped=False),
        escaped=_interpret(raw, body, entering_escaped=True),
    )
