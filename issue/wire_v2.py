"""V2 windowed piece wire format.

A V2 piece is canonical UTF-8 JSON.  The digest covers every outer member
except ``digest`` itself, using sorted keys and compact separators (the same
convention as V1).  Boundary pieces carry framing summaries only at the data
body endpoints and the permitted window endpoints; window pieces carry the
original window bytes only.
"""

import hashlib
import json
from dataclasses import dataclass

from source.locations import RevisionRef, require_name
from source.raw_store import RawRevision


V2_VERSION = 2
KIND_BOUNDARY = 'windowed-boundary'
KIND_WINDOW = 'windowed-window'

# Escape marker used in checkpoint triples: an incoming escape from the
# preceding segment has no local raw offset.
INCOMING_ESCAPE = -1


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest_fields(fields: dict) -> str:
    unsigned = {key: value for key, value in fields.items() if key != 'digest'}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


@dataclass(frozen=True, slots=True)
class V2Identity:
    manifest_id: str
    revision: RevisionRef
    window: str | None

    def __post_init__(self) -> None:
        require_name(self.manifest_id)
        if not isinstance(self.revision, RevisionRef):
            raise ValueError('piece requires a fixed revision identity')
        if self.window is not None:
            require_name(self.window)

    @property
    def kind(self) -> str:
        return KIND_BOUNDARY if self.window is None else KIND_WINDOW

    @property
    def piece_id(self) -> str:
        fields = [V2_VERSION, self.kind, self.manifest_id, self.revision.to_list(), self.window]
        return hashlib.sha256(canonical_json(fields)).hexdigest()


@dataclass(frozen=True, slots=True)
class V2Checkpoint:
    coord: int
    # Each triple is (record_end_count, open_reading_count, pending_escape).
    # pending_escape is None, a raw offset, or INCOMING_ESCAPE.
    normal: tuple[int, int, int | None]
    escaped: tuple[int, int, int | None]

    def to_list(self) -> list:
        return [self.coord, list(self.normal), list(self.escaped)]


@dataclass(frozen=True, slots=True)
class V2BoundaryBody:
    body_start: int
    body_end: int
    windows: tuple[tuple[str, int, int], ...]
    checkpoints: tuple[V2Checkpoint, ...]


@dataclass(frozen=True, slots=True)
class V2WindowBody:
    window_name: str
    start: int
    end: int
    data: bytes


@dataclass(frozen=True, slots=True)
class V2Envelope:
    kind: str
    piece_id: str
    identity: V2Identity
    body: V2BoundaryBody | V2WindowBody
    digest: str

    @property
    def version(self) -> int:
        return V2_VERSION


def _scan_prefixes(
    raw_bytes: bytes, body_start: int, body_end: int, coords: list[int], *, entering_escaped: bool
) -> dict[int, tuple[int, int, int | None]]:
    ends = 0
    open_count = 0
    pending: int | None = INCOMING_ESCAPE if entering_escaped else None
    wanted = set(coords)
    result: dict[int, tuple[int, int, int | None]] = {}
    if body_start in wanted:
        result[body_start] = (ends, open_count, pending)
    for offset in range(body_start, body_end):
        octet = raw_bytes[offset]
        if pending is not None:
            # The pending escape (local or incoming) makes this a data byte.
            open_count += 1
            pending = None
        elif octet == 0x1B:
            pending = offset
        elif octet == 0x0A:
            ends += 1
            open_count = 0
        else:
            open_count += 1
        coord = offset + 1
        if coord in wanted:
            result[coord] = (ends, open_count, pending)
    return result


def build_checkpoints(raw: RawRevision, window_coords: tuple[tuple[str, int, int], ...]) -> tuple[V2Checkpoint, ...]:
    coords = {raw.body_start, raw.body_end}
    for _name, start, end in window_coords:
        coords.add(start)
        coords.add(end)
    ordered = sorted(coords)
    normal = _scan_prefixes(
        raw.raw_bytes, raw.body_start, raw.body_end, ordered, entering_escaped=False
    )
    escaped = _scan_prefixes(
        raw.raw_bytes, raw.body_start, raw.body_end, ordered, entering_escaped=True
    )
    return tuple(V2Checkpoint(coord, normal[coord], escaped[coord]) for coord in ordered)


def _unsigned_fields(envelope: V2Envelope) -> dict:
    identity = envelope.identity
    if isinstance(envelope.body, V2BoundaryBody):
        body = {
            'body': [envelope.body.body_start, envelope.body.body_end],
            'windows': [[name, start, end] for name, start, end in envelope.body.windows],
            'checkpoints': [item.to_list() for item in envelope.body.checkpoints],
        }
        window_name = None
    else:
        body = {
            'window': [envelope.body.window_name, envelope.body.start, envelope.body.end],
            'data_hex': envelope.body.data.hex(),
        }
        window_name = envelope.body.window_name
    return {
        'version': V2_VERSION,
        'kind': identity.kind,
        'piece_id': identity.piece_id,
        'identity': [identity.manifest_id, identity.revision.to_list(), window_name],
        'body': body,
    }


def v2_digest(envelope: V2Envelope) -> str:
    return digest_fields(_unsigned_fields(envelope))


def encode_v2(envelope: V2Envelope) -> bytes:
    fields = _unsigned_fields(envelope)
    fields['digest'] = envelope.digest
    return canonical_json(fields)


