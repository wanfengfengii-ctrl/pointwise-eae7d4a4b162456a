import hashlib
import json
from dataclasses import dataclass

from source.locations import RevisionRef, require_name


VERSION = 1
KIND = 'recording-segment'
RECORD_END = 0x0A
ESCAPE = 0x1B


@dataclass(frozen=True, slots=True)
class PieceIdentity:
    manifest_id: str
    revision: RevisionRef

    def __post_init__(self) -> None:
        require_name(self.manifest_id)
        if not isinstance(self.revision, RevisionRef):
            raise ValueError('piece requires a fixed revision identity')

    @property
    def piece_id(self) -> str:
        fields = [VERSION, KIND, self.manifest_id, self.revision.to_list()]
        return hashlib.sha256(_json_bytes(fields)).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceReading:
    value: int
    offset: int
    escape_offset: int | None = None
    from_previous: bool = False


@dataclass(frozen=True, slots=True)
class RecordEnd:
    offset: int


@dataclass(frozen=True, slots=True)
class EvidenceBranch:
    events: tuple[EvidenceReading | RecordEnd, ...]
    trailing_escape: int | None = None


@dataclass(frozen=True, slots=True)
class SegmentEvidence:
    ordinary: EvidenceBranch
    escaped: EvidenceBranch | None


@dataclass(frozen=True, slots=True)
class PieceEnvelope:
    identity: PieceIdentity
    evidence: SegmentEvidence
    digest: str


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _branch_data(branch: EvidenceBranch | None) -> dict | None:
    if branch is None:
        return None
    events = []
    for event in branch.events:
        if isinstance(event, RecordEnd):
            events.append(['end', event.offset])
        else:
            events.append(['read', event.value, event.offset, event.escape_offset, event.from_previous])
    return {'events': events, 'tail': branch.trailing_escape}


def _unsigned_fields(identity: PieceIdentity, evidence: SegmentEvidence) -> dict:
    return {
        'version': VERSION,
        'kind': KIND,
        'piece_id': identity.piece_id,
        'identity': [identity.manifest_id, identity.revision.to_list()],
        'body': {'ordinary': _branch_data(evidence.ordinary), 'escaped': _branch_data(evidence.escaped)},
    }


def piece_digest(identity: PieceIdentity, evidence: SegmentEvidence) -> str:
    return hashlib.sha256(_json_bytes(_unsigned_fields(identity, evidence))).hexdigest()


def encode_piece(envelope: PieceEnvelope) -> bytes:
    fields = _unsigned_fields(envelope.identity, envelope.evidence)
    fields['digest'] = envelope.digest
    return _json_bytes(fields)


def _offset(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('invalid evidence offset')
    return value


def _read_event(data: object) -> EvidenceReading | RecordEnd:
    if type(data) is not list or not data:
        raise ValueError('invalid evidence event')
    if data[0] == 'end' and len(data) == 2:
        return RecordEnd(_offset(data[1]))
    if data[0] != 'read' or len(data) != 5:
        raise ValueError('unknown evidence event')
    _, value, offset, escape_offset, from_previous = data
    if type(value) is not int or not 0 <= value <= 255 or type(from_previous) is not bool:
        raise ValueError('invalid reading evidence')
    offset = _offset(offset)
    if escape_offset is not None:
        if from_previous or _offset(escape_offset) + 1 != offset:
            raise ValueError('invalid same-segment escape evidence')
    elif not from_previous and value in (RECORD_END, ESCAPE):
        raise ValueError('control byte has no escape evidence')
    return EvidenceReading(value, offset, escape_offset, from_previous)


def _read_branch(data: object, entering_escaped: bool) -> tuple[EvidenceBranch, tuple[int | None, int | None]]:
    if type(data) is not dict or set(data) != {'events', 'tail'} or type(data['events']) is not list:
        raise ValueError('invalid evidence branch')
    events = tuple(_read_event(item) for item in data['events'])
    first_offset = None
    next_offset = None
    for index, event in enumerate(events):
        needs_previous = isinstance(event, EvidenceReading) and event.from_previous
        if needs_previous != (entering_escaped and index == 0):
            raise ValueError('invalid incoming escape binding')
        start = event.offset
        if isinstance(event, EvidenceReading) and event.escape_offset is not None:
            start = event.escape_offset
        if next_offset is None:
            first_offset = start
        elif start != next_offset:
            raise ValueError('nonconsecutive locations in evidence branch')
        next_offset = event.offset + 1
    tail = data['tail']
    if tail is not None:
        tail = _offset(tail)
        if not events:
            if entering_escaped:
                raise ValueError('first byte must consume the incoming escape')
            first_offset = tail
        elif tail != next_offset:
            raise ValueError('invalid outgoing escape location')
        next_offset = tail + 1
    return EvidenceBranch(events, tail), (first_offset, next_offset)


def decode_piece(payload: bytes) -> PieceEnvelope:
    if type(payload) is not bytes:
        raise ValueError('piece must be bytes')
    try:
        fields = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid piece encoding') from exc
    if type(fields) is not dict or set(fields) != {'version', 'kind', 'piece_id', 'identity', 'body', 'digest'}:
        raise ValueError('invalid piece envelope fields')
    if type(fields['version']) is not int or fields['version'] != VERSION or fields['kind'] != KIND:
        raise ValueError('unsupported piece envelope')
    identity_fields = fields['identity']
    if type(identity_fields) is not list or len(identity_fields) != 2:
        raise ValueError('invalid piece identity')
    identity = PieceIdentity(identity_fields[0], RevisionRef.from_list(identity_fields[1]))
    if fields['piece_id'] != identity.piece_id:
        raise ValueError('piece number does not match its identity')
    digest = fields['digest']
    if type(digest) is not str or len(digest) != 64 or digest.strip('0123456789abcdef'):
        raise ValueError('invalid piece digest field')
    body = fields['body']
    if type(body) is not dict or set(body) != {'ordinary', 'escaped'}:
        raise ValueError('invalid segment evidence fields')
    ordinary, ordinary_span = _read_branch(body['ordinary'], entering_escaped=False)
    escaped = None
    if body['escaped'] is not None:
        escaped, escaped_span = _read_branch(body['escaped'], entering_escaped=True)
        if ordinary_span != escaped_span:
            raise ValueError('branch spans disagree')
    envelope = PieceEnvelope(identity, SegmentEvidence(ordinary, escaped), digest)
    if encode_piece(envelope) != payload:
        raise ValueError('noncanonical piece encoding')
    return envelope