def _exact_int(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError('invalid integer field in V2 piece')
    return value


def _decode_triple(data: object) -> tuple[int, int, int | None]:
    if type(data) is not list or len(data) != 3:
        raise ValueError('invalid framing checkpoint')
    ends, open_count, pending = data
    _exact_int(ends)
    _exact_int(open_count)
    if pending is not None:
        _exact_int(pending, minimum=-1)
    return ends, open_count, pending


def _decode_boundary_body(body: dict) -> V2BoundaryBody:
    if set(body) != {'body', 'windows', 'checkpoints'}:
        raise ValueError('invalid boundary body fields')
    span = body['body']
    if type(span) is not list or len(span) != 2:
        raise ValueError('invalid body range')
    body_start = _exact_int(span[0])
    body_end = _exact_int(span[1])
    if body_start > body_end:
        raise ValueError('invalid body range')
    windows_data = body['windows']
    if type(windows_data) is not list:
        raise ValueError('invalid permitted windows')
    windows = []
    for item in windows_data:
        if type(item) is not list or len(item) != 3:
            raise ValueError('invalid permitted window')
        name, start, end = item
        require_name(name)
        start = _exact_int(start)
        end = _exact_int(end, minimum=1)
        if not body_start <= start < end <= body_end:
            raise ValueError('permitted window outside body range')
        windows.append((name, start, end))
    names = [name for name, _s, _e in windows]
    if len(set(names)) != len(names):
        raise ValueError('permitted window names must be unique')
    windows_tuple = tuple(windows)

    points_data = body['checkpoints']
    if type(points_data) is not list or not points_data:
        raise ValueError('missing framing checkpoints')
    checkpoints = []
    previous_coord = None
    for item in points_data:
        if type(item) is not list or len(item) != 3:
            raise ValueError('invalid framing checkpoint entry')
        coord = _exact_int(item[0])
        if previous_coord is not None and coord <= previous_coord:
            raise ValueError('checkpoints must be strictly ordered')
        previous_coord = coord
        normal = _decode_triple(item[1])
        escaped = _decode_triple(item[2])
        checkpoints.append(V2Checkpoint(coord, normal, escaped))
    if checkpoints[0].coord != body_start or checkpoints[-1].coord != body_end:
        raise ValueError('checkpoints must cover both body endpoints')
    first = checkpoints[0]
    if first.normal != (0, 0, None) or first.escaped != (0, 0, INCOMING_ESCAPE):
        raise ValueError('invalid entry checkpoint state')
    covered_coords = {item.coord for item in checkpoints}
    expected_coords = {body_start, body_end}
    for _name, start, end in windows_tuple:
        expected_coords.add(start)
        expected_coords.add(end)
    if covered_coords != expected_coords:
        raise ValueError('checkpoints must cover every window endpoint')
    return V2BoundaryBody(body_start, body_end, windows_tuple, tuple(checkpoints))


def _decode_window_body(body: dict) -> V2WindowBody:
    if set(body) != {'window', 'data_hex'}:
        raise ValueError('invalid window body fields')
    coord = body['window']
    if type(coord) is not list or len(coord) != 3:
        raise ValueError('invalid window binding')
    name, start, end = coord
    require_name(name)
    start = _exact_int(start)
    end = _exact_int(end, minimum=1)
    if start >= end:
        raise ValueError('invalid window coordinates')
    text = body['data_hex']
    if type(text) is not str or len(text) % 2 or any(ch not in '0123456789abcdef' for ch in text):
        raise ValueError('window bytes must be lowercase hex')
    data = bytes.fromhex(text)
    if len(data) != end - start:
        raise ValueError('window byte length does not match its coordinates')
    return V2WindowBody(name, start, end, data)


def decode_v2(payload: bytes) -> V2Envelope:
    if type(payload) is not bytes:
        raise ValueError('piece must be bytes')
    try:
        fields = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid piece encoding') from exc
    if type(fields) is not dict or set(fields) != {
        'version', 'kind', 'piece_id', 'identity', 'body', 'digest'
    }:
        raise ValueError('invalid V2 piece envelope fields')
    if type(fields['version']) is not int or fields['version'] != V2_VERSION:
        raise ValueError('unsupported V2 piece version')
    kind = fields['kind']
    if kind not in (KIND_BOUNDARY, KIND_WINDOW):
        raise ValueError('unknown V2 piece kind')
    identity_data = fields['identity']
    if type(identity_data) is not list or len(identity_data) != 3:
        raise ValueError('invalid V2 piece identity')
    manifest_id, ref_data, window_name = identity_data
    revision = RevisionRef.from_list(ref_data)
    if window_name is not None:
        require_name(window_name)
    identity = V2Identity(manifest_id, revision, window_name)
    if identity.kind != kind:
        raise ValueError('V2 piece kind does not match its binding')
    piece_id = fields['piece_id']
    if type(piece_id) is not str or piece_id != identity.piece_id:
        raise ValueError('piece number does not match its identity')
    digest = fields['digest']
    if type(digest) is not str or len(digest) != 64 or digest.strip('0123456789abcdef'):
        raise ValueError('invalid piece digest field')
    body_data = fields['body']
    if type(body_data) is not dict:
        raise ValueError('invalid V2 piece body')
    if kind == KIND_BOUNDARY:
        body = _decode_boundary_body(body_data)
        if window_name is not None:
            raise ValueError('boundary piece must not bind a window name')
    else:
        body = _decode_window_body(body_data)
        if window_name != body.window_name:
            raise ValueError('window identity mismatch')
    envelope = V2Envelope(kind, piece_id, identity, body, digest)
    if encode_v2(envelope) != payload:
        raise ValueError('noncanonical piece encoding')
    return envelope


def is_v2_payload(payload: object) -> bool:
    if type(payload) is not bytes:
        return False
    try:
        fields = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return False
    return type(fields) is dict and fields.get('version') == V2_VERSION
